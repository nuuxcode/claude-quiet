"""What the edits do, checked against the shapes they anchor on.

The fixture is written here rather than lifted out of Claude Code: it is the
smallest thing that carries each anchor's shape, so an anchor that stops
matching fails here instead of on someone's machine after an update.

Behaviour that needs the real renderer is proved by driving a real session in
harness/, not from this file.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import patch_def  # noqa: E402


# One line per anchor, in the order the edits expect to find them. Names are
# deliberately short and ugly, the way a minifier leaves them, and one of them
# carries a `$` because that is the character these anchors exist to survive.
FIXTURE = "\n".join([
    # the two edit-diff renderers
    'X.jsx(D,{collapsed:!a&&isPlan(e),patch:p});',
    'Y.jsx(D,{collapsed:!b&&isPlan(e),patch:q});',
    # the write summary
    'X.jsx(y,{bold:!0,children:P.relative(cwd(),e)})]})}else if(!n&&(isPlan(f)||isDraft(f))){let B=2}',
    # the Bash command header
    'function b$h(e,{verbose:t,theme:r}){let{command:n}=e;if(!n)return null;'
    'let o=asEdit(n);if(o)return t?o.filePath:short(o.filePath);'
    'if(!t){let i=n.split(`\\n`);'
    'let s=i.length>LINES,a=n.length>WIDE;'
    'if(s||a){let l=n;if(s)l=i.slice(0,LINES).join(`\\n`);'
    'if(l.length>WIDE)l=l.slice(0,WIDE);'
    'return J.jsxs(T,{children:[l.trim(),"\\u2026"]})}}return n}',
    # the fold cap, and the byte guard that reads it
    'function isFolded(e){return!1}var LINES=3,GUT=10;',
    'function fold(e,t,r=!1){let n=e.trimEnd();if(!n)return"";'
    'let o=Math.max(t-GUT,10),i=LINES*o*4,s=n.length>i,a=s?n.slice(0,i):n;}',
])


def applied():
    return patch_def.PATCH.apply(FIXTURE)


class AnchorTests(unittest.TestCase):
    def test_every_edit_finds_its_shape_exactly_as_often_as_it_expects(self):
        applied()  # apply() is the assertion: it refuses on a count mismatch

    def test_the_patch_changed_something(self):
        self.assertNotEqual(applied(), FIXTURE)

    def test_no_anchor_spells_a_name_with_backslash_w(self):
        r"""`$` is legal in a JavaScript identifier and minifiers use it.

        A Claude Code release renamed a helper in the sibling patch to `s$t`,
        three `\w` anchors stopped matching, and the update left that patch
        unapplied with nothing about the code they described having changed.
        """
        offenders = [e.name for e in patch_def.PATCH.edits
                     if re.search(r"\\w(?!\$)", e.anchor.pattern)]
        self.assertEqual(
            offenders, [],
            "these anchors cannot match a $ in a minified name: "
            + ", ".join(offenders))


class ToleranceTests(unittest.TestCase):
    """2.1.224 turned one predicate into two and both original edits stopped
    matching, on a release that changed nothing about either renderer.

    An anchor should describe where a thing is, not what it says. These are
    the two conditions that were caught being spelled out, checked in the
    shape they were written for and in the shape that broke them.
    """

    CASES = [
        ("collapse edit diffs",
         "R.jsx(D,{collapsed:!a&&isPlan(e),patch:p});",
         "R.jsx(D,{collapsed:!a&&(isPlan(e)||isDraft(e)),patch:p});"),
        ("collapse the write summary",
         "X.jsx(y,{bold:!0,children:P.relative(cwd(),e)})]})}"
         "else if(!n&&isPlan(f)){let B=2}",
         "X.jsx(y,{bold:!0,children:P.relative(cwd(),e)})]})}"
         "else if(!n&&(isPlan(f)||isDraft(f))){let B=2}"),
    ]

    def test_both_conditions_are_found_however_they_are_written(self):
        by_name = {e.name: e for e in patch_def.PATCH.edits}
        for name, before, after in self.CASES:
            for js in (before, after):
                with self.subTest(edit=name, js=js[-30:]):
                    self.assertIsNotNone(by_name[name].anchor.search(js))

    def test_the_write_summary_is_not_confused_with_other_branches(self):
        """`else if(!x&&<anything>)` on its own matches eight other places."""
        edit = {e.name: e for e in patch_def.PATCH.edits}[
            "collapse the write summary"]
        for other in ('else if(!l&&(u==="cd"||u==="chdir")){let x=1}',
                      "else if(!t&&l&&!a&&s){let y=2}"):
            with self.subTest(other=other[:30]):
                self.assertIsNone(edit.anchor.search(other))


class BashHeaderTests(unittest.TestCase):
    def setUp(self):
        self.out = applied()

    def test_the_line_cap_becomes_one(self):
        self.assertIn("let LINES=1,", self.out)

    def test_the_width_cap_follows_the_terminal(self):
        self.assertIn("(process.stdout.columns||80)-14", self.out)

    def test_the_caps_are_shadowed_not_edited(self):
        """A local of the same name reaches every use in this one function and
        nothing outside it, so the shared constants keep their own values."""
        self.assertIn("var LINES=", self.out)
        self.assertEqual(self.out.count("let LINES=1,"), 1)

    def test_the_rest_of_the_renderer_is_carried_through_untouched(self):
        for kept in ('let o=asEdit(n);if(o)return t?o.filePath:',
                     'if(l.length>WIDE)l=l.slice(0,WIDE);',
                     'return J.jsxs(T,{children:[l.trim(),"\\u2026"]})'):
            with self.subTest(kept=kept[:40]):
                self.assertIn(kept, self.out)

    def test_it_can_be_turned_off_without_uninstalling(self):
        self.assertIn('process.env.CLAUDE_QUIET_BASH==="off"?160', self.out)


class FoldTests(unittest.TestCase):
    def setUp(self):
        self.out = applied()

    def test_the_cap_comes_from_the_environment_and_defaults_to_zero(self):
        self.assertIn(
            "var LINES=Math.max(0,parseInt(process.env.CLAUDE_QUIET_LINES,10)"
            "||0),GUT=10;", self.out)

    def test_the_byte_guard_never_collapses_to_nothing(self):
        """Without the floor, a cap of zero also zeroes the pre-wrap slice, the
        fast path is skipped, and even a two word answer folds."""
        self.assertIn("i=Math.max(LINES,1)*o*4,", self.out)


class CapValueTests(unittest.TestCase):
    """The cap expression is arithmetic in a shipped binary. Run it."""

    def evaluate(self, value):
        expr = re.search(
            r"var LINES=(Math\.max\(0,parseInt\(process\.env\."
            r"CLAUDE_QUIET_LINES,10\)\|\|0\)),", applied()).group(1)
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is unavailable")
        script = f"console.log(JSON.stringify({expr}))"
        env = dict(os.environ)
        env.pop("CLAUDE_QUIET_LINES", None)
        if value is not None:
            env["CLAUDE_QUIET_LINES"] = value
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, check=True, env=env)
        return json.loads(r.stdout)

    def test_unset_folds_at_one_line(self):
        self.assertEqual(self.evaluate(None), 0)

    def test_three_gives_claude_codes_own_behaviour_back(self):
        self.assertEqual(self.evaluate("3"), 3)

    def test_nonsense_is_treated_as_unset_rather_than_crashing(self):
        for value in ("", "off", "-4", "banana"):
            with self.subTest(value=value):
                self.assertEqual(self.evaluate(value), 0)


if __name__ == "__main__":
    unittest.main()
