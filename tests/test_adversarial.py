"""Failure-oriented contract checks for the workspace snapshot store.

These tests deliberately damage isolated temporary stores. They never operate on
the checkout itself. Platform features are skipped only when unavailable.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from snapshot_daemon import SnapshotError, WorkspaceSnapshotter


class AdversarialSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot-adversarial-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = self.root / "store"
        self.snap = WorkspaceSnapshotter(str(self.workspace), str(self.store))

    def write(self, relative, content):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def tree(self):
        """Compare observable names and file contents without relying on mtimes."""
        result = {}
        for path in sorted(self.workspace.rglob("*")):
            name = path.relative_to(self.workspace).as_posix()
            if path.is_symlink():
                result[name] = ("symlink", os.readlink(path))
            elif path.is_dir():
                result[name] = ("directory",)
            else:
                result[name] = ("file", path.read_bytes())
        return result

    def make_symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

    def snapshot_then_change(self):
        self.write("tracked.txt", b"original contents")
        sid = self.snap.create_snapshot("before failure")
        self.write("tracked.txt", b"uncommitted contents")
        self.write("untracked.txt", b"must not be deleted on failed restore")
        return sid

    def only_blob(self):
        blobs = [path for path in (self.store / "blobs").iterdir() if path.is_file()]
        self.assertEqual(len(blobs), 1)
        return blobs[0]

    def test_same_size_and_mtime_edit_is_not_mistaken_for_unchanged_data(self):
        path = self.write("tracked.txt", b"AAAA")
        old = path.stat()
        first = self.snap.create_snapshot("first")
        path.write_bytes(b"BBBB")
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        second = self.snap.create_snapshot("same metadata, different bytes")
        path.write_bytes(b"CCCC")
        self.assertTrue(self.snap.rollback(second))
        self.assertEqual(path.read_bytes(), b"BBBB")
        self.assertTrue(self.snap.rollback(first))
        self.assertEqual(path.read_bytes(), b"AAAA")

    def test_clients_created_before_writes_refresh_persisted_state(self):
        other = WorkspaceSnapshotter(str(self.workspace), str(self.store))
        self.write("tracked.txt", b"first")
        first = self.snap.create_snapshot("first client")
        self.write("tracked.txt", b"second")
        second = other.create_snapshot("second client")
        self.assertNotEqual(first, second)
        for client in (self.snap, other):
            self.assertEqual({item["id"] for item in client.list_snapshots()}, {first, second})
        self.snap.delete_snapshot(first)
        self.assertEqual([item["id"] for item in other.list_snapshots()], [second])
        reloaded = WorkspaceSnapshotter(str(self.workspace), str(self.store))
        self.write("tracked.txt", b"later")
        self.assertTrue(reloaded.rollback(second))
        self.assertEqual((self.workspace / "tracked.txt").read_bytes(), b"second")

    def test_missing_blob_is_reported_before_workspace_mutation(self):
        sid = self.snapshot_then_change()
        before = self.tree()
        blob = self.only_blob()
        blob.chmod(0o600)
        blob.unlink()
        with self.assertRaises(SnapshotError):
            self.snap.verify_snapshot(sid)
        with self.assertRaises(SnapshotError):
            self.snap.rollback(sid)
        self.assertEqual(self.tree(), before)

    def test_same_size_blob_corruption_is_reported_before_mutation(self):
        sid = self.snapshot_then_change()
        before = self.tree()
        blob = self.only_blob()
        blob.chmod(0o600)
        blob.write_bytes(b"X" * blob.stat().st_size)
        with self.assertRaises(SnapshotError):
            self.snap.verify_snapshot(sid)
        with self.assertRaises(SnapshotError):
            self.snap.rollback(sid)
        self.assertEqual(self.tree(), before)

    def test_invalid_or_unknown_snapshot_ids_leave_workspace_unchanged(self):
        self.snapshot_then_change()
        before = self.tree()
        ids = ("", "..", "../escape", "/absolute", "C:\\escape", "a/b", "snapshot_" + "0" * 32)
        for sid in ids:
            with self.subTest(snapshot_id=sid):
                with self.assertRaises(SnapshotError):
                    self.snap.rollback(sid)
                self.assertEqual(self.tree(), before)

    def test_malicious_manifest_paths_cannot_escape_workspace(self):
        sid = self.snapshot_then_change()
        manifest_path = self.store / "manifests" / f"{sid}.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("tracked.txt", original["entries"])
        outside = self.root / "escape.txt"
        outside.write_bytes(b"outside sentinel")
        before = self.tree()
        for name in ("../escape.txt", "nested/../../escape.txt", str(outside), "C:\\escape.txt"):
            with self.subTest(path=name):
                data = json.loads(json.dumps(original))
                data["entries"][name] = data["entries"].pop("tracked.txt")
                manifest_path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(SnapshotError):
                    self.snap.rollback(sid)
                self.assertEqual(self.tree(), before)
                self.assertEqual(outside.read_bytes(), b"outside sentinel")

    def test_truncated_manifest_never_silently_restores_partial_tree(self):
        sid = self.snapshot_then_change()
        before = self.tree()
        manifest_path = self.store / "manifests" / f"{sid}.json"
        manifest_path.write_text('{"files":', encoding="utf-8")
        with self.assertRaises(SnapshotError):
            self.snap.rollback(sid)
        self.assertEqual(self.tree(), before)

    def test_workspace_local_snapshot_store_is_rejected(self):
        with self.assertRaises(SnapshotError):
            WorkspaceSnapshotter(str(self.workspace), str(self.workspace / "hidden-store"))

    def test_store_cannot_be_reused_for_a_different_workspace(self):
        self.write("tracked.txt", b"first workspace")
        sid = self.snap.create_snapshot("belongs to first workspace")
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        sentinel = other_workspace / "sentinel"
        sentinel.write_bytes(b"second workspace")
        with self.assertRaises(SnapshotError):
            WorkspaceSnapshotter(str(other_workspace), str(self.store))
        self.assertEqual(sentinel.read_bytes(), b"second workspace")
        self.assertTrue(self.snap.verify_snapshot(sid)["valid"])

    def test_regular_file_symlink_is_rejected_without_following_target(self):
        target = self.root / "outside.txt"
        target.write_bytes(b"outside sentinel")
        self.make_symlink(target, self.workspace / "link")
        with self.assertRaises(SnapshotError):
            self.snap.create_snapshot("unsupported link")
        self.assertEqual(target.read_bytes(), b"outside sentinel")
        self.assertEqual(self.snap.list_snapshots(), [])

    def test_directory_symlink_is_rejected_without_following_target(self):
        target = self.root / "outside-dir"
        target.mkdir()
        (target / "secret.txt").write_bytes(b"outside sentinel")
        self.make_symlink(target, self.workspace / "link", directory=True)
        with self.assertRaises(SnapshotError):
            self.snap.create_snapshot("unsupported directory link")
        self.assertEqual(self.snap.list_snapshots(), [])

    def test_dangling_symlink_is_rejected_instead_of_omitted(self):
        self.make_symlink(self.root / "does-not-exist", self.workspace / "dangling")
        with self.assertRaises(SnapshotError):
            self.snap.create_snapshot("dangling link")
        self.assertEqual(self.snap.list_snapshots(), [])

    def test_blob_symlink_is_rejected_without_using_external_data(self):
        sid = self.snapshot_then_change()
        before = self.tree()
        blob = self.only_blob()
        external = self.root / "external-blob"
        external.write_bytes(blob.read_bytes())
        # Probe capability first, so an unavailable feature does not corrupt this store.
        probe = self.root / "probe-link"
        self.make_symlink(external, probe)
        probe.unlink()
        blob.chmod(0o600)
        blob.unlink()
        blob.symlink_to(external)
        with self.assertRaises(SnapshotError):
            self.snap.rollback(sid)
        self.assertEqual(self.tree(), before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO support is unavailable")
    def test_fifo_is_rejected_promptly_instead_of_blocking_read(self):
        os.mkfifo(self.workspace / "named-pipe")
        script = (
            "import sys\n"
            "from snapshot_daemon import WorkspaceSnapshotter, SnapshotError\n"
            "try:\n"
            "    WorkspaceSnapshotter(sys.argv[1], sys.argv[2]).create_snapshot()\n"
            "except SnapshotError:\n"
            "    sys.exit(0)\n"
            "sys.exit(7)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.workspace), str(self.store)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.snap.list_snapshots(), [])

    def test_nested_transaction_releases_lock_when_user_operation_raises(self):
        with self.assertRaisesRegex(ValueError, "user operation failed"):
            with self.snap.transaction():
                with self.snap.transaction():
                    self.write("tracked.txt", b"kept")
                    self.snap.create_snapshot("inside nested lock")
                    raise ValueError("user operation failed")
        # New process must acquire the lock; same-process reentrancy could hide a leak.
        script = (
            "import sys; from snapshot_daemon import WorkspaceSnapshotter; "
            "s=WorkspaceSnapshotter(sys.argv[1],sys.argv[2]); "
            "assert len(s.list_snapshots()) == 1"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.workspace), str(self.store)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_concurrent_processes_publish_all_snapshots_without_lost_updates(self):
        self.write("tracked.txt", b"shared unchanged workspace")
        script = (
            "import json, sys\n"
            "from snapshot_daemon import WorkspaceSnapshotter\n"
            "s = WorkspaceSnapshotter(sys.argv[1], sys.argv[2])\n"
            "print(json.dumps([s.create_snapshot('parallel') for _ in range(3)]))\n"
        )
        processes = []
        try:
            for _ in range(4):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(self.workspace), str(self.store)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ))
            created = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                created.extend(json.loads(stdout))
            self.assertEqual(len(set(created)), 12)
            self.assertEqual({item["id"] for item in self.snap.list_snapshots()}, set(created))
            for sid in created:
                self.assertTrue(self.snap.verify_snapshot(sid)["valid"])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def crash_during_restore(self, sid, crashpoint):
        """Kill a subprocess after a real directory rename, without exception cleanup."""
        script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "import snapshot_daemon\n"
            "workspace = Path(sys.argv[1]).resolve()\n"
            "s = snapshot_daemon.WorkspaceSnapshotter(str(workspace), sys.argv[2])\n"
            "real_replace = os.replace\n"
            "def interrupted_replace(src, dst, *args, **kwargs):\n"
            "    real_replace(src, dst, *args, **kwargs)\n"
            "    old_moved = Path(src) == workspace and Path(dst) != workspace\n"
            "    new_installed = Path(dst) == workspace and Path(src) != workspace\n"
            "    if (sys.argv[4] == 'old_moved' and old_moved) or (sys.argv[4] == 'new_installed' and new_installed):\n"
            "        os._exit(91)\n"
            "snapshot_daemon.os.replace = interrupted_replace\n"
            "s.rollback(sys.argv[3])\n"
            "sys.exit(7)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.workspace), str(self.store), sid, crashpoint],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 91, result.stdout + result.stderr)

    def test_process_crash_after_old_tree_rename_recovers_original_workspace(self):
        sid = self.snapshot_then_change()
        before = self.tree()
        self.crash_during_restore(sid, "old_moved")
        recovered = WorkspaceSnapshotter(str(self.workspace), str(self.store))
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.store / "rollback.json").exists())
        self.assertTrue(recovered.verify_snapshot(sid)["valid"])
        self.assertTrue(recovered.rollback(sid))
        self.assertEqual((self.workspace / "tracked.txt").read_bytes(), b"original contents")

    def test_process_crash_after_new_tree_rename_preserves_complete_target(self):
        sid = self.snapshot_then_change()
        self.crash_during_restore(sid, "new_installed")
        recovered = WorkspaceSnapshotter(str(self.workspace), str(self.store))
        self.assertEqual(self.tree(), {"tracked.txt": ("file", b"original contents")})
        self.assertFalse((self.store / "rollback.json").exists())
        self.assertTrue(recovered.verify_snapshot(sid)["valid"])
        self.assertTrue(recovered.rollback(sid))


if __name__ == "__main__":
    unittest.main()
