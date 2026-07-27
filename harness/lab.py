#!/usr/bin/env python3
"""
Drive a Claude Code binary on a real pty and read its rendered screen.

Why not tmux: a tmux server running claude-lab dies within seconds of the first
prompt, reproducibly, while a plain shell in the same tmux survives for
minutes. A direct pty has no such problem, needs nothing installed, and gives
the same thing tmux capture-pane would: the rendered screen as plain text.

pyte replays the ANSI stream into a virtual screen, so what `screen()` returns
is what a human would see, not the raw redraw soup.

Usage as a library:

    lab = Lab()                      # or Lab(binary=..., model=...)
    lab.start()
    lab.send("do the thing")
    lab.wait_for("DONE", timeout=90)
    print(lab.screen())
    lab.stop()
"""

from __future__ import annotations

import codecs
import os
import pty
import re
import select
import shutil
import signal
import time
import uuid

HOME = os.path.expanduser("~")

# Claude Code prints an elapsed counter while it works. Matching only on
# "esc to interrupt" misses it on builds that show the counter instead.
BUSY = re.compile(r"\(\d+s\s*·|esc to interrupt")

# Any tool call, not just Bash: a scenario can open with a Read.
TOOL = re.compile(r"(Bash|Read|Write|Edit|Glob|Grep|Task|Agent)\(")

# The fixture prompt for "keep Claude busy for a known length of time".
#
# Both halves of this were learned by losing runs. Say "count slowly with a
# sleep between each number" and the model sometimes picks sleep 0.1 and
# finishes in nine seconds. Pin the command and it sometimes moves that command
# to the BACKGROUND and finishes in seven. Either way the session is idle when
# the test types into it. A prompt that leaves the model any choice about how
# long it stays busy is not a fixture.
def busy_for(seconds: int) -> str:
    return ("run this exact bash command in the foreground and wait for it to "
            "finish. do not run it in the background, do not change it: "
            f"for i in {{1..{seconds}}}; do echo $i; sleep 1; done")


class SetupFailed(Exception):
    """The run never reached the state under test. NOT a product failure."""

try:
    import pyte
except ImportError:
    pyte = None


class Lab:
    def __init__(self, binary=None, workspace=None, model="haiku", cols=120,
                 rows=44, extra_env=None):
        self.binary = binary or shutil.which("claude")
        if not self.binary:
            raise FileNotFoundError("no claude binary found; pass binary=...")
        # Absolute, always. The child chdirs into the workspace before exec, so
        # a relative path here would resolve against the workspace and fail.
        self.binary = os.path.abspath(self.binary)
        self.workspace = os.path.abspath(
            workspace or os.path.join(os.getcwd(), "workspace"))
        self.model = model
        # Applied AFTER the CLAUDE_CODE* strip in start(), which is the only way
        # to test one of Claude Code's own feature flags. Setting it in the
        # parent environment does not work: the strip deletes it, and a test
        # that believes it enabled a flag it actually deleted reports the
        # unchanged behaviour as the flag's behaviour.
        self.extra_env = dict(extra_env or {})
        self.cols, self.rows = cols, rows
        self.pid = self.fd = None
        self.session_id = str(uuid.uuid4())
        self._history_dirs_before = set()
        self.raw = bytearray()
        self.events = []  # (elapsed_seconds, label)
        self.t0 = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        if pyte:
            self._screen = pyte.Screen(cols, rows)
            self._stream = pyte.Stream(self._screen)
        else:
            self._screen = self._stream = None

    # -- lifecycle ---------------------------------------------------------

    def _child_env(self):
        env = os.environ.copy()
        # A nested Claude Code disables its own transcript when it sees the
        # parent's markers, and inherited limits would skew any timing test.
        for k in list(env):
            if k.startswith("CLAUDE_CODE") or k == "CLAUDECODE":
                del env[k]
        env["TERM"] = "xterm-256color"
        env["COLUMNS"], env["LINES"] = str(self.cols), str(self.rows)
        env.update(self.extra_env)
        return env

    def _argv(self):
        return [self.binary, "--safe-mode", "--dangerously-skip-permissions",
                "--settings", '{"skipDangerousModePermissionPrompt":true}',
                "--session-id", self.session_id, "--model", self.model]

    def _snapshot_history_dirs(self):
        root = os.path.join(HOME, ".claude", "projects")
        if not os.path.isdir(root):
            return set()
        return {path for path, _, _ in os.walk(root)}

    def _cleanup_session_history(self):
        """Remove only this driver's UUID-named transcript and new empty dirs."""
        root = os.path.join(HOME, ".claude", "projects")
        if not os.path.isdir(root):
            return
        # A subagent stores its own transcript under a directory named with the
        # parent session UUID, but the child filenames do not include that UUID.
        # The complete UUID directory is still owned only by this driver.
        for path, dirs, _ in os.walk(root):
            if self.session_id not in dirs:
                continue
            owned = os.path.join(path, self.session_id)
            shutil.rmtree(owned)
            dirs.remove(self.session_id)
        for path, dirs, files in os.walk(root, topdown=False):
            for name in files:
                if self.session_id in name:
                    try:
                        os.unlink(os.path.join(path, name))
                    except OSError:
                        pass
            for name in dirs:
                candidate = os.path.join(path, name)
                if candidate in self._history_dirs_before:
                    continue
                try:
                    os.rmdir(candidate)
                except OSError:
                    pass

    def start(self, settle=7):
        os.makedirs(self.workspace, exist_ok=True)
        self._history_dirs_before = self._snapshot_history_dirs()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(self.workspace)
            os.execve(
                self.binary,
                self._argv(),
                self._child_env(),
            )
        self.t0 = time.time()
        self._pump(settle)
        deadline = time.time() + 45
        while time.time() < deadline:
            screen = self.screen()
            if "Yes, I trust this folder" in screen:
                # The caller chose a scratch workspace and explicitly invoked
                # a bypass-permissions harness. Confirm only this exact prompt.
                self.write(b"\r")
                self._pump(1.5)
                continue
            if "Yes, I accept" not in screen and any(
                    line.lstrip().startswith("❯")
                    for line in screen.splitlines()):
                return self
            if not self.alive():
                break
            self._pump(0.5)
        self.stop()
        raise RuntimeError("Claude Code did not reach a ready prompt")

    def stop(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                time.sleep(0.5)
                os.kill(self.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        if self.fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.pid = self.fd = None
        self._cleanup_session_history()

    def alive(self):
        if not self.pid:
            return False
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            return done == 0
        except ChildProcessError:
            return False

    # -- input -------------------------------------------------------------

    def write(self, data):
        """Raw bytes to the child, looping until all of them land.

        A single os.write can write fewer bytes than it was given, and a long
        paste is exactly the case where that happens. Silently dropping the
        tail produces a truncated line that reads as the program mangling the
        input.
        """
        remaining = memoryview(data)
        while remaining:
            try:
                written = os.write(self.fd, remaining)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("pty write made no progress")
            remaining = remaining[written:]

    def type(self, text):
        self.write(text.encode())
        self._pump(0.4)

    def send(self, text, label=None):
        self.type(text)
        time.sleep(0.5)
        self.write(b"\r")
        self.mark(label or f"SENT: {text[:48]}")
        self._pump(0.6)

    def key(self, name):
        keys = {
            "enter": b"\r", "escape": b"\x1b", "up": b"\x1b[A", "down": b"\x1b[B",
            "tab": b"\t", "ctrl-c": b"\x03", "shift-tab": b"\x1b[Z",
            # CSI 1;2A and 1;2B, what a modern terminal sends for shift
            # with an arrow. Written out rather than composed so a wrong
            # modifier number fails loudly instead of arriving as a plain
            # arrow and quietly passing the test it was meant to fail.
            "shift-up": b"\x1b[1;2A", "shift-down": b"\x1b[1;2B",
        }
        self.write(keys[name.lower()])
        self._pump(0.4)

    # -- output ------------------------------------------------------------

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return False
            if not data:
                return False
            self.raw.extend(data)
            if self._stream:
                # Incremental, because a read can land in the middle of a
                # multi-byte character. Decoding each chunk on its own
                # turns that character into a replacement mark, which is
                # how a solid rule line came back as "-----?-----" and
                # made a screen test miss the box it was looking for.
                self._stream.feed(self._decoder.decode(data))
        return True

    def screen(self):
        """The rendered screen, as a human would see it."""
        if self._screen:
            return "\n".join(l.rstrip() for l in self._lines() if l.strip())
        return self.text()[-3000:]

    def _lines(self):
        """Render pyte safely when a spinner leaves an empty cell behind."""
        try:
            from wcwidth import wcwidth
        except ImportError:
            def wcwidth(_):
                return 1

        out = []
        for y in range(self.rows):
            row = self._screen.buffer[y]
            line, skip = [], False
            for x in range(self.cols):
                if skip:
                    skip = False
                    continue
                ch = row[x].data
                if not ch:
                    line.append(" ")
                    continue
                if wcwidth(ch[0]) == 2:
                    skip = True
                line.append(ch)
            out.append("".join(line))
        return out

    def text(self):
        """The whole output stream with ANSI stripped. Messy but complete."""
        s = self.raw.decode("utf-8", "replace")
        s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]|\r", "", s)
        return s

    def mark(self, label):
        self.events.append((round(time.time() - self.t0, 1), label))

    def wait_for_tool(self, timeout=120):
        """Wait until a tool is actually RUNNING, not until a spinner appears.

        The spinner shows while the model is still thinking, before it has
        called anything. Returning then means whatever you type next can arrive
        at a session that finishes moments later, so a message meant to queue
        runs instead and every assertion afterwards fails on an empty screen.

        Measured cost of getting this wrong: one suite reported 2 of 13 on a
        build that scores 13 of 13.
        """
        end = time.time() + timeout
        while time.time() < end:
            self._pump(0.3)
            screen = self.screen()
            if TOOL.search(screen) and BUSY.search(screen):
                return True
        return False

    def expect(self, predicate, what, timeout=25):
        """Wait for the state under test, and say SETUP rather than FAIL if it
        never arrives.

        This distinction is the difference between a suite you trust and one you
        argue with. A run that never reached the state it was testing has proved
        nothing; reporting that as a failure sends you hunting a bug in code
        that is fine. It happened repeatedly before this existed.
        """
        end = time.time() + timeout
        while time.time() < end:
            self._pump(0.4)
            if predicate(self.screen()):
                return
        raise SetupFailed(f"never reached: {what}")

    def wait_for(self, needle, timeout=120, label=None):
        """Pump until `needle` appears anywhere in the output. Returns seconds, or None."""
        end = time.time() + timeout
        while time.time() < end:
            if not self._pump(0.5):
                break
            if needle in self.text():
                t = round(time.time() - self.t0, 1)
                self.events.append((t, label or f"SAW: {needle}"))
                return t
        return None

    def timeline(self):
        return "\n".join(f"  T+{t:>6.1f}s  {lbl}" for t, lbl in self.events)
