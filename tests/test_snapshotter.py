"""Core checkpoint, retention, metadata, and pre-publication failure contracts."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from snapshot_daemon import SnapshotError, WorkspaceSnapshotter


class SnapshotterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.snap = WorkspaceSnapshotter(str(self.workspace))

    def test_empty_checkpoint_restores_empty_workspace(self):
        sid = self.snap.create_snapshot("empty")
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested/file").write_bytes(b"new")
        self.assertTrue(self.snap.rollback(sid))
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_shared_blobs_survive_until_last_reference_is_removed(self):
        (self.workspace / "a").write_bytes(b"same")
        (self.workspace / "b").write_bytes(b"same")
        first = self.snap.create_snapshot("one")
        second = self.snap.create_snapshot("two")
        self.assertEqual(self.snap.storage_stats()["blob_count"], 1)
        self.snap.delete_snapshot(first)
        self.assertTrue(self.snap.verify_snapshot(second)["valid"])
        self.assertEqual(self.snap.storage_stats()["blob_count"], 1)
        self.snap.delete_snapshot(second)
        self.assertEqual(self.snap.storage_stats()["blob_count"], 0)

    def test_retention_keeps_newest_and_reclaims_unused_contents(self):
        path = self.workspace / "version"
        snapshots = []
        for version in range(4):
            path.write_bytes(bytes([version]))
            snapshots.append(self.snap.create_snapshot(str(version)))
        self.snap.cleanup_old_snapshots(2)
        self.assertEqual([s["id"] for s in self.snap.list_snapshots()], snapshots[-2:])
        self.assertEqual(self.snap.storage_stats()["blob_count"], 2)
        self.assertEqual(self.snap.get_latest_snapshot(), snapshots[-1])

    def test_snapshot_ids_do_not_depend_on_clock_resolution(self):
        with patch("snapshot_daemon.time.time", return_value=1_700_000_000):
            ids = {self.snap.create_snapshot() for _ in range(12)}
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(self.snap.list_snapshots()), 12)

    def test_failed_manifest_publication_does_not_advertise_snapshot(self):
        (self.workspace / "a").write_bytes(b"contents")
        with patch.object(self.snap, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(SnapshotError):
                self.snap.create_snapshot()
        self.assertEqual(self.snap.list_snapshots(), [])
        self.assertEqual((self.workspace / "a").read_bytes(), b"contents")
        self.snap.cleanup_old_snapshots(0)
        self.assertEqual(self.snap.storage_stats()["blob_count"], 0)

    def test_staging_failure_preserves_current_workspace(self):
        path = self.workspace / "a"
        path.write_bytes(b"before")
        sid = self.snap.create_snapshot()
        path.write_bytes(b"current")

        def fail(stage, manifest):
            stage.mkdir()
            (stage / "partial").write_bytes(b"incomplete")
            raise OSError("injected staging failure")

        with patch.object(self.snap, "_build_stage", side_effect=fail):
            with self.assertRaises(SnapshotError):
                self.snap.rollback(sid)
        self.assertEqual(path.read_bytes(), b"current")
        self.assertEqual(list(self.root.glob(".workspace.restore-*")), [])
        self.assertTrue(self.snap.rollback(sid))
        self.assertEqual(path.read_bytes(), b"before")

    def test_regular_file_to_directory_and_back(self):
        path = self.workspace / "entry"
        path.write_bytes(b"file")
        first = self.snap.create_snapshot()
        path.unlink()
        path.mkdir()
        (path / "empty").mkdir()
        second = self.snap.create_snapshot()
        self.snap.rollback(first)
        self.assertEqual(path.read_bytes(), b"file")
        self.snap.rollback(second)
        self.assertTrue((path / "empty").is_dir())

    def test_store_format_rejects_legacy_without_changing_it(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        old = legacy / "snapshots.json"
        old.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(SnapshotError, "legacy"):
            WorkspaceSnapshotter(str(self.workspace), str(legacy))
        self.assertEqual(old.read_text(), "{}")

    @unittest.skipUnless(os.name == "nt", "Windows path aliases are unavailable")
    def test_equivalent_windows_path_spelling_reopens_same_store(self):
        (self.workspace / "a").write_bytes(b"original")
        sid = self.snap.create_snapshot()
        other = WorkspaceSnapshotter(str(self.workspace).upper(), str(self.snap.snapshot_dir).upper())
        self.assertTrue(other.verify_snapshot(sid)["valid"])
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(self.workspace), buffer, len(buffer))
        if length and length < len(buffer):
            alias = WorkspaceSnapshotter(buffer.value)
            self.assertTrue(alias.verify_snapshot(sid)["valid"])

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions are unavailable")
    def test_readonly_directories_can_be_replaced_and_cleaned_up(self):
        directory = self.workspace / "readonly"
        directory.mkdir()
        (directory / "file").write_bytes(b"original")
        directory.chmod(0o555)
        self.addCleanup(lambda: directory.chmod(0o755) if directory.exists() else None)
        sid = self.snap.create_snapshot()
        self.assertTrue(self.snap.rollback(sid))
        self.assertEqual((directory / "file").read_bytes(), b"original")
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)

    @unittest.skipIf(os.name == "nt", "POSIX executable modes are unavailable")
    def test_identical_contents_preserve_individual_modes_and_mtimes(self):
        executable = self.workspace / "executable"
        ordinary = self.workspace / "ordinary"
        for path in (executable, ordinary):
            path.write_bytes(b"same contents")
        executable.chmod(0o755)
        ordinary.chmod(0o640)
        stamp = 1_700_000_000_123_456_789
        os.utime(executable, ns=(stamp, stamp))
        sid = self.snap.create_snapshot()
        executable.chmod(0o600)
        ordinary.chmod(0o600)
        self.snap.rollback(sid)
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(ordinary.stat().st_mode), 0o640)
        self.assertEqual(executable.stat().st_mtime_ns, stamp)


if __name__ == "__main__":
    unittest.main()
