"""
claude-quiet: stop Claude Code printing the whole file every time it edits one.

Edit a 400 line file and Claude Code prints the diff, every hunk, no cap. Your
scrollback is gone and whatever it said before the edit went with it.

Claude Code already knows how to show a one line summary. It just reserves it
for files in one internal directory. This makes it the behaviour for your files
too, and ctrl+o still expands any of them.

WHAT IT CHANGES

The renderer for a file edit takes a `collapsed` flag, worked out as:

    collapsed = (not a plan file) and (file is in one internal directory)

Three places decide that. All three are made unconditional, so the summary is
used for every file rather than only theirs. Nothing else about the renderer
changes: the same summary, the same expand key, the same colours.
"""

import mmap
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ccpatch import Edit, Patch, PatchError, main  # noqa: E402

# The first release changed the program a different way, kept its own state in
# ~/.claude-quiet, and installed its own launcher there which it put on your
# PATH. So pulling this repo does not change what actually runs, and installing
# on top would save the OLD version's output as your pristine original and bake
# it in permanently. This undoes it first.
V1_HOME = Path(os.environ.get("CLAUDE_QUIET_HOME", Path.home() / ".claude-quiet"))
V1_MARKER = b"collapsed:!!1"


def _carries_v1_patch(binary: Path) -> bool:
    try:
        with open(binary, "rb") as fh:
            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm.find(V1_MARKER) != -1
    except (OSError, ValueError):
        return False


def _migrate_from_v1(binary: Path, log) -> None:
    """Put the original program back, and retire the old launcher."""
    if not V1_HOME.exists():
        return

    if _carries_v1_patch(binary):
        backups = sorted((V1_HOME / "backups").glob("claude-*.orig"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            raise PatchError(
                "found the previous version of claude-quiet, but its saved copy "
                "of the original program is gone, so there is nothing safe to "
                "build from. Reinstall Claude Code, then run this again."
            )
        log("  upgrading from the previous version of claude-quiet")
        log(f"  restoring the original program from {backups[0]}")
        shutil.copy2(backups[0], binary)
        binary.chmod(0o755)

    # The old launcher sits on your PATH and would keep trying to apply the old
    # change every time Claude Code starts. Renamed rather than deleted, so
    # nothing is destroyed and it is obvious what happened.
    for name in ("claude", "claude-quiet"):
        old = V1_HOME / "bin" / name
        if old.exists():
            retired = old.with_name(name + ".retired")
            if retired.exists():
                retired.unlink()
            old.rename(retired)
            log(f"  retired the old launcher at {old}")


def _always_collapsed(m):
    """Force the diff renderer's collapsed flag on.

    Two call sites, one for each renderer that draws a file edit.
    """
    return "collapsed:!0/*cq*/"


def _write_summary(m):
    """The Write path picks the compact "Wrote N lines" form only for their
    internal directory. Drop that half of the condition, keep the rest."""
    return "else if(!{neg}){{let ".format(neg=m.group("neg"))


PATCH = Patch(
    name="claude-quiet",
    summary="collapse file-edit diffs to one line instead of dumping the file",
    version="2.0.0",
    marker="collapsed:!0/*cq*/",
    migrate=_migrate_from_v1,
    usage="""
Nothing to learn. Edits and writes print one line:

    Updated src/app.ts with 47 additions and 12 removals (ctrl+o to expand)

ctrl+o still expands any of them. /focus hides tool output entirely, which is
a different thing; this keeps everything and only shortens the diff.
""",
    edits=[
        Edit(
            "collapse edit diffs",
            # Anchored on shape: the flag, a negated local, and a one argument
            # predicate. Never on the minified names, which change per release.
            re.compile(r"collapsed:!\w{1,4}&&\w{1,8}\(e\)"),
            _always_collapsed,
            count=2,
        ),
        Edit(
            "collapse the write summary",
            re.compile(r"else if\(!(?P<neg>\w{1,4})&&\w{1,8}\(\w{1,4}\)\)\{let "),
            _write_summary,
            count=1,
        ),
    ],
)


if __name__ == "__main__":
    sys.exit(main(PATCH, sys.argv))
