"""Content-addressed filesystem checkpoints for owned, quiescent workspaces.

This is a file-copy implementation, not overlayfs or a VM snapshot. Callers must
stop independent writers while checkpointing/restoring. The lock coordinates
clients of one store; it cannot lock arbitrary processes out of a workspace.
Symlinks, reparse points, and special files are intentionally unsupported.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
from typing import Any, Iterator
import uuid


class SnapshotError(RuntimeError):
    """A checkpoint operation could not safely complete."""


_ID = re.compile(r"snapshot_[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _plain_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SnapshotError(f"Symlinks/reparse points are unsupported: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise SnapshotError(f"Special files are unsupported: {path}")
    return info


def _check_ancestors(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part):
            _plain_stat(part)


def _guard_tree(root: Path) -> None:
    """Validate all entries, including broken links and junctions."""
    _check_ancestors(root)
    stack = [root]
    while stack:
        path = stack.pop()
        if stat.S_ISDIR(_plain_stat(path).st_mode):
            stack.extend(path.iterdir())


def _sync_dir(path: Path) -> None:
    # Windows has no POSIX directory-fsync interface. Rename recovery works;
    # power-loss durability still depends on the operating system/filesystem.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _remove_tree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    _guard_tree(path)

    # Only disposable stage/backup trees reach this helper. Make directories
    # writable before unlinking children: chmod on a child alone does not grant
    # POSIX permission to remove it from a read-only parent directory.
    for directory, subdirectories, files in os.walk(path):
        current = Path(directory)
        current.chmod(stat.S_IMODE(current.stat().st_mode) | 0o700)
        for filename in files:
            file_path = current / filename
            file_path.chmod(stat.S_IMODE(file_path.stat().st_mode) | 0o600)
    shutil.rmtree(path)


class WorkspaceSnapshotter:
    """SHA-256 blobs, atomic manifests, and recoverable whole-tree restores.

    Store and workspace must be disjoint directories. Restore builds a sibling
    tree and performs two renames, leaving a brief interval when the workspace
    path is absent. A journal/backup recover interrupted renames. No live-writer
    isolation, timing bound, ACL/xattr preservation, or network-filesystem lock
    guarantee is provided. Stop processes with open workspace files first.
    """

    def __init__(self, workspace_path: str, snapshot_dir: str | None = None):
        self.workspace = Path(os.path.abspath(os.path.expanduser(workspace_path)))
        self.snapshot_dir = Path(os.path.abspath(os.path.expanduser(snapshot_dir or (str(self.workspace) + ".snapshots"))))
        self._thread_lock = threading.RLock()
        self._depth = 0
        try:
            self._check_locations()
            if not self.workspace.exists() and not (self.snapshot_dir / "rollback.json").is_file():
                raise SnapshotError(f"Workspace directory does not exist: {self.workspace}")
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            with self.transaction():
                pass
        except SnapshotError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SnapshotError(f"Cannot initialize checkpoint store: {exc}") from exc

    def _check_locations(self) -> None:
        _check_ancestors(self.workspace)
        _check_ancestors(self.snapshot_dir)
        workspace = os.path.normcase(str(self.workspace))
        store = os.path.normcase(str(self.snapshot_dir))
        try:
            common = os.path.commonpath((workspace, store))
        except ValueError:
            common = ""  # Different drives; staging stays beside workspace.
        if common in (workspace, store):
            raise SnapshotError("Workspace and snapshot store must not overlap")
        if self.workspace == self.workspace.parent:
            raise SnapshotError("A filesystem root cannot be a workspace")

    @contextmanager
    def transaction(self) -> Iterator[WorkspaceSnapshotter]:
        """Lock checkpoint/action/restore as a unit; nesting is safe per client.

        Independent writers remain the caller's responsibility. Different stores
        targeting the same workspace are not coordinated by this lock.
        """
        with self._thread_lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            lock = None
            acquired = False
            try:
                self._check_locations()
                # Inspect the stable lock path before locking. Walking mutable
                # store entries here races another client's temporary files.
                _check_ancestors(self.snapshot_dir / ".lock")
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                fd = os.open(self.snapshot_dir / ".lock", flags, 0o600)
                lock = os.fdopen(fd, "r+b", buffering=0)
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"\0")
                self._acquire_lock(lock)
                acquired = True
                self._depth = 1
                self._initialize_store()
                _guard_tree(self.snapshot_dir)
                self._recover_rollback()
                if not self.workspace.is_dir():
                    raise SnapshotError(f"Workspace directory does not exist: {self.workspace}")
                yield self
            except SnapshotError:
                raise
            except (OSError, TypeError, KeyError) as exc:
                raise SnapshotError(f"Checkpoint operation failed: {exc}") from exc
            finally:
                self._depth = 0
                if lock is not None:
                    try:
                        if acquired:
                            if os.name == "nt":
                                import msvcrt
                                lock.seek(0)
                                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock.close()

    @staticmethod
    def _acquire_lock(lock) -> None:
        deadline = time.monotonic() + 30
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise SnapshotError("Timed out waiting for the checkpoint-store lock") from exc
                time.sleep(0.05)

    def _initialize_store(self) -> None:
        expected = {"format": "agent-workspace-checkpoints", "version": 2, "workspace": str(self.workspace)}
        format_path = self.snapshot_dir / "format.json"
        if not format_path.exists():
            if {p.name for p in self.snapshot_dir.iterdir()} - {".lock"}:
                raise SnapshotError("Unrecognized/legacy store; choose a new empty snapshot directory")
            self._atomic_json(format_path, expected)
        if self._read_json(format_path) != expected:
            raise SnapshotError("Unsupported store format or store bound to a different workspace")
        (self.snapshot_dir / "blobs").mkdir(exist_ok=True)
        (self.snapshot_dir / "manifests").mkdir(exist_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not stat.S_ISREG(_plain_stat(path).st_mode):
            raise SnapshotError(f"Expected a regular JSON file: {path}")
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise SnapshotError(f"Cannot read JSON file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotError(f"Invalid JSON object: {path}")
        return data

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, sort_keys=True, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _sync_dir(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _validate_id(snapshot_id: str) -> None:
        if not isinstance(snapshot_id, str) or not _ID.fullmatch(snapshot_id):
            raise SnapshotError("Invalid snapshot ID")

    @staticmethod
    def _validate_metadata(value: Any) -> None:
        if not isinstance(value, dict):
            raise SnapshotError("Invalid entry metadata")
        if type(value.get("mode")) is not int or not 0 <= value["mode"] <= 0o7777:
            raise SnapshotError("Invalid entry mode")
        if type(value.get("mtime_ns")) is not int:
            raise SnapshotError("Invalid entry modification time")

    @staticmethod
    def _validate_relative(relative: str) -> None:
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or "\0" in relative:
            raise SnapshotError(f"Unsupported relative path: {relative!r}")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != relative or relative == ".":
            raise SnapshotError(f"Unsafe manifest path: {relative!r}")

    def _load_manifest(self, snapshot_id: str) -> dict:
        self._validate_id(snapshot_id)
        path = self.snapshot_dir / "manifests" / (snapshot_id + ".json")
        if not path.exists():
            raise SnapshotError(f"Snapshot not found: {snapshot_id}")
        manifest = self._read_json(path)
        if manifest.get("version") != 2 or manifest.get("id") != snapshot_id:
            raise SnapshotError(f"Invalid manifest identity: {snapshot_id}")
        timestamp = manifest.get("timestamp")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            raise SnapshotError("Invalid manifest timestamp")
        if not isinstance(manifest.get("label"), str):
            raise SnapshotError("Invalid manifest label")
        self._validate_metadata(manifest.get("root"))
        entries = manifest.get("entries")
        if not isinstance(entries, dict):
            raise SnapshotError("Invalid manifest entries")
        for relative, metadata in entries.items():
            self._validate_relative(relative)
            self._validate_metadata(metadata)
            if metadata.get("kind") not in ("file", "directory"):
                raise SnapshotError(f"Invalid entry type: {relative}")
            if metadata["kind"] == "file":
                if not isinstance(metadata.get("hash"), str) or not _HASH.fullmatch(metadata["hash"]):
                    raise SnapshotError(f"Invalid blob hash: {relative}")
                if type(metadata.get("size")) is not int or metadata["size"] < 0:
                    raise SnapshotError(f"Invalid file size: {relative}")
            for parent in PurePosixPath(relative).parents:
                if str(parent) != "." and entries.get(str(parent), {}).get("kind") != "directory":
                    raise SnapshotError(f"Missing directory entry: {parent}")
        return manifest

    def _all_manifests(self) -> list[dict]:
        manifests = []
        for path in sorted((self.snapshot_dir / "manifests").iterdir()):
            if path.name.startswith(".tmp-"):
                continue
            if not path.name.endswith(".json"):
                raise SnapshotError(f"Unexpected manifest-store entry: {path.name}")
            manifests.append(self._load_manifest(path.stem))
        return manifests

    @staticmethod
    def _identity(info: os.stat_result) -> tuple:
        # On Windows, lstat and fstat can expose different ctime semantics.
        # Comparing them rejected unchanged files. This is a best-effort race
        # check only; the contract still requires a quiescent workspace.
        identity = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode
        return identity if os.name == "nt" else (*identity, info.st_ctime_ns)

    @staticmethod
    def _metadata(info: os.stat_result) -> dict:
        return {"mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}

    def _store_file(self, source: Path) -> tuple[str, int]:
        before = _plain_stat(source)
        temporary = self.snapshot_dir / "blobs" / (".tmp-" + uuid.uuid4().hex)
        digest = hashlib.sha256()
        size = 0
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(source, flags), "rb") as original, temporary.open("xb") as output:
                if self._identity(os.fstat(original.fileno())) != self._identity(before):
                    raise SnapshotError(f"File changed while opening: {source}")
                while chunk := original.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if self._identity(_plain_stat(source)) != self._identity(before):
                raise SnapshotError(f"File changed during checkpoint: {source}")
            checksum = digest.hexdigest()
            target = self.snapshot_dir / "blobs" / checksum
            if target.exists():
                self._verify_blob(checksum, size)
            else:
                os.replace(temporary, target)
                _sync_dir(target.parent)
            return checksum, size
        finally:
            if temporary.exists():
                temporary.unlink()

    def create_snapshot(self, label: str = "") -> str:
        with self.transaction():
            if not isinstance(label, str):
                raise SnapshotError("Snapshot label must be text")
            _guard_tree(self.workspace)
            root_stat = self.workspace.stat()
            entries: dict[str, dict] = {}
            identities = {}
            for path in sorted(self.workspace.rglob("*")):
                relative = path.relative_to(self.workspace).as_posix()
                self._validate_relative(relative)
                info = _plain_stat(path)
                identities[relative] = self._identity(info)
                metadata = self._metadata(info)
                if stat.S_ISDIR(info.st_mode):
                    metadata["kind"] = "directory"
                else:
                    checksum, size = self._store_file(path)
                    metadata.update(kind="file", hash=checksum, size=size)
                entries[relative] = metadata
            # Detect ordinary edits, not a substitute for caller quiescence.
            current = {p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob("*")}
            if current != set(entries) or self._identity(self.workspace.stat()) != self._identity(root_stat):
                raise SnapshotError("Workspace changed during checkpoint")
            for relative, identity in identities.items():
                if self._identity(_plain_stat(self.workspace / relative)) != identity:
                    raise SnapshotError(f"Workspace changed during checkpoint: {relative}")
            snapshot_id = "snapshot_" + uuid.uuid4().hex
            manifest = {"version": 2, "id": snapshot_id, "timestamp": time.time(), "label": label,
                        "root": self._metadata(root_stat), "entries": entries}
            self._atomic_json(self.snapshot_dir / "manifests" / (snapshot_id + ".json"), manifest)
            return snapshot_id

    def _verify_blob(self, checksum: str, expected_size: int) -> None:
        path = self.snapshot_dir / "blobs" / checksum
        if not path.exists():
            raise SnapshotError(f"Missing content blob: {checksum}")
        info = _plain_stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            raise SnapshotError(f"Invalid content blob size/type: {checksum}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != checksum:
            raise SnapshotError(f"Corrupt content blob: {checksum}")

    def _verify_manifest(self, manifest: dict) -> dict:
        verified = {}
        file_count = directory_count = total_bytes = 0
        for metadata in manifest["entries"].values():
            if metadata["kind"] == "directory":
                directory_count += 1
                continue
            checksum, size = metadata["hash"], metadata["size"]
            if checksum in verified and verified[checksum] != size:
                raise SnapshotError("Inconsistent sizes for the same content blob")
            if checksum not in verified:
                self._verify_blob(checksum, size)
                verified[checksum] = size
            file_count += 1
            total_bytes += size
        return {"id": manifest["id"], "valid": True, "file_count": file_count,
                "directory_count": directory_count, "total_bytes": total_bytes}

    def verify_snapshot(self, snapshot_id: str) -> dict:
        with self.transaction():
            return self._verify_manifest(self._load_manifest(snapshot_id))

    @staticmethod
    def _apply_metadata(path: Path, metadata: dict) -> None:
        os.utime(path, ns=(metadata["mtime_ns"], metadata["mtime_ns"]))
        os.chmod(path, metadata["mode"])

    def _build_stage(self, stage: Path, manifest: dict) -> None:
        stage.mkdir(mode=0o700)
        entries = manifest["entries"]
        directories = [rel for rel, meta in entries.items() if meta["kind"] == "directory"]
        for relative in sorted(directories, key=lambda value: (value.count("/"), value)):
            (stage / relative).mkdir(mode=0o700)
        for relative, metadata in entries.items():
            if metadata["kind"] != "file":
                continue
            target = stage / relative
            source = self.snapshot_dir / "blobs" / metadata["hash"]
            digest = hashlib.sha256()
            with source.open("rb") as original, target.open("xb") as restored:
                while chunk := original.read(1024 * 1024):
                    digest.update(chunk)
                    restored.write(chunk)
                restored.flush()
                os.fsync(restored.fileno())
            if digest.hexdigest() != metadata["hash"] or target.stat().st_size != metadata["size"]:
                raise SnapshotError(f"Blob changed during restore: {relative}")
            self._apply_metadata(target, metadata)
        for relative in sorted(directories, key=lambda value: (value.count("/"), value), reverse=True):
            _sync_dir(stage / relative)
            self._apply_metadata(stage / relative, entries[relative])
        _sync_dir(stage)
        self._apply_metadata(stage, manifest["root"])
        _sync_dir(stage.parent)

    def _journal_paths(self, journal: dict) -> tuple[Path, Path]:
        token = journal.get("token")
        if journal.get("version") != 1 or journal.get("workspace") != str(self.workspace):
            raise SnapshotError("Rollback journal belongs to a different workspace or format")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise SnapshotError("Invalid rollback journal token")
        if journal.get("phase") not in ("prepared", "backed_up", "installed"):
            raise SnapshotError("Invalid rollback journal phase")
        self._validate_id(journal.get("snapshot_id"))
        return (self.workspace.parent / f".{self.workspace.name}.restore-{token}",
                self.workspace.parent / f".{self.workspace.name}.backup-{token}")

    def _recover_rollback(self) -> None:
        journal_path = self.snapshot_dir / "rollback.json"
        if not journal_path.exists():
            return
        journal = self._read_json(journal_path)
        stage, backup = self._journal_paths(journal)
        for candidate in (stage, backup):
            if os.path.lexists(candidate):
                _guard_tree(candidate)
                if not candidate.is_dir():
                    raise SnapshotError(f"Rollback recovery expected a directory: {candidate}")
        workspace_exists = self.workspace.exists()
        if backup.exists() and not workspace_exists:
            os.replace(backup, self.workspace)
            _sync_dir(self.workspace.parent)
            _remove_tree(stage)
        elif backup.exists() and workspace_exists and not stage.exists():
            # Stage-to-workspace rename committed, possibly before the journal
            # update. Keep the target and finish cleanup.
            _guard_tree(self.workspace)
            _remove_tree(backup)
        elif workspace_exists and not backup.exists():
            _remove_tree(stage)
        else:
            raise SnapshotError("Ambiguous recovery; workspace/stage/backup preserved for inspection")
        _sync_dir(self.workspace.parent)
        journal_path.unlink()
        _sync_dir(self.snapshot_dir)

    def rollback(self, snapshot_id: str) -> bool:
        with self.transaction():
            manifest = self._load_manifest(snapshot_id)
            self._verify_manifest(manifest)  # Whole preflight before mutation.
            _guard_tree(self.workspace)
            token = uuid.uuid4().hex
            journal = {"version": 1, "token": token, "workspace": str(self.workspace),
                       "snapshot_id": snapshot_id, "phase": "prepared"}
            stage, backup = self._journal_paths(journal)
            journal_path = self.snapshot_dir / "rollback.json"
            try:
                self._build_stage(stage, manifest)
                self._atomic_json(journal_path, journal)
                os.replace(self.workspace, backup)
                _sync_dir(self.workspace.parent)
                journal["phase"] = "backed_up"
                self._atomic_json(journal_path, journal)
                os.replace(stage, self.workspace)
                _sync_dir(self.workspace.parent)
                journal["phase"] = "installed"
                self._atomic_json(journal_path, journal)
                _remove_tree(backup)
                _sync_dir(self.workspace.parent)
                journal_path.unlink()
                _sync_dir(self.snapshot_dir)
            except Exception as exc:
                try:
                    if journal_path.exists():
                        self._recover_rollback()
                    else:
                        _remove_tree(stage)
                except Exception as recovery_exc:
                    raise SnapshotError(f"Rollback interrupted; recovery data preserved in {journal_path}: {recovery_exc}") from exc
                raise SnapshotError(f"Rollback encountered an error; recovery completed: {exc}") from exc
            return True

    def list_snapshots(self) -> list[dict]:
        with self.transaction():
            return [{"id": item["id"],
                     "timestamp": datetime.fromtimestamp(item["timestamp"], timezone.utc).isoformat(),
                     "label": item["label"],
                     "file_count": sum(meta["kind"] == "file" for meta in item["entries"].values()),
                     "directory_count": sum(meta["kind"] == "directory" for meta in item["entries"].values())}
                    for item in sorted(self._all_manifests(), key=lambda item: (item["timestamp"], item["id"]))]

    def get_latest_snapshot(self) -> str | None:
        with self.transaction():
            manifests = self._all_manifests()
            return max(manifests, key=lambda item: (item["timestamp"], item["id"]))["id"] if manifests else None

    def _referenced_blobs(self) -> set[str]:
        return {meta["hash"] for manifest in self._all_manifests()
                for meta in manifest["entries"].values() if meta["kind"] == "file"}

    def _garbage_collect(self) -> None:
        referenced = self._referenced_blobs()
        for path in (self.snapshot_dir / "blobs").iterdir():
            if not stat.S_ISREG(_plain_stat(path).st_mode):
                raise SnapshotError(f"Unexpected blob directory: {path.name}")
            if not (_HASH.fullmatch(path.name) or path.name.startswith(".tmp-")):
                raise SnapshotError(f"Unexpected blob-store entry: {path.name}")
            if path.name not in referenced:
                path.unlink()
        _sync_dir(self.snapshot_dir / "blobs")

    def delete_snapshot(self, snapshot_id: str) -> bool:
        with self.transaction():
            self._load_manifest(snapshot_id)
            self._all_manifests()
            (self.snapshot_dir / "manifests" / (snapshot_id + ".json")).unlink()
            _sync_dir(self.snapshot_dir / "manifests")
            self._garbage_collect()
            return True

    def cleanup_old_snapshots(self, keep_last_n: int = 10) -> None:
        with self.transaction():
            if type(keep_last_n) is not int or keep_last_n < 0:
                raise SnapshotError("keep_last_n must be a nonnegative integer")
            manifests = sorted(self._all_manifests(), key=lambda item: (item["timestamp"], item["id"]), reverse=True)
            for manifest in manifests[keep_last_n:]:
                (self.snapshot_dir / "manifests" / (manifest["id"] + ".json")).unlink()
            _sync_dir(self.snapshot_dir / "manifests")
            self._garbage_collect()

    def storage_stats(self) -> dict:
        with self.transaction():
            manifests = self._all_manifests()
            referenced = self._referenced_blobs()
            blobs = []
            for path in (self.snapshot_dir / "blobs").iterdir():
                if _HASH.fullmatch(path.name):
                    if not stat.S_ISREG(_plain_stat(path).st_mode):
                        raise SnapshotError(f"Invalid blob type: {path.name}")
                    blobs.append(path)
            return {"snapshot_count": len(manifests), "blob_count": len(blobs),
                    "blob_bytes": sum(path.stat().st_size for path in blobs),
                    "referenced_blob_count": len(referenced),
                    "unreferenced_blob_count": len({path.name for path in blobs} - referenced)}

    def diff_snapshots(self, before: str, after: str) -> dict[str, list[str]]:
        with self.transaction():
            first = self._load_manifest(before)["entries"]
            second = self._load_manifest(after)["entries"]
            return {"added": sorted(second.keys() - first.keys()),
                    "removed": sorted(first.keys() - second.keys()),
                    "modified": sorted(path for path in first.keys() & second.keys() if first[path] != second[path])}


def main():
    # The package CLI owns argument validation and nonzero error exits.
    from workspace_cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
