"""
claude-quiet: one line per tool call, instead of a screenful.

Edit a 400 line file and Claude Code prints the diff, every hunk, no cap. Run a
long shell command and it prints the command over four wrapped lines and then
three lines of its output. Your scrollback is gone and whatever it said before
went with it.

Every one of those is one line here, and ctrl+o still expands any of them.

    Updated src/app.ts with 47 additions and 12 removals (ctrl+o)
    Bash(cd ~/project && npm run build -- --verbose --no-cache…)
      +24 lines (ctrl+o)

WHAT IT CHANGES

**File edits.** The renderer takes a `collapsed` flag, worked out as
`(not a plan file) and (file is in one internal directory)`. Three places
decide it, all three are made unconditional, so the summary Claude Code
already knows how to draw is used for every file rather than only theirs.

**The Bash command line.** It is already truncated, to two lines and 160
characters, on the reasonable assumption of a wide terminal. In a split pane
160 characters is four wrapped lines. The two caps become one line and the
width of your actual terminal, so the header is one line at any size.

**Tool output.** Claude Code shows the first three lines of any tool result and
folds the rest behind "… +N lines (ctrl+o to expand)". That cap becomes zero,
so anything longer than a single line is the fold line on its own. Output that
already fits one line is still printed in full, because the fold has always had
a rule that it will not hide exactly one line behind a message the same size.

**The rest of the block.** Two things sit under a shell command that do not
earn their line. The fold line is trimmed to "+24 lines (ctrl+o)", and the
"Shell cwd was reset to ..." notice, which is the same sentence every time and
names a directory you are already in, stops getting a row of its own. It is
still stripped out of the output rather than left in the middle of it, and
Claude is still told about it, because that part is not the transcript.

So a shell command that used to draw three lines draws one: what ran. Output
that fits on a line is still drawn, three columns further left than it was,
because the corner marking a result row had two spaces in front of it that
pushed output further right than the command it belongs to. Output that does
not fit is not summarised either, on the reasoning that twenty commands should
not cost forty lines to say nothing. Raising CLAUDE_QUIET_LINES brings the
output and its count back together.

Nothing here deletes anything. Every collapsed thing is one ctrl+o away, and
/focus, which hides tool output completely, is still a different feature.
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
    internal directory. Drop that half of the condition, keep the rest.

    The half being dropped is captured, not spelled. It was one predicate call
    until 2.1.224 made it two, and an anchor that described the shape of the
    old one stopped matching. What this edit needs to know is where the
    condition is, never what it says.
    """
    return "{pre}!0){{let ".format(pre=m.group("pre"))


def _one_line_command(m):
    """Draw the Bash command on one line, whatever the terminal is.

    It is already truncated. The renderer keeps the first two lines of the
    command and the first 160 characters of that, which is one line on a wide
    terminal and the reason this looked deliberate for so long. In a split pane
    at sixty columns, 160 characters is four wrapped lines carrying a path you
    have already read, and the four lines cost more than the tail of the
    command was worth.

    So the two caps are replaced rather than removed: one line instead of two,
    and the width of the terminal you are actually looking at instead of a
    number chosen for a wide one. Fourteen columns are left for the tool name,
    the bullet and the brackets around the command.

    They are replaced by shadowing, not by editing the constants. Both are
    module level and shared, and a local of the same name inside this one
    function reaches every use in it and nothing outside. The rest of the
    function is carried through from the match exactly as it was found, which
    is the rule the 2.1.223 breakage bought: an edit writes only the part it
    changes and copies the rest verbatim.
    """
    return (
        "function {fn}({e},{{verbose:{t},theme:{r}}}){{"
        "let{{command:{n}}}={e};if(!{n})return null;"
        'let {two}=1,{cap}=process.env.CLAUDE_QUIET_BASH==="off"?160'
        ":Math.max(24,(process.stdout.columns||80)-14);"
        "{rest}"
        "let {s}={i}.length>{two},{a}={n}.length>{cap};"
    ).format(**m.groupdict())


def _fold_everything(m):
    """Fold tool output at one line instead of three.

    Claude Code prints the first three lines of a tool result and folds the
    rest behind "… +N lines (ctrl+o to expand)". Three lines is a strange
    number to stop at: rarely the whole answer, always enough to push the last
    thing you read off the top.

    Zero is the honest version of the same idea. The fold line says how much is
    there and ctrl+o still shows all of it, so nothing is lost and the shape of
    a session stops depending on how chatty a command was.

    One line of output is still printed whole. That is not a special case added
    here, it is the fold's own rule: it refuses to hide exactly one line behind
    a message that is itself one line, and at a cap of zero that rule is what
    keeps every short answer visible.

    CLAUDE_QUIET_LINES sets it to any number. 3 is what Claude Code ships.
    """
    return (
        "return!1}}var {fold}=Math.max(0,"
        "parseInt(process.env.CLAUDE_QUIET_LINES,10)||0),{gut}=10;"
    ).format(fold=m.group("fold"), gut=m.group("gut"))


def _no_cwd_notice(m):
    """Stop drawing "Shell cwd was reset to ..." as a row of its own.

    Any command that changes directory ends with this notice, and Claude Code
    gives it a line under the result. It is the same line every time, it names
    a directory you are already in, and in a session that runs a lot of
    commands it is a third of everything on screen.

    This is the whole of the function that produces it: it finds the notice,
    strips it out of the output, and hands it back separately to be drawn.
    Everything except the handing back is kept, so the notice is still removed
    from the output rather than left in the middle of it, and the caller that
    draws the row now gets nothing to draw.

    Claude still sees it. This is the transcript on your screen, not what is
    sent, so a command that reset the directory is still reported where that
    matters.
    """
    return (
        "function {fn}({e}){{return{{"
        'cleanedStderr:{e}.replace({re},"").trim(),cwdResetWarning:null}}}}'
    ).format(fn=m.group("fn"), e=m.group("e"), re=m.group("re"))


def _short_count(m):
    """"+6 lines" rather than "... +6 lines".

    The ellipsis says the text continues, which reads well under three lines
    of output that stop mid sentence. With the fold at one line there is
    nothing above it to continue from, so it is a piece of punctuation
    floating on its own at the start of the line.
    """
    return (
        'function {fn}({e},{t}="line"){{if({e}<=0)return"";'
        "return`+${{{e}}} ${{{pl}({e},{t})}}`}}"
    ).format(fn=m.group("fn"), e=m.group("e"), t=m.group("t"),
             pl=m.group("pl"))


def _short_hint(m):
    """"(ctrl+o)" rather than "(ctrl+o to expand)".

    Eleven characters of instruction on every folded row. The key is the part
    worth knowing, and it sits in brackets after a line count, which is enough
    for anyone to guess what pressing it does.

    The key name is read rather than written, so someone who has rebound the
    transcript key still sees their own key.
    """
    return (
        'function {fn}(){{let {e}={o1}("app:toggleTranscript","Global",'
        '"ctrl+o");return {kt}.dim(`(${{{e}}})`)}}'
    ).format(fn=m.group("fn"), e=m.group("e"), o1=m.group("o1"),
             kt=m.group("kt"))


def _no_fold_line(m):
    """When nothing is above the fold, draw nothing at all.

    At a cap of zero every multi-line result was one row saying how many lines
    it was hiding. That is honest, and it is still a row: a session of twenty
    commands is twenty commands and twenty notices about them. Mounssif chose
    to drop the notice rather than keep a line for it.

    The rule is the cap, not a new setting. A fold line is only dropped when
    there is nothing above it to caption, so raising CLAUDE_QUIET_LINES brings
    both the output and the count straight back: at 1 you get the first line
    and "+23 lines (ctrl+o)" under it, exactly as before.

    Nothing is lost. ctrl+o still expands the whole thing, and Claude reads
    the output in full either way, because this is the transcript on your
    screen and not what was sent.

    The cost, stated plainly: a tool result that is several lines of error
    text now shows nothing on screen until you expand it. Errors that Claude
    Code marks as errors go through a different renderer and are unaffected,
    but a command that fails quietly into stdout is quiet here too. Set
    CLAUDE_QUIET_LINES=1 if that trade is wrong for you.
    """
    return (
        'if(!{l})return"";'
        'return[{l},{u}>0?{dim}.dim({cnt}({u})+({r}?"":` ${{{hint}()}}`))'
        ':""].filter(Boolean).join(`\n`)}}'
    ).format(l=m.group("l"), u=m.group("u"), dim=m.group("dim"),
             cnt=m.group("cnt"), r=m.group("r"), hint=m.group("hint"))


def _no_empty_row(m):
    """A row with nothing in it should not be a row.

    Without this the fold above leaves the corner and its indent drawn against
    an empty line, which is the same line count and less information. The
    early return sits after every hook this component calls, so the rules
    about calling hooks unconditionally still hold.
    """
    return (
        "let {t}={z};if(!{t})return null;"
        'let {g}={err}?"error":{warn}?"warning":void 0,'
    ).format(t=m.group("t"), z=m.group("z"), g=m.group("g"),
             err=m.group("err"), warn=m.group("warn"))


def _tight_result_indent(m):
    """Pull the result row back to where the command starts.

    The prefix is five columns: two spaces, the corner, a space and a
    non-breaking space. The two leading spaces push the row past the bullet
    the command is drawn with, so the output of a command starts three columns
    further right than the command itself, on a line that already lost
    characters to being narrow.

    Two columns is enough: the corner still marks the row as belonging to the
    line above, and the text under it now lines up with the text above it.
    """
    return '{pre}["\\u23BF "]'.format(pre=m.group("pre"))


def _keep_the_short_path(m):
    """Stop a cap of zero from folding output that is already one line.

    Found by using it rather than by reading it. With the cap at zero every
    result folded, including a two word answer, so the screen filled with
    "… +1 line (ctrl+o to expand)" where the line itself would have been
    shorter than the notice.

    The cause is a second use of the same number. Before wrapping anything the
    renderer slices the text to `cap * width * 4` characters, a cheap guard
    against measuring a megabyte of output. At a cap of zero that slice takes
    nothing, the fast path is skipped, and the fold's own rule about not hiding
    exactly one line never gets to run.

    So the guard gets a floor of one cap's worth. It changes nothing at any
    other setting, because it only raises a limit that exists to be generous.
    """
    return (
        "function {fn}({e},{t},{r}=!1){{"
        'let {n}={e}.trimEnd();if(!{n})return"";'
        "let {o}=Math.max({t}-{gut},10),"
        "{i}=Math.max({fold},1)*{o}*4,"
    ).format(**m.groupdict())


PATCH = Patch(
    name="claude-quiet",
    summary="one line per tool call instead of a screenful",
    version="2.4.0",
    marker="collapsed:!0/*cq*/",
    migrate=_migrate_from_v1,
    usage="""
Nothing to learn. Edits, writes, shell commands and tool output are one line:

    Updated src/app.ts with 47 additions and 12 removals (ctrl+o to expand)
    Bash(cd ~/project && npm run build -- --verbose --no-cache…)
      … +24 lines (ctrl+o to expand)

ctrl+o still expands any of them. Output that already fits on one line is
printed in full. The "Shell cwd was reset to ..." notice no longer gets a line
of its own, and the fold line is trimmed to "+24 lines (ctrl+o)". /focus hides
tool output completely, which is a different thing; this keeps everything and
only folds it.

    export CLAUDE_QUIET_LINES=3    show three lines of output before folding,
                                   which is what Claude Code ships with
    export CLAUDE_QUIET_BASH=off   leave the shell command header alone
""",
    edits=[
        Edit(
            "collapse edit diffs",
            # Anchored on shape: the flag, a negated local, and a one argument
            # predicate. Never on the minified names, which change per release.
            #
            # Names are spelled [\w$], never \w. `$` is legal in a JavaScript
            # identifier and minifiers use it: a Claude Code release renamed a
            # helper in the sibling patch to `s$t`, three \w anchors stopped
            # matching, and the update left that patch unapplied with nothing
            # about the code it described having changed.
            # The test itself is matched loosely and thrown away, because it is
            # not what this edit is about. It was one predicate call until
            # 2.1.224 made it two, `p(e)` became `(p(e)||q(e))`, and an anchor
            # that spelled the old shape found nothing on a release that
            # changed nothing about the renderer.
            re.compile(r"collapsed:![\w$]+&&[^,}]{1,80}"),
            _always_collapsed,
            count=2,
        ),
        Edit(
            "collapse the write summary",
            # Anchored on the line above it, "Wrote N lines to <path>", because
            # `else if(!x&&<anything>)` on its own matches eight places.
            re.compile(
                r"(?P<pre>\.relative\([\w$]+\(\),[\w$]+\)\}\)\]\}\)\}"
                r"else if\(!(?P<neg>[\w$]+)&&)[^;{}]{1,80}\)\{let "
            ),
            _write_summary,
            count=1,
        ),
        Edit(
            "one line for the shell command",
            re.compile(
                r"function (?P<fn>[\w$]+)\((?P<e>[\w$]+),"
                r"\{verbose:(?P<t>[\w$]+),theme:(?P<r>[\w$]+)\}\)\{"
                r"let\{command:(?P<n>[\w$]+)\}=(?P=e);if\(!(?P=n)\)return null;"
                r"(?P<rest>.*?)"
                r"let (?P<s>[\w$]+)=(?P<i>[\w$]+)\.length>(?P<two>[\w$]+),"
                r"(?P<a>[\w$]+)=(?P=n)\.length>(?P<cap>[\w$]+);",
                re.S,
            ),
            _one_line_command,
            count=1,
        ),
        Edit(
            "fold tool output at one line",
            re.compile(r"return!1\}var (?P<fold>[\w$]+)=3,(?P<gut>[\w$]+)=10;"),
            _fold_everything,
            count=1,
        ),
        Edit(
            "keep one-line output out of the fold",
            re.compile(
                r"function (?P<fn>[\w$]+)\((?P<e>[\w$]+),(?P<t>[\w$]+),"
                r"(?P<r>[\w$]+)=!1\)\{"
                r'let (?P<n>[\w$]+)=(?P=e)\.trimEnd\(\);if\(!(?P=n)\)return"";'
                r"let (?P<o>[\w$]+)=Math\.max\((?P=t)-(?P<gut>[\w$]+),10\),"
                r"(?P<i>[\w$]+)=(?P<fold>[\w$]+)\*(?P=o)\*4,"
            ),
            _keep_the_short_path,
            count=1,
        ),
        Edit(
            "never give the cwd notice a line of its own",
            re.compile(
                r"function (?P<fn>[\w$]+)\((?P<e>[\w$]+)\)\{"
                r"let (?P<t>[\w$]+)=(?P=e)\.match\((?P<re>[\w$]+)\);"
                r"if\(!(?P=t)\)return\{cleanedStderr:(?P=e),"
                r"cwdResetWarning:null\};"
                r"let (?P<r>[\w$]+)=(?P=t)\[1\]\?\?null;"
                r'return\{cleanedStderr:(?P=e)\.replace\((?P=re),""\)\.trim\(\),'
                r"cwdResetWarning:(?P=r)\}\}"
            ),
            _no_cwd_notice,
            count=1,
        ),
        Edit(
            "drop the ellipsis from the fold line",
            re.compile(
                r"function (?P<fn>[\w$]+)\((?P<e>[\w$]+),(?P<t>[\w$]+)="
                r'"line"\)\{if\((?P=e)<=0\)return"";'
                r"return`\\u2026 \+\$\{(?P=e)\} "
                r"\$\{(?P<pl>[\w$]+)\((?P=e),(?P=t)\)\}`\}"
            ),
            _short_count,
            count=1,
        ),
        Edit(
            "no fold line when there is nothing above it",
            re.compile(
                r'return\[(?P<l>[\w$]+),(?P<u>[\w$]+)>0\?(?P<dim>[\w$]+)'
                r'\.dim\((?P<cnt>[\w$]+)\((?P=u)\)\+\((?P<r>[\w$]+)\?"":'
                r'` \$\{(?P<hint>[\w$]+)\(\)\}`\)\):""\]'
                r'\.filter\(Boolean\)\.join\(`\n`\)\}'
            ),
            _no_fold_line,
            count=1,
        ),
        Edit(
            "no row for an empty result",
            re.compile(
                r'let (?P<t>[\w$]+)=(?P<z>[\w$]+),(?P<g>[\w$]+)='
                r'(?P<err>[\w$]+)\?"error":(?P<warn>[\w$]+)\?"warning":void 0,'
            ),
            _no_empty_row,
            count=1,
        ),
        Edit(
            "pull the result row back three columns",
            re.compile(
                r'(?P<pre>"aria-hidden":[\w$]+,"aria-label":[\w$]+,'
                r'dimColor:!0,children:)\["  ","\\u23BF \\xA0"\]'
            ),
            _tight_result_indent,
            count=1,
        ),
        Edit(
            "shorten the expand hint to the key",
            re.compile(
                r"function (?P<fn>[\w$]+)\(\)\{let (?P<e>[\w$]+)="
                r'(?P<o1>[\w$]+)\("app:toggleTranscript","Global","ctrl\+o"\);'
                r"return (?P<kt>[\w$]+)\.dim\(`\(\$\{(?P=e)\} to expand\)`\)\}"
            ),
            _short_hint,
            count=1,
        ),
    ],
)


if __name__ == "__main__":
    sys.exit(main(PATCH, sys.argv))
