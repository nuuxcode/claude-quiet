"""
Applies a small set of edits to the Claude Code executable on this machine.

Claude Code's program code lives inside its executable, so a change means
opening it, editing, closing it back up, and doing that again whenever Claude
Code updates itself and replaces the file.

Nothing here downloads anything on its own, and nothing new runs without you
asking for it. Updates are checked only when you run the update command, which
shows you what changed and waits for a yes.

Nothing here is irreversible. The untouched original is kept, and restoring it
puts the file back byte for byte.

Safety rules, enforced rather than remembered:

  - every edit must match exactly as many times as expected, or nothing is
    written and the executable is left alone
  - every edit must leave bracket balance unchanged, or nothing is written
  - the new file is built to one side and run once before it is allowed to
    replace the working one
  - registry writes and binary swaps are journaled, atomic and recoverable
  - the saved original is accepted only after its complete digest matches
  - patch operations are serialized by an owner-safe kernel lock
  - the unpack/repack helper comes from a committed lockfile and installs with
    package lifecycle scripts disabled
  - re-applying after a Claude Code update happens by itself, because it is the
    same code you already approved. Code that CHANGED on disk is never applied
    on its own: you are told, and it waits for you to install it
  - more than one patch can be installed at once without damaging each other:
    there is one saved original and one record of what is enabled, and any
    change rebuilds from the original applying each enabled patch in order
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import inspect
import json
import mmap
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Pattern

HOME = Path.home()
STATE = Path(os.environ.get("CLAUDE_PATCH_HOME", HOME / ".claude-patch"))
REGISTRY = STATE / "registry.json"
TRANSACTION = STATE / "transaction.json"
BACKUPS = STATE / "backups"
WORK = STATE / "work"
TWEAKCC = STATE / "tweakcc"
TWEAKCC_MANIFEST = Path(__file__).resolve().parent / "tweakcc-package"
TWEAKCC_VERSION = "4.3.2"
KEEP_BACKUPS = 2


class PatchError(RuntimeError):
    """The binary does not look the way we expect. Never fatal, never silent."""


# ---------------------------------------------------------------------------
# What a patch is
# ---------------------------------------------------------------------------

@dataclass
class Edit:
    """
    One anchored change.

    `build` is called for every match and returns the replacement text. It may
    take either the match alone, or the match plus the whole file:

        build(m)        the usual case
        build(m, js)    when the replacement needs a name that lives elsewhere

    The second form exists because minified names cannot be hardcoded. A
    replacement sometimes has to call a function whose real name is only
    discoverable from a different part of the file, and a regex cannot
    practically span megabytes to capture both.
    """
    name: str
    anchor: Pattern
    build: Callable
    count: int = 1

    def replacement(self, m, js: str) -> str:
        try:
            wants_js = len(inspect.signature(self.build).parameters) >= 2
        except (TypeError, ValueError):
            wants_js = False
        return self.build(m, js) if wants_js else self.build(m)


@dataclass
class Patch:
    name: str
    summary: str
    marker: str
    edits: list
    version: str = "0"
    usage: str = ""
    extra_status: Callable | None = field(default=None)
    # migrate(binary, log): called before the binary is read, so an older
    # release of this patch can hand back a clean binary. Must be idempotent
    # and must do nothing when there is nothing to upgrade.
    migrate: Callable | None = field(default=None)

    def apply(self, js: str, log=lambda s: None) -> str:
        for e in self.edits:
            hits = list(e.anchor.finditer(js))
            if len(hits) != e.count:
                raise PatchError(
                    f"{self.name}: edit '{e.name}' matched {len(hits)} times, "
                    f"expected {e.count}. Claude Code's internals changed; "
                    "refusing to guess."
                )
            # Right to left, so earlier offsets stay valid.
            for m in reversed(hits):
                old, new = m.group(0), e.replacement(m, js)
                for o, c in (("{", "}"), ("(", ")"), ("[", "]")):
                    if (old.count(o) - old.count(c)) != (new.count(o) - new.count(c)):
                        raise PatchError(
                            f"{self.name}: edit '{e.name}' would change {o}{c} balance"
                        )
                js = js[: m.start()] + new + js[m.end():]
            log(f"    {e.name}  ({e.count}x)")
        return js


# ---------------------------------------------------------------------------
# Registry: which patches are enabled, and where their definitions live
# ---------------------------------------------------------------------------

def _empty_registry() -> dict:
    return {"stock": None, "patches": {}}


def _validate_registry(reg: object) -> dict:
    if not isinstance(reg, dict):
        raise PatchError("registry root must be an object")
    patches = reg.get("patches", {})
    if not isinstance(patches, dict):
        raise PatchError("registry patches must be an object")
    for name, entry in patches.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise PatchError("registry patch entries must be named objects")
        if not isinstance(entry.get("definition"), str):
            raise PatchError(f"registry patch {name!r} has no definition path")
        if "approved" in entry and not isinstance(entry["approved"], str):
            raise PatchError(f"registry patch {name!r} has an invalid approval digest")
    if reg.get("stock") is not None and not isinstance(reg.get("stock"), dict):
        raise PatchError("registry stock entry must be an object or null")
    if reg.get("built") is not None and not isinstance(reg.get("built"), dict):
        raise PatchError("registry built entry must be an object or null")
    reg.setdefault("stock", None)
    reg.setdefault("patches", {})
    return reg


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = None
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        path.chmod(mode)
    finally:
        if fd is not None:
            os.close(fd)
        tmp.unlink(missing_ok=True)


def read_registry() -> dict:
    if not REGISTRY.exists():
        return _empty_registry()
    try:
        return _validate_registry(json.loads(REGISTRY.read_text()))
    except (OSError, json.JSONDecodeError) as e:
        raise PatchError(f"cannot read registry {REGISTRY}: {e}") from e


def write_registry(reg: dict) -> None:
    _validate_registry(reg)
    _atomic_write_text(REGISTRY, json.dumps(reg, indent=2) + "\n")


def _load_patch_from(entry: dict):
    """Import a registered patch's definition from its own repo."""
    path = Path(entry["definition"])
    if not path.exists():
        raise PatchError(f"definition missing: {path}")
    spec = importlib.util.spec_from_file_location(f"_ccp_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise PatchError(f"cannot load a patch definition from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    patch = getattr(mod, "PATCH", None)
    if patch is None:
        raise PatchError(f"{path} does not define PATCH")
    return patch


def enabled_patches(reg: dict) -> list:
    out = []
    for name, entry in reg.get("patches", {}).items():
        try:
            out.append(_load_patch_from(entry))
        except PatchError as e:
            print(f"  warning: skipping {name}: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Finding the binary
# ---------------------------------------------------------------------------

def find_binary(skip_dirs: list | None = None) -> Path:
    """The real Claude Code executable, never a launcher shim."""
    skip = {Path(d).resolve() for d in (skip_dirs or [])}
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            d = Path(entry).resolve()
            if d in skip:
                continue
            p = Path(entry) / "claude"
            if not p.exists():
                continue
            real = p.resolve()
            if real.stat().st_size > 50_000_000:  # the real one is ~250 MB
                return real
        except OSError:
            continue
    raise PatchError("could not find the Claude Code executable on PATH")


def identity(path: Path) -> dict:
    st = path.stat()
    return {"size": st.st_size, "mtime": int(st.st_mtime), "ino": st.st_ino}


def fast_signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size} {int(st.st_mtime)} {st.st_ino}"


def definition_digest(path: Path) -> str:
    """Content hash of a patch definition, so "has this changed" is about the
    code itself and not about a timestamp."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def unapproved_patches(reg: dict) -> list:
    """Patches whose code on disk is not what the user approved.

    Nothing here ever downloads anything. This is only about code already on
    the machine having changed since it was installed, which is exactly when a
    human should look before it runs.
    """
    out = []
    for name, entry in sorted(reg.get("patches", {}).items()):
        approved = entry.get("approved")
        current = definition_digest(Path(entry["definition"]))
        if approved is None or approved != current:
            out.append(name)
    return out


def watched_definitions(reg: dict) -> list:
    """Definition files whose contents decide what the binary should contain."""
    return [Path(e["definition"])
            for _, e in sorted(reg.get("patches", {}).items())]


def combined_signature(binary: Path, reg: dict) -> str:
    """What the binary should be built from, in one cheap line.

    Covers the binary AND every patch definition. Without the definitions, a
    `git pull` bringing a newer version of a patch would sit unused until
    something else happened to trigger a rebuild, and the user would think they
    had upgraded when they had not.
    """
    parts = [fast_signature(binary)]
    for d in watched_definitions(reg):
        try:
            st = d.stat()
            parts.append(f"{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append("missing")
    return " ".join(parts)


def _binary_is_ours(binary: Path, reg: dict) -> bool:
    """Is this exact file the one we last wrote?

    Narrower than is_current on purpose: it asks only about the binary, so it
    stays true when a patch definition changes underneath us.
    """
    built = reg.get("built") or {}
    return bool(built) and built.get("identity") == identity(binary)


def is_current(binary: Path | None = None) -> bool:
    """Is the binary on disk built from exactly what is on disk now?

    Broader than _binary_is_ours: false when a patch has been updated, which is
    what makes an update apply itself on the next launch.
    """
    reg = read_registry()
    if not reg.get("built"):
        return False
    try:
        binary = binary or find_binary()
    except PatchError:
        return False
    return reg["built"].get("signature") == combined_signature(binary, reg)


# ---------------------------------------------------------------------------
# tweakcc
# ---------------------------------------------------------------------------

def tweakcc_bin() -> Path:
    return TWEAKCC / "node_modules" / ".bin" / "tweakcc"


def _tweakcc_manifest_digest() -> str:
    package = TWEAKCC_MANIFEST / "package.json"
    lock = TWEAKCC_MANIFEST / "package-lock.json"
    if not package.is_file() or not lock.is_file():
        raise PatchError(f"locked tweakcc manifest is missing from {TWEAKCC_MANIFEST}")
    h = hashlib.sha256()
    h.update(package.read_bytes())
    h.update(lock.read_bytes())
    return h.hexdigest()


def ensure_tweakcc(log=print) -> Path:
    """Install the unpack/repack helper locally, pinned to a known version."""
    exe = tweakcc_bin()
    expected = _tweakcc_manifest_digest()
    installed = TWEAKCC / ".manifest-sha256"
    if exe.is_file() and installed.is_file() \
            and installed.read_text().strip() == expected:
        return exe
    if not shutil.which("npm"):
        raise PatchError("npm is required to install tweakcc (needs Node.js)")
    log(f"installing locked tweakcc@{TWEAKCC_VERSION} tree (one time)...")
    STATE.mkdir(parents=True, exist_ok=True)
    nonce = f"{os.getpid()}-{time.time_ns()}"
    staged = STATE / f"tweakcc-next-{nonce}"
    previous = STATE / f"tweakcc-old-{nonce}"
    moved_previous = False
    try:
        staged.mkdir(mode=0o700)
        shutil.copy2(TWEAKCC_MANIFEST / "package.json", staged / "package.json")
        shutil.copy2(TWEAKCC_MANIFEST / "package-lock.json", staged / "package-lock.json")
        r = subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--omit=dev", "--silent"],
            cwd=staged,
            capture_output=True, text=True,
        )
        staged_exe = staged / "node_modules" / ".bin" / "tweakcc"
        if r.returncode != 0 or not staged_exe.is_file():
            raise PatchError(f"tweakcc install failed: {r.stderr.strip()[:300]}")
        _atomic_write_text(staged / ".manifest-sha256", expected + "\n")
        if TWEAKCC.exists():
            os.replace(TWEAKCC, previous)
            moved_previous = True
        os.replace(staged, TWEAKCC)
        if moved_previous:
            shutil.rmtree(previous)
        return tweakcc_bin()
    except BaseException:
        if moved_previous and previous.exists() and not TWEAKCC.exists():
            os.replace(previous, TWEAKCC)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if previous.exists() and TWEAKCC.exists():
            shutil.rmtree(previous)


def _tweakcc(exe: Path, *args) -> None:
    r = subprocess.run([str(exe), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise PatchError(f"tweakcc {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

class Lock:
    """Anyone running several sessions will start them together right after an
    update, and every one of them will notice the patch is gone. Without this
    they fight over the same staged file."""

    def __init__(self, stale_after: int = 600):
        self.path = STATE / "lock"
        self.stale_after = stale_after
        self.fd = None
        self.owner = f"{os.getpid()}:{secrets.token_hex(16)}"

    @staticmethod
    def _owner_is_alive(value: str) -> bool:
        try:
            pid = int(value.split(":", 1)[0])
            if pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, TypeError, ValueError):
            return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            created = True
        except FileExistsError:
            self.fd = os.open(self.path, os.O_RDWR)
            created = False
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            os.close(self.fd)
            self.fd = None
            raise PatchError("another patch run is in progress") from e
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            recorded = os.read(self.fd, 256).decode(errors="replace").strip()
            age = time.time() - os.fstat(self.fd).st_mtime
            has_pid = recorded.split(":", 1)[0].isdigit()
            if self._owner_is_alive(recorded):
                raise PatchError("another patch run is in progress")
            if not created and not recorded and age <= self.stale_after:
                # A live pre-flock version may have created the file but not
                # written its PID yet. A clean release writes "released".
                raise PatchError("another patch run is in progress")
            if recorded not in ("", "released") and not has_pid \
                    and age <= self.stale_after:
                raise PatchError("another patch run is in progress")
            os.ftruncate(self.fd, 0)
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.write(self.fd, self.owner.encode())
            os.fsync(self.fd)
            return self
        except BaseException:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, *exc):
        if self.fd is None:
            return False
        try:
            current = os.fstat(self.fd)
            path_stat = self.path.stat()
            os.lseek(self.fd, 0, os.SEEK_SET)
            recorded = os.read(self.fd, 256).decode(errors="replace").strip()
            if (current.st_dev, current.st_ino) == \
                    (path_stat.st_dev, path_stat.st_ino) \
                    and recorded == self.owner:
                os.ftruncate(self.fd, 0)
                os.lseek(self.fd, 0, os.SEEK_SET)
                os.write(self.fd, b"released")
                os.fsync(self.fd)
        except OSError:
            pass
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        return False


# ---------------------------------------------------------------------------
# The one operation: rebuild from stock
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    """Content identity for the stock binary. ~0.5s on 250 MB, and only ever
    run on a rebuild, which happens once per Claude Code update."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def _version_of(binary: Path) -> str:
    try:
        r = subprocess.run([str(binary), "--version"], capture_output=True,
                           text=True, timeout=120)
        return r.stdout.strip().split()[0] or "unknown"
    except Exception:
        return "unknown"


def _hardlinks_of(target: Path) -> list:
    """The npm package points two paths at one inode. Replacing the file breaks
    that, so record the siblings and recreate them.

    Confined to the Claude Code package directory on purpose: walking an
    unverified parent can mean walking all of /opt/homebrew for nothing.
    """
    st = target.stat()
    if st.st_nlink < 2:
        return []
    root = target.parent
    if root.name == "bin":
        root = root.parent
    if root.name != "claude-code":
        return []
    found = []
    for p in root.rglob("*"):
        try:
            if p != target and p.is_file() and not p.is_symlink() \
                    and p.stat().st_ino == st.st_ino:
                found.append(p)
        except OSError:
            continue
    return found


def _prune_backups() -> None:
    """Each backup is ~250 MB."""
    if not BACKUPS.exists():
        return
    for old in sorted(BACKUPS.glob("claude-*.orig"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _carries_our_work(binary: Path, patches: list, reg: dict) -> bool:
    """Does this binary already contain one of our patches?

    Asked of the file itself rather than of our records, because the records
    can be right about the binary and still stale about everything else. When a
    patch is updated the binary is unchanged but no longer matches what we
    would build, and answering "not ours" there would save our own patched
    binary as the pristine original and bake it in permanently.

    Markers we have EVER written count, not just the current ones. A patch that
    changes its marker between releases would otherwise stop recognising its own
    output, which is the same trap by another route.
    """
    seen = set(m for m in (reg.get("built") or {}).get("markers", []) if m)
    seen.update(p.marker for p in patches if p.marker)
    markers = [m.encode() for m in seen]
    if not markers:
        return False
    try:
        with open(binary, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return any(mm.find(m) != -1 for m in markers)
    except (OSError, ValueError):
        return False


def _verified_stock(reg: dict) -> tuple[Path, str]:
    stock = reg.get("stock") or {}
    recorded = stock.get("path")
    expected = stock.get("sha256")
    if not isinstance(recorded, str) or not recorded:
        raise PatchError("the stock backup path is not recorded")
    path = Path(recorded)
    try:
        st = path.lstat()
    except OSError as e:
        raise PatchError(f"the stock backup is missing: {path}") from e
    if path.is_symlink() or not stat.S_ISREG(st.st_mode):
        raise PatchError(f"the stock backup is not a regular file: {path}")
    if not isinstance(expected, str) or len(expected) not in (12, 64):
        raise PatchError("the stock backup has no valid recorded digest")
    actual = _sha256(path)
    good = actual == expected if len(expected) == 64 else actual.startswith(expected)
    if not good:
        raise PatchError(
            f"the stock backup digest does not match its registry record: {path}")
    return path, actual


def _ensure_stock(binary: Path, reg: dict, log, patches: list) -> Path:
    """The pristine binary for this version.

    The only dangerous case is copying a binary that already carries our own
    patches and calling it stock, which bakes them in with no way back. Two
    independent checks guard it: our record of what we last built, and the
    contents of the file itself.

    This must not key off "are any patches registered", because on the very
    first install the patch is registered a moment before the first build.
    """
    if _binary_is_ours(binary, reg) or _carries_our_work(binary, patches, reg):
        # The binary on disk is our own build, so it is not stock. Use the
        # backup we recorded when we built it.
        backup, digest = _verified_stock(reg)
        reg["stock"]["sha256"] = digest
        return backup

    # Not our build, so this IS stock: a fresh install, or what a Claude Code
    # update just wrote. Name the backup by content, not by version alone: a
    # release that reuses a version number but ships different bytes would
    # otherwise be rebuilt from a stale backup, silently undoing the update.
    version = _version_of(binary)
    digest = _sha256(binary)
    backup = BACKUPS / f"claude-{version}-{digest[:12]}.orig"

    if not backup.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        log(f"  saving stock binary ({version})")
        shutil.copy2(binary, backup)
    backup_digest = _sha256(backup)
    if backup_digest != digest:
        raise PatchError(f"the stock backup does not match the source binary: {backup}")
    reg["stock"] = {"version": version, "sha256": digest, "path": str(backup)}
    _prune_backups()
    return backup


def _drop_missing_patches(reg: dict, log) -> list[str]:
    missing = []
    for name, entry in sorted(list(reg.get("patches", {}).items())):
        definition = Path(entry["definition"])
        if definition.is_file():
            continue
        missing.append(name)
        del reg["patches"][name]
        log(f"  warning: removing {name}: definition missing: {definition}")
    return missing


def _write_runtime_state(reg: dict, binary: Path) -> None:
    built = reg.get("built") or {}
    _atomic_write_text(STATE / "target", str(binary) + "\n")
    _atomic_write_text(STATE / "fast.stamp", str(built.get("signature", "")) + "\n")
    _atomic_write_text(
        STATE / "watch", "\n".join(str(d) for d in watched_definitions(reg)) + "\n")


def _restore_hardlinks(binary: Path, links: list[Path]) -> None:
    for link in links:
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            os.link(binary, link)
        except OSError as e:
            raise PatchError(f"could not restore hardlink {link}: {e}") from e


def recover_transaction(log=lambda s: None) -> bool:
    """Roll back a commit interrupted after the binary swap began."""
    if not TRANSACTION.exists():
        return False
    try:
        tx = json.loads(TRANSACTION.read_text())
        before = _validate_registry(tx["registry_before"])
        binary = Path(tx["binary"])
        rollback = Path(tx["rollback"])
        links = [Path(p) for p in tx.get("hardlinks", [])]
    except (OSError, KeyError, TypeError, json.JSONDecodeError, PatchError) as e:
        raise PatchError(f"cannot read interrupted transaction {TRANSACTION}: {e}") from e

    expected_prefix = f".{binary.name}.claude-patch-"
    if rollback.parent != binary.parent or not rollback.name.startswith(expected_prefix):
        raise PatchError("interrupted transaction has an unsafe rollback path")
    if not rollback.is_file() or rollback.is_symlink():
        raise PatchError(f"interrupted transaction rollback is missing: {rollback}")
    allowed_root = binary.parent.parent.resolve()
    for link in links:
        if not link.parent.resolve().is_relative_to(allowed_root):
            raise PatchError(f"interrupted transaction has an unsafe hardlink path: {link}")

    os.replace(rollback, binary)
    binary.chmod(0o755)
    _restore_hardlinks(binary, links)
    write_registry(before)
    _write_runtime_state(before, binary)
    TRANSACTION.unlink()
    log("  recovered an interrupted patch transaction")
    return True


def rebuild(log=print, skip_dirs=None, require_approved: bool = False,
            registry: dict | None = None) -> bool:
    """Rebuild the binary as stock + every enabled patch. The only mutator.

    require_approved is set when this runs by itself, from the launcher. Then
    it will re-apply code the user already agreed to, but it will not apply
    code that changed since. Repairing after a Claude Code update is safe and
    automatic; running something new is a decision, and decisions belong to the
    person, not to a background task.
    """
    binary = find_binary(skip_dirs)

    with Lock():
        recover_transaction(log)
        before = read_registry()
        reg = json.loads(json.dumps(registry if registry is not None else before))
        _validate_registry(reg)
        _drop_missing_patches(reg, log)
        if require_approved:
            changed = unapproved_patches(reg)
            if changed:
                raise PatchError(
                    f"{', '.join(changed)} changed on disk since you installed "
                    f"it. Nothing has been applied. Review the changes, then "
                    f"run: {changed[0]} install"
                )
        patches = enabled_patches(reg)

        # Upgrades run before anything else touches the binary. A patch that
        # shipped an older, differently-built version has to hand back a clean
        # binary here, or the next step would save the OLD patch's output as
        # the pristine original and bake it in permanently.
        for p in patches:
            if p.migrate:
                p.migrate(binary, log)

        stock = _ensure_stock(binary, reg, log, patches)

        WORK.mkdir(parents=True, exist_ok=True)
        nonce = f"{os.getpid()}-{time.time_ns()}"
        staged = WORK / f"claude-{nonce}.staged"
        js_path = WORK / f"cli-{nonce}.js"
        install_staged = binary.parent / f".{binary.name}.claude-patch-{nonce}.staged"
        rollback = binary.parent / f".{binary.name}.claude-patch-{nonce}.rollback"
        journal_written = False

        try:
            if not patches:
                log("no patches enabled; restoring stock Claude Code")
                shutil.copy2(stock, staged)
            else:
                exe = ensure_tweakcc(log)
                shutil.copy2(stock, staged)
                staged.chmod(0o755)
                log("unpacking...")
                _tweakcc(exe, "unpack", str(js_path), str(staged))
                js = js_path.read_text(encoding="utf-8")
                for p in patches:
                    log(f"  applying {p.name}")
                    js = p.apply(js, log)
                js_path.write_text(js, encoding="utf-8")
                log("repacking...")
                _tweakcc(exe, "repack", str(js_path), str(staged))

            staged.chmod(0o755)
            log("verifying...")
            r = subprocess.run([str(staged), "--version"], capture_output=True,
                               text=True, timeout=120)
            if r.returncode != 0 or "Claude Code" not in r.stdout:
                raise PatchError(
                    f"rebuilt binary failed to run: {(r.stderr or r.stdout)[:200]}")

            shutil.copy2(staged, install_staged)
            install_staged.chmod(0o755)
            if _sha256(install_staged) != _sha256(staged):
                raise PatchError("copying the staged binary beside the target changed it")

            links = _hardlinks_of(binary)
            try:
                os.link(binary, rollback)
            except OSError:
                shutil.copy2(binary, rollback)
            tx = {
                "binary": str(binary),
                "rollback": str(rollback),
                "hardlinks": [str(p) for p in links],
                "registry_before": before,
            }
            _atomic_write_text(TRANSACTION, json.dumps(tx, indent=2) + "\n")
            journal_written = True

            os.replace(install_staged, binary)
            binary.chmod(0o755)
            _restore_hardlinks(binary, links)

            reg["built"] = {
                "identity": identity(binary),
                "version": _version_of(binary),
                "patches": {p.name: p.version for p in patches},
                "markers": sorted(set(
                    [m for m in (reg.get("built") or {}).get("markers", []) if m]
                    + [p.marker for p in patches if p.marker])),
                "signature": combined_signature(binary, reg),
                "at": int(time.time()),
            }
            write_registry(reg)
            _write_runtime_state(reg, binary)
            TRANSACTION.unlink()
            journal_written = False
            rollback.unlink(missing_ok=True)
            log(f"claude code {reg['built']['version']}: "
                + (", ".join(p.name for p in patches) if patches else "stock"))
            return True
        except BaseException:
            if journal_written:
                try:
                    recover_transaction(log)
                    journal_written = False
                except BaseException as rollback_error:
                    log(f"  warning: automatic rollback failed: {rollback_error}")
            raise
        finally:
            staged.unlink(missing_ok=True)
            js_path.unlink(missing_ok=True)
            install_staged.unlink(missing_ok=True)
            if not TRANSACTION.exists():
                rollback.unlink(missing_ok=True)


def _with_rollback(before: dict, desired: dict, log) -> bool:
    """Build the desired registry and publish it only with the binary."""
    return rebuild(log=log, registry=desired)


def enable(patch: Patch, definition: Path, log=print) -> bool:
    """Install, which is also the moment the user approves this code."""
    before = read_registry()
    reg = json.loads(json.dumps(before))
    definition = Path(definition).resolve()
    reg.setdefault("patches", {})[patch.name] = {
        "definition": str(definition),
        "approved": definition_digest(definition),
        "at": int(time.time()),
    }
    return _with_rollback(before, reg, log)


def disable(patch: Patch, log=print) -> bool:
    before = read_registry()
    if patch.name not in before.get("patches", {}):
        log(f"{patch.name} is not enabled")
        return False
    reg = json.loads(json.dumps(before))
    del reg["patches"][patch.name]
    return _with_rollback(before, reg, log)


def self_heal(log=lambda s: None) -> bool:
    """Called by the launcher. Must never stop Claude Code starting."""
    deadline = time.time() + 45
    while True:
        try:
            if TRANSACTION.exists():
                with Lock():
                    recover_transaction(log)
            if is_current():
                return True
            if not read_registry().get("patches"):
                return True
            return rebuild(log=log, require_approved=True)
        except PatchError as e:
            if "in progress" in str(e) and time.time() < deadline:
                time.sleep(1.5)
                continue
            log(f"claude-patch: leaving Claude Code as it is ({e})")
            return False
        except Exception as e:  # noqa: BLE001 - failing open is the point
            log(f"claude-patch: leaving Claude Code as it is ({e})")
            return False


# ---------------------------------------------------------------------------
# The CLI every patch gets for free
# ---------------------------------------------------------------------------

def _definition_path(patch: Patch) -> Path:
    """Where this patch is defined, so the registry can re-import it later.

    Only .py files count. The CLI entry point imports PATCH into its own
    namespace too, so it looks like a match, but it has no extension and
    importlib cannot load it back. The definition module is the one ending in
    .py, whether it was imported or run directly.
    """
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.endswith(".py") and getattr(m, "PATCH", None) is patch:
            return Path(f).resolve()
    raise PatchError("cannot locate this patch's definition file")


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _cmd_update(patch: Patch) -> int:
    """Check for a newer version, show what changed, and ask before applying.

    Deliberately not automatic. Fetching is safe, running new code is not, so
    the diff is shown and the decision stays with the person. Nothing is
    applied until you say yes.
    """
    definition = _definition_path(patch)
    repo = definition.parent.parent
    if not (repo / ".git").exists():
        print(f"{patch.name} was not installed from a git checkout.")
        print(f"Update it however you got it, then run: {patch.name} install")
        return 1

    print("checking for updates...")
    if _git(repo, "fetch", "--quiet").returncode != 0:
        print("could not reach the remote. Check your connection and try again.")
        return 1

    upstream = _git(repo, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip()
    if not upstream:
        print("this checkout has no upstream branch set; update it manually.")
        return 1

    behind = _git(repo, "rev-list", "--count", f"HEAD..{upstream}").stdout.strip()
    if behind in ("", "0"):
        print(f"{patch.name} is up to date (v{patch.version}).")
        return 0

    print(f"\n{behind} new commit(s):\n")
    print(_git(repo, "log", "--oneline", "--no-decorate", f"HEAD..{upstream}").stdout.rstrip())
    print("\nFiles that would change:\n")
    print(_git(repo, "diff", "--stat", f"HEAD..{upstream}").stdout.rstrip())
    print(f"\nReview it in full with:\n  git -C {repo} diff HEAD..{upstream}\n")

    if not sys.stdin.isatty():
        print(f"Not a terminal, so nothing was applied. Run `{patch.name} update` "
              "yourself, or pull and run install.")
        return 1
    try:
        reply = input("Apply this update? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not reply.startswith("y"):
        print("Nothing was applied.")
        return 1

    r = _git(repo, "merge", "--ff-only", upstream)
    if r.returncode != 0:
        print("could not fast-forward this checkout:")
        print((r.stderr or r.stdout).strip()[:400])
        return 1
    print("updated. applying...\n")
    enable(patch, definition)
    return 0


def main(patch: Patch, argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    usage = f"""{patch.name} - {patch.summary}

  {patch.name} status     is it on
  {patch.name} install    turn it on
  {patch.name} restore    turn it off (other patches stay on)
  {patch.name} verify     show the patched code, read from your own disk
  {patch.name} doctor     check everything this needs
  {patch.name} update     check for a newer version, show it, ask before applying
  {patch.name} heal       re-apply if a Claude Code update wiped it
{patch.usage}"""

    if cmd in ("-h", "--help", "help"):
        print(usage)
        return 0

    try:
        if TRANSACTION.exists():
            with Lock():
                recover_transaction(print)
        if cmd == "status":
            reg = read_registry()
            on = patch.name in reg.get("patches", {})
            try:
                binary = find_binary()
                print(f"binary   {binary}")
                print(f"version  {_version_of(binary)}")
            except PatchError as e:
                print(f"binary   not found: {e}")
            print(f"{patch.name:16} {'ON' if on else 'off'}  v{patch.version}")
            built = (reg.get("built") or {}).get("patches") or {}
            for n, v in built.items():
                if n != patch.name:
                    print(f"also on  {n}  v{v}")
            if on:
                changed = unapproved_patches(reg)
                if patch.name in changed:
                    built_v = built.get(patch.name)
                    what = (f"v{built_v} installed, v{patch.version} on disk"
                            if built_v and built_v != patch.version
                            else "the code on disk changed")
                    print(f"         update waiting: {what}.")
                    print(f"         review it, then run: {patch.name} install")
                elif not is_current():
                    print("         needs re-applying. Just launch claude.")
            if patch.extra_status:
                patch.extra_status()
            return 0

        if cmd in ("install", "patch", "enable"):
            enable(patch, _definition_path(patch))
            return 0

        if cmd in ("restore", "uninstall", "disable"):
            disable(patch)
            return 0

        if cmd == "update":
            return _cmd_update(patch)

        if cmd == "heal":
            return 0 if self_heal(log=print) else 1

        if cmd == "verify":
            binary = find_binary()
            exe = ensure_tweakcc()
            WORK.mkdir(parents=True, exist_ok=True)
            tmp, js_path = WORK / "verify.bin", WORK / "verify.js"
            shutil.copy2(binary, tmp)
            _tweakcc(exe, "unpack", str(js_path), str(tmp))
            js = js_path.read_text(encoding="utf-8")
            tmp.unlink(missing_ok=True)
            js_path.unlink(missing_ok=True)
            idx = js.find(patch.marker)
            if idx == -1:
                print(f"{patch.name} is NOT present in {binary}")
                return 1
            print(f"Read from YOUR binary: {binary}")
            print(f"{patch.name} found at offset {idx}\n")
            print("-" * 72)
            print(js[idx: idx + 400])
            print("-" * 72)
            print("\nEvery anchor must match its expected count or nothing is written.")
            print(f"Undo with: {patch.name} restore")
            return 0

        if cmd == "doctor":
            ok = True

            def check(label, good, detail=""):
                nonlocal ok
                ok = ok and good
                print(f"  [{'ok' if good else 'XX'}] {label}{'  ' + detail if detail else ''}")

            print(f"{patch.name} doctor")
            check("python3", sys.version_info >= (3, 9), sys.version.split()[0])
            node = shutil.which("node")
            check("node.js (tweakcc needs it)", bool(node), node or "")
            check("npm", bool(shutil.which("npm")))
            try:
                b = find_binary()
                check("claude code found", True, str(b))
                check("writable", os.access(b, os.W_OK) or os.access(b.parent, os.W_OK))
            except PatchError as e:
                check("claude code found", False, str(e))
            check("tweakcc installed", tweakcc_bin().exists(),
                  "" if tweakcc_bin().exists() else "(installed on first patch)")
            try:
                manifest_ok = ((TWEAKCC / ".manifest-sha256").read_text().strip()
                               == _tweakcc_manifest_digest())
            except (OSError, PatchError):
                manifest_ok = False
            check("tweakcc dependency tree locked", manifest_ok,
                  "" if manifest_ok else "(refreshed on next install)")
            reg = read_registry()
            stock = reg.get("stock") or {}
            check("stock backup saved", bool(stock), stock.get("version", ""))
            if stock:
                path = Path(stock.get("path", ""))
                try:
                    st = path.lstat()
                    regular = stat.S_ISREG(st.st_mode) and not path.is_symlink()
                except OSError:
                    regular = False
                check("stock backup is a regular file", regular, str(path))
                try:
                    _, actual = _verified_stock(reg)
                    full_digest = len(stock.get("sha256", "")) == 64 \
                        and stock.get("sha256") == actual
                except PatchError:
                    full_digest = False
                check("stock backup full digest", full_digest)
            return 0 if ok else 1

        print(usage)
        return 2

    except PatchError as e:
        print(f"{patch.name}: {e}", file=sys.stderr)
        print("Claude Code is unchanged.", file=sys.stderr)
        return 1
