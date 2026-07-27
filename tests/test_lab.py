import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))

import lab as lab_module  # noqa: E402


class LabTests(unittest.TestCase):
    @staticmethod
    def binary():
        binary = shutil.which("true")
        if not binary:
            raise unittest.SkipTest("true binary is unavailable")
        return binary

    def test_lab_disables_customizations_and_names_its_session(self):
        with tempfile.TemporaryDirectory() as td:
            driver = lab_module.Lab(
                binary=self.binary(), workspace=td,
                extra_env={"CLAUDE_CONFIG_DIR": "/should/not/win"},
            )
            with mock.patch.dict(
                    lab_module.os.environ,
                    {"CLAUDE_CODE_PARENT": "parent", "CLAUDECODE": "1",
                     "CLAUDE_CONFIG_DIR": "/real/config"}):
                env = driver._child_env()
            self.assertNotIn("CLAUDE_CODE_PARENT", env)
            self.assertNotIn("CLAUDECODE", env)
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/should/not/win")
            self.assertIn("--safe-mode", driver._argv())
            self.assertIn("--session-id", driver._argv())
            self.assertIn(driver.session_id, driver._argv())
            self.assertIn(
                "skipDangerousModePermissionPrompt", " ".join(driver._argv())
            )

    def test_lab_retries_partial_pty_writes(self):
        driver = lab_module.Lab(binary=self.binary())
        driver.fd = 99
        received = bytearray()

        def short_write(_fd, data):
            chunk = bytes(data[:2])
            received.extend(chunk)
            return len(chunk)

        with mock.patch.object(lab_module.os, "write", side_effect=short_write):
            driver.write(b"abcdef")
        self.assertEqual(received, b"abcdef")

    def test_lab_removes_only_its_named_session_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".claude" / "projects"
            existing = root / "existing"
            existing.mkdir(parents=True)
            keep = existing / "keep.jsonl"
            keep.write_text("keep")
            driver = lab_module.Lab(binary=self.binary())
            driver._history_dirs_before = {str(root), str(existing)}
            created = root / "created"
            created.mkdir()
            (created / f"{driver.session_id}.jsonl").write_text("test")
            owned = existing / driver.session_id / "subagents"
            owned.mkdir(parents=True)
            (owned / "agent-child.jsonl").write_text("test")
            with mock.patch.object(lab_module, "HOME", td):
                driver._cleanup_session_history()
            self.assertTrue(keep.is_file())
            self.assertFalse(created.exists())
            self.assertFalse((existing / driver.session_id).exists())

    def test_busy_fixture_pins_the_foreground_command(self):
        prompt = lab_module.busy_for(17)
        self.assertIn("for i in {1..17}", prompt)
        self.assertIn("foreground", prompt)
        self.assertIn("do not change it", prompt)

    def test_wait_for_tool_rejects_spinner_only_state(self):
        driver = lab_module.Lab(binary=self.binary())
        screens = iter([
            "thinking (1s · esc to interrupt)",
            "Bash(for i in {1..17}) (2s · esc to interrupt)",
        ])
        with mock.patch.object(driver, "_pump"), \
                mock.patch.object(driver, "screen", side_effect=screens):
            self.assertTrue(driver.wait_for_tool(timeout=1))

    def test_empty_terminal_cell_does_not_crash_rendering(self):
        driver = lab_module.Lab(binary=self.binary(), cols=3, rows=1)
        row = {
            0: SimpleNamespace(data="A"),
            1: SimpleNamespace(data=""),
            2: SimpleNamespace(data="B"),
        }
        driver._screen = SimpleNamespace(buffer={0: row})
        self.assertEqual(driver._lines(), ["A B"])


if __name__ == "__main__":
    unittest.main()
