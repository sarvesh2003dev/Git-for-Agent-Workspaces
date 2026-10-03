"""Exercise the actual CLI boundary, subprocess exit codes, and recovery."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from snapshot_daemon import WorkspaceSnapshotter


ROOT = Path(__file__).resolve().parents[1]


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "project"
        self.workspace.mkdir()
        (self.workspace / "config.txt").write_text("working", encoding="utf-8")

    def cli(self, *args, entry="workspace_cli.py"):
        return subprocess.run(
            [sys.executable, str(ROOT / entry), "--workspace", str(self.workspace), *args],
            text=True, capture_output=True, cwd=ROOT, timeout=30,
        )

    def test_failed_command_restores_exact_state_and_exit_code(self):
        (self.workspace / "empty").mkdir()
        result = self.cli("run", "--", sys.executable, "-c",
                          "from pathlib import Path; "
                          "Path('config.txt').write_text('broken'); "
                          "Path('extra.txt').write_text('new'); "
                          "Path('empty').rmdir(); raise SystemExit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.workspace / "config.txt").read_text(), "working")
        self.assertFalse((self.workspace / "extra.txt").exists())
        self.assertTrue((self.workspace / "empty").is_dir())
        self.assertIn("restored", result.stderr)
        self.assertEqual(len(WorkspaceSnapshotter(str(self.workspace)).list_snapshots()), 1)

    def test_successful_command_keeps_changes(self):
        result = self.cli("run", "--", sys.executable, "-c",
                          "from pathlib import Path; Path('config.txt').write_text('improved')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "config.txt").read_text(), "improved")

    def test_init_creates_missing_workspace_and_preserves_existing(self):
        first = self.cli("init")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("snapshot_id", json.loads(first.stdout))
        self.assertEqual((self.workspace / "config.txt").read_text(), "working")
        self.workspace = Path(self.temp.name) / "new" / "nested"
        second = self.cli("init")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(self.workspace.is_dir())

    def test_manual_snapshot_verify_restore_and_prune(self):
        created = self.cli("snapshot", "--label", "known good")
        self.assertEqual(created.returncode, 0, created.stderr)
        sid = json.loads(created.stdout)["snapshot_id"]
        checked = self.cli("verify", sid)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        (self.workspace / "config.txt").write_text("broken", encoding="utf-8")
        restored = self.cli("restore", sid)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.workspace / "config.txt").read_text(), "working")
        listing = self.cli("list")
        self.assertEqual(json.loads(listing.stdout)[0]["label"], "known good")
        pruned = self.cli("prune", "--keep", "0")
        self.assertEqual(pruned.returncode, 0, pruned.stderr)
        self.assertEqual(json.loads(pruned.stdout)["snapshot_count"], 0)

    def test_missing_checkpoint_is_an_error(self):
        result = self.cli("restore", "does-not-exist")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.workspace / "config.txt").read_text(), "working")

    def test_missing_executable_is_an_error_and_retains_checkpoint(self):
        result = self.cli("run", "--", "definitely-not-a-real-checkpoint-command")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.workspace / "config.txt").read_text(), "working")
        self.assertEqual(len(WorkspaceSnapshotter(str(self.workspace)).list_snapshots()), 1)

    def test_negative_retention_does_not_delete_history(self):
        self.assertEqual(self.cli("snapshot").returncode, 0)
        result = self.cli("prune", "--keep", "-1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(WorkspaceSnapshotter(str(self.workspace)).list_snapshots()), 1)

    def test_legacy_watcher_is_explicit_not_background_monitor(self):
        result = self.cli("run", "--", sys.executable, "-c", "raise SystemExit(3)",
                          entry="watcher.py")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("restored", result.stderr)


if __name__ == "__main__":
    unittest.main()
