#!/usr/bin/env python3
"""One line for the command, one line for its output, out of the installed
program rather than off a screen.

The obvious way to check this is to drive a session at sixty columns and read
the terminal. That was tried first and the frames came back torn: a narrow
redraw leaves characters from the previous frame in place, so half the checks
measured leftovers and reported failures against a build that was drawing the
right thing. The one line that did survive intact was the fold, "… +12 lines
(ctrl+o to expand)", which is the part a screen can show honestly.

So this asks the program instead. It unpacks the Claude Code executable that
is actually installed, lifts out the three functions the patch changed, and
runs them on real input at a real width. What it proves is narrower than a
screenshot and much harder to fool: these are the shipped functions, not a
copy, and the answer is a string rather than a picture of one.

    python3 harness/test_one_line.py [--binary <path>] [--columns 60]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

CANDIDATES = [
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
    os.path.expanduser(
        "~/.claude-patch/target/claude.exe"),
]
TWEAKCC = os.path.expanduser(
    "~/.claude-patch/tweakcc/node_modules/.bin/tweakcc")

failures = []


def check(ok, what, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + what
          + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(what)


def find_binary():
    env = os.environ.get("CLAUDE_QUIET_TEST_BINARY")
    if env:
        return env
    for path in CANDIDATES:
        if os.path.isfile(path):
            return path
    raise SystemExit("no Claude Code executable found; pass --binary")


def unpack(binary, into):
    if not os.path.isfile(TWEAKCC):
        raise SystemExit(
            "the unpack helper is missing. Install claude-quiet once, which "
            f"puts it at {TWEAKCC}")
    out = os.path.join(into, "cli.js")
    r = subprocess.run([TWEAKCC, "unpack", out, binary],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit((r.stderr or r.stdout).strip()[:400])
    with open(out, encoding="utf-8") as fh:
        return fh.read()


def lift(name, js):
    """Cut one function out of the bundle, braces balanced.

    The body does not begin at the first `{`. One of these functions takes a
    destructured options argument, so the first brace is in the parameter
    list; balancing from there returns the parameter list and nothing else,
    which node then reports as a syntax error thirty lines later. Walk the
    parentheses first, then take the brace after them.
    """
    start = js.find(f"function {name}(")
    if start < 0:
        raise SystemExit(f"{name} is not in this build")
    depth = 0
    body = None
    for i in range(js.index("(", start), len(js)):
        if js[i] == "(":
            depth += 1
        elif js[i] == ")":
            depth -= 1
            if depth == 0:
                body = js.index("{", i)
                break
    if body is None:
        raise SystemExit(f"{name} has an unclosed parameter list")
    depth = 0
    for i in range(body, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
    raise SystemExit(f"{name} has unbalanced braces")


def stubs(bodies):
    """Stand-ins for the helpers these functions call, named from their own
    source rather than from a release. Every one of these names is minified
    and every one of them changed between 2.1.223 and 2.1.224."""
    count, wrap, fold, header, hint = bodies
    out = []

    def pick(pattern, text, what):
        m = re.search(pattern, text)
        if not m:
            raise SystemExit(f"could not find the {what} this build uses")
        return m.group(1)

    # "… +3 lines": the pluraliser, in the only call it makes
    plural = pick(r"\$\{([\w$]+)\([\w$]+,[\w$]+\)\}", count, "pluraliser")
    out.append('function %s(n,u){return n===1?u:u+"s"}' % plural)

    # the fold's dim() wrapper. The hint itself is NOT stubbed: its wording is
    # one of the things being shortened, and a stub would have cheerfully
    # reported the old wording as a pass. It is lifted in by the caller, with
    # only its keybinding lookup stood in for.
    out.append("var %s={dim:(s)=>s};"
               % pick(r"([\w$]+)\.dim\(", fold, "dim helper"))
    out.append('function %s(){return "ctrl+o"}'
               % pick(r'=([\w$]+)\("app:toggleTranscript"', hint,
                      "keybinding lookup"))

    # the wrapper: a display-width function and a slice
    out.append("function %s(s){return s.length}"
               % pick(r"let [\w$]+=([\w$]+)\([\w$]+\);if\(", wrap,
                      "width function"))
    out.append("function %s(s,a,b){return s.slice(a,b)}"
               % pick(r"=([\w$]+)\([\w$]+,[\w$]+,[\w$]+\+", wrap,
                      "slice helper"))

    # the header: "is this really a file edit" and "are we in plan mode"
    out.append("function %s(){return null}"
               % pick(r"let [\w$]+=([\w$]+)\([\w$]+\);if\([\w$]+\)return",
                      header, "file-edit test"))
    out.append("function %s(){return false}"
               % pick(r"if\(([\w$]+)\(\)\)\{let", header, "plan-mode test"))

    jsx = re.search(r"return ([\w$]+)\.jsxs\(([\w$]+),\{children:", header)
    if not jsx:
        raise SystemExit("could not find the header's renderer call")
    out.append("var %s={jsxs:(_,p)=>({text:p.children.join('')})},%s={};"
               % (jsx.group(1), jsx.group(2)))
    return out


def names(js):
    """The minified names of the three functions, found the way the patch
    finds them: by the shape around them, never hardcoded."""
    header = re.search(
        r"function ([\w$]+)\([\w$]+,\{verbose:[\w$]+,theme:[\w$]+\}\)\{"
        r"let\{command:[\w$]+\}=", js)
    fold = re.search(
        r'function ([\w$]+)\(([\w$]+),[\w$]+,[\w$]+=!1\)\{'
        r'let ([\w$]+)=\2\.trimEnd\(\);if\(!\3\)return"";', js)
    wrap = re.search(r"function ([\w$]+)\([\w$]+,[\w$]+\)\{"
                     r"let [\w$]+=[\w$]+\.split\(`\n`\),[\w$]+=\[\];", js)
    count = re.search(r'function ([\w$]+)\([\w$]+,[\w$]+="line"\)\{', js)
    hint = re.search(r'function ([\w$]+)\(\)\{let [\w$]+='
                     r'[\w$]+\("app:toggleTranscript"', js)
    missing = [n for n, m in (("bash header", header), ("fold", fold),
                              ("wrap", wrap), ("count", count),
                              ("expand hint", hint)) if not m]
    if missing:
        raise SystemExit("could not find: " + ", ".join(missing))
    return (header.group(1), fold.group(1), wrap.group(1), count.group(1),
            hint.group(1))


def run(js, columns, command, output):
    header, fold, wrap, count, hint = names(js)
    bodies = [lift(n, js) for n in (count, wrap, fold, header, hint)]
    # the two module constants the lifted functions read
    consts = re.search(r"var ([\w$]+)=(Math\.max\(0,parseInt\(process\.env\."
                       r"CLAUDE_QUIET_LINES,10\)\|\|0\)|3),([\w$]+)=10;", js)
    if not consts:
        raise SystemExit("could not find the fold constants")
    script = "\n".join([
        f"var {consts.group(1)}={consts.group(2)},{consts.group(3)}=10;",
        *stubs(bodies),
        *bodies,
        f"process.stdout.columns={columns};",
        f"let cmd={json.dumps(command)},out={json.dumps(output)};",
        f"let h={header}({{command:cmd}},{{verbose:false,theme:'dark'}});",
        f"let o={fold}(out,{columns});",
        'console.log(JSON.stringify({'
        'header: typeof h==="string"?h:h.text, folded:o}));',
    ])
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is required")
    r = subprocess.run([node, "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(r.stderr.strip()[:800])
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=None)
    ap.add_argument("--columns", type=int, default=60)
    args = ap.parse_args()

    binary = args.binary or find_binary()
    print(f"binary   {binary}")
    print(f"columns  {args.columns}  (roughly a split pane)")
    print()

    command = ("cd /private/tmp/claude-501/-Users-hamzadebbarh/0008a7dc-b569"
               "-4f99-b802-0b18058c9419/scratchpad && S=shots; playwright-cli"
               " -s=arkx screenshot --filename=$PWD/$S/01-front-door.png")
    output = "\n".join(["slides captured", "01-admin-attendance.png",
                        "01-front-door.png"]
                       + [f"0{i}-slide.png" for i in range(2, 23)])

    with tempfile.TemporaryDirectory() as td:
        js = unpack(binary, td)
        got = run(js, args.columns, command, output)

    print("the command header:")
    print("   |" + got["header"])
    print("the output:")
    for line in got["folded"].splitlines():
        print("   |" + line)
    print()

    header_lines = got["header"].split("\n")
    check(len(header_lines) == 1, "the command is one line",
          f"{len(header_lines)} line(s)")
    check(len(got["header"]) <= args.columns,
          "and it fits the terminal without wrapping",
          f"{len(got['header'])} of {args.columns} columns")
    check(got["header"].endswith("…"), "it says it was shortened")
    check(got["header"].startswith("cd /private/tmp"),
          "the start of the command is still readable")

    folded = got["folded"].splitlines()
    check(len(folded) == 1, "the output is one line",
          f"{len(folded)} line(s) for {len(output.splitlines())} of output")
    check(bool(re.search(r"\+24 lines", got["folded"])),
          "and it says how much is behind it")
    check("(ctrl+o)" in got["folded"], "and how to see it, in three words",
          repr(got["folded"]))
    check("to expand" not in got["folded"],
          "without spelling out what the key does")
    check(not got["folded"].startswith("…"),
          "and without a lone ellipsis in front of it")

    short = run(js, args.columns, "ls", "3 files")
    check(short["folded"] == "3 files",
          "output that already fits one line is left alone",
          repr(short["folded"]))
    check(short["header"] == "ls", "so is a short command",
          repr(short["header"]))

    print()
    print(f"{len(failures)} failed" if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
