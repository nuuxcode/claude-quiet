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
  - a failed build rolls back the record of what is installed
  - re-applying after a Claude Code update happens by itself, because it is the
    same code you already approved. Code that CHANGED on disk is never applied
    on its own: you are told, and it waits for you to install it
  - more than one patch can be installed at once without damaging each other:
    there is one saved original and one record of what is enabled, and any
    change rebuilds from the original applying each enabled patch in order
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import mmap
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Pattern

HOME = Path.home()
STATE = Path(os.environ.get("CLAUDE_PATCH_HOME", HOME / ".claude-patch"))
REGISTRY = STATE / "registry.json"
BACKUPS = STATE / "backups"
WORK = STATE / "work"
TWEAKCC = STATE / "tweakcc"
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

def read_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except Exception:
        return {"stock": None, "patches": {}}


def write_registry(reg: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2))


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


def ensure_tweakcc(log=print) -> Path:
    """Install the unpack/repack helper locally, pinned to a known version."""
    exe = tweakcc_bin()
    if exe.exists():
        return exe
    if not shutil.which("npm"):
        raise PatchError("npm is required to install tweakcc (needs Node.js)")
    log(f"installing tweakcc@{TWEAKCC_VERSION} (one time)...")
    TWEAKCC.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["npm", "install", "--no-save", "--silent", "--prefix", str(TWEAKCC),
         f"tweakcc@{TWEAKCC_VERSION}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not exe.exists():
        raise PatchError(f"tweakcc install failed: {r.stderr.strip()[:300]}")
    return exe


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

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > self.stale_after:
                    self.path.unlink(missing_ok=True)
                    continue
                raise PatchError("another patch run is in progress")
        raise PatchError("could not take the lock")

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)
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
        recorded = (reg.get("stock") or {}).get("path")
        if recorded and Path(recorded).exists():
            return Path(recorded)
        raise PatchError(
            "the stock backup is missing and the binary already carries "
            "patches. Reinstall Claude Code, then patch again."
        )

    # Not our build, so this IS stock: a fresh install, or what a Claude Code
    # update just wrote. Name the backup by content, not by version alone: a
    # release that reuses a version number but ships different bytes would
    # otherwise be rebuilt from a stale backup, silently undoing the update.
    version = _version_of(binary)
    digest = _sha256(binary)[:12]
    backup = BACKUPS / f"claude-{version}-{digest}.orig"

    if not backup.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        log(f"  saving stock binary ({version})")
        shutil.copy2(binary, backup)
    reg["stock"] = {"version": version, "sha256": digest, "path": str(backup)}
    _prune_backups()
    return backup


def rebuild(log=print, skip_dirs=None, require_approved: bool = False) -> bool:
    """Rebuild the binary as stock + every enabled patch. The only mutator.

    require_approved is set when this runs by itself, from the launcher. Then
    it will re-apply code the user already agreed to, but it will not apply
    code that changed since. Repairing after a Claude Code update is safe and
    automatic; running something new is a decision, and decisions belong to the
    person, not to a background task.
    """
    binary = find_binary(skip_dirs)

    with Lock():
        reg = read_registry()
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
        staged = WORK / "claude.staged"
        js_path = WORK / "cli.js"

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
            js_path.unlink(missing_ok=True)

        staged.chmod(0o755)
        log("verifying...")
        r = subprocess.run([str(staged), "--version"], capture_output=True,
                           text=True, timeout=120)
        if r.returncode != 0 or "Claude Code" not in r.stdout:
            raise PatchError(f"rebuilt binary failed to run: {(r.stderr or r.stdout)[:200]}")

        links = _hardlinks_of(binary)
        os.replace(staged, binary)
        binary.chmod(0o755)
        for link in links:
            try:
                link.unlink()
                os.link(binary, link)
            except OSError as e:
                log(f"  note: could not restore hardlink {link}: {e}")

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
        (STATE / "target").write_text(str(binary))
        (STATE / "fast.stamp").write_text(reg["built"]["signature"])
        (STATE / "watch").write_text(
            "\n".join(str(d) for d in watched_definitions(reg)))
        log(f"claude code {reg['built']['version']}: "
            + (", ".join(p.name for p in patches) if patches else "stock"))
        return True


def _with_rollback(before: dict, log) -> bool:
    """Rebuild, and put the registry back if it fails.

    The registry is a claim about what the binary contains. A failed rebuild
    leaves the binary untouched, so a registry still claiming the change would
    be a lie: `status` would report ON for something absent, and every later
    heal would retry the same doomed build.
    """
    try:
        return rebuild(log)
    except Exception:
        write_registry(before)
        raise


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
    write_registry(reg)
    return _with_rollback(before, log)


def disable(patch: Patch, log=print) -> bool:
    before = read_registry()
    if patch.name not in before.get("patches", {}):
        log(f"{patch.name} is not enabled")
        return False
    reg = json.loads(json.dumps(before))
    del reg["patches"][patch.name]
    write_registry(reg)
    return _with_rollback(before, log)


def self_heal(log=lambda s: None) -> None:
    """Called by the launcher. Must never stop Claude Code starting."""
    deadline = time.time() + 45
    while True:
        try:
            if is_current():
                return
            if not read_registry().get("patches"):
                return
            rebuild(log=log, require_approved=True)
            return
        except PatchError as e:
            if "in progress" in str(e) and time.time() < deadline:
                time.sleep(1.5)
                continue
            log(f"claude-patch: leaving Claude Code as it is ({e})")
            return
        except Exception as e:  # noqa: BLE001 - failing open is the point
            log(f"claude-patch: leaving Claude Code as it is ({e})")
            return


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
            self_heal(log=print)
            return 0

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
            reg = read_registry()
            check("stock backup saved", bool(reg.get("stock")),
                  reg.get("stock", {}).get("version", "") if reg.get("stock") else "")
            return 0 if ok else 1

        print(usage)
        return 2

    except PatchError as e:
        print(f"{patch.name}: {e}", file=sys.stderr)
        print("Claude Code is unchanged.", file=sys.stderr)
        return 1
