#!/usr/bin/env python3
"""Measure real, verified workspace snapshots against a full-copy baseline.

This is a local filesystem microbenchmark. It does not measure virtual machines,
Dedalus, agent quality, or a universal latency guarantee. Every sample restores
the same seeded fixture and verifies paths, bytes, permissions, and timestamps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from snapshot_daemon import WorkspaceSnapshotter


def tree_inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Independently describe files and empty directories, excluding root metadata."""
    inventory = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        entry = {"mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns}
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entry.update(type="file", size=info.st_size, sha256=digest.hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            entry.update(type="directory")
        else:
            raise ValueError(f"Unsupported fixture entry: {path}")
        inventory[path.relative_to(root).as_posix()] = entry
    return inventory


def assert_restored(root: Path, expected: dict[str, dict[str, Any]]) -> None:
    """Fail the run rather than report a fast but incorrect restoration."""
    actual = tree_inventory(root)
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(key for key in expected.keys() & actual.keys()
                         if expected[key] != actual[key])
        raise AssertionError(
            f"Restore verification failed: missing={missing[:5]}, "
            f"extra={extra[:5]}, changed={changed[:5]}"
        )


def _create_fixture(root: Path, files: int, size_kib: int, seed: int) -> None:
    rng = random.Random(seed)
    root.mkdir()
    for index in range(files):
        path = root / f"group_{index % 8:02d}" / f"file_{index:06d}.bin"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(rng.randbytes(size_kib * 1024))
    (root / "empty_directory").mkdir()
    (root / "nested_empty" / "child").mkdir(parents=True)
    # Integer-second times are reproducible across common filesystem resolutions.
    timestamp_ns = 1_700_000_000_000_000_000
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
        os.utime(path, ns=(timestamp_ns, timestamp_ns))


def _mutate_fixture(root: Path, seed: int) -> dict[str, int]:
    rng = random.Random(seed + 1)
    paths = sorted(root.glob("group_*/*.bin"))
    edit_count = max(1, len(paths) // 4)
    delete_count = len(paths) // 4
    for path in paths[:edit_count]:
        path.write_bytes(rng.randbytes(path.stat().st_size))
    for path in paths[len(paths) - delete_count:] if delete_count else []:
        path.unlink()
    additions = max(1, len(paths) // 4)
    new_dir = root / "new_directory"
    new_dir.mkdir()
    for index in range(additions):
        (new_dir / f"added_{index}.bin").write_bytes(rng.randbytes(1024))
    (root / "empty_directory").rmdir()
    (root / "empty_directory").write_text("directory replaced by file", encoding="utf-8")
    return {"files_edited": edit_count, "files_deleted": delete_count,
            "files_added": additions + 1, "directories_added": 1,
            "directories_replaced_by_files": 1}


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: list[float]) -> dict[str, float]:
    return {"p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95),
            "min": min(values), "max": max(values)}


def _logical_file_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _filesystem_type(path: Path) -> str:
    """Best-effort context; unsupported platforms are reported honestly."""
    if os.name == "nt":
        try:
            import ctypes
            volume = ctypes.create_unicode_buffer(261)
            filesystem = ctypes.create_unicode_buffer(261)
            kernel32 = ctypes.windll.kernel32
            if kernel32.GetVolumePathNameW(str(path), volume, len(volume)):
                if kernel32.GetVolumeInformationW(volume.value, None, 0, None, None,
                                                  None, filesystem, len(filesystem)):
                    return filesystem.value
        except (AttributeError, OSError):
            pass
    elif Path("/proc/mounts").exists():
        candidates = []
        with Path("/proc/mounts").open(encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                mount = fields[1].replace("\\040", " ")
                try:
                    path.relative_to(mount)
                    candidates.append((len(mount), fields[2]))
                except ValueError:
                    pass
        if candidates:
            return max(candidates)[1]
    return "not detected"


def run_benchmark(*, files: int = 100, size_kib: int = 4, iterations: int = 5,
                  seed: int = 42, temp_parent: str | Path | None = None) -> dict[str, Any]:
    if files < 1 or size_kib < 1 or iterations < 1:
        raise ValueError("files, size_kib, and iterations must all be positive")
    with tempfile.TemporaryDirectory(prefix="workspace-benchmark-", dir=temp_parent) as temporary:
        root = Path(temporary).resolve()
        workspaces = {name: root / name for name in ("content_store", "full_copy")}
        for workspace in workspaces.values():
            _create_fixture(workspace, files, size_kib, seed)
        expected = tree_inventory(workspaces["content_store"])
        assert_restored(workspaces["full_copy"], expected)
        store_path = root / "snapshot_store"
        snapshotter = WorkspaceSnapshotter(str(workspaces["content_store"]), str(store_path))
        copies_path = root / "full_copy_snapshots"
        copies_path.mkdir()
        samples: dict[str, list[dict[str, Any]]] = {name: [] for name in workspaces}
        mutation_counts: dict[str, int] = {}

        for iteration in range(iterations):
            # Alternate measured order; neither backend gets only the first position.
            order = list(workspaces) if iteration % 2 == 0 else list(reversed(workspaces))
            for position, backend in enumerate(order):
                workspace = workspaces[backend]
                start = time.perf_counter_ns()
                if backend == "content_store":
                    snapshot_id = snapshotter.create_snapshot(f"benchmark_{iteration}")
                else:
                    copy_path = copies_path / f"snapshot_{iteration:04d}"
                    shutil.copytree(workspace, copy_path, copy_function=shutil.copy2)
                snapshot_ms = (time.perf_counter_ns() - start) / 1_000_000
                if backend == "content_store":
                    snapshotter.verify_snapshot(snapshot_id)
                else:
                    assert_restored(copy_path, expected)
                mutation_counts = _mutate_fixture(workspace, seed)

                start = time.perf_counter_ns()
                if backend == "content_store":
                    if snapshotter.rollback(snapshot_id) is not True:
                        raise AssertionError("Snapshot engine did not report successful rollback")
                else:
                    shutil.rmtree(workspace)
                    shutil.copytree(copy_path, workspace, copy_function=shutil.copy2)
                rollback_ms = (time.perf_counter_ns() - start) / 1_000_000
                assert_restored(workspace, expected)
                samples[backend].append({"iteration": iteration + 1,
                                         "measurement_order": position + 1,
                                         "snapshot_ms": snapshot_ms,
                                         "rollback_ms": rollback_ms,
                                         "snapshot_plus_rollback_ms": snapshot_ms + rollback_ms,
                                         "restoration_verified": True})

        results = {}
        for backend, raw_samples in samples.items():
            results[backend] = {"raw_samples": raw_samples,
                                "snapshot_ms": _summary([s["snapshot_ms"] for s in raw_samples]),
                                "rollback_ms": _summary([s["rollback_ms"] for s in raw_samples]),
                                "snapshot_plus_rollback_ms": _summary(
                                    [s["snapshot_plus_rollback_ms"] for s in raw_samples])}
        results["content_store"]["storage"] = {
            **snapshotter.storage_stats(), "logical_file_bytes": _logical_file_bytes(store_path)}
        results["full_copy"]["storage"] = {
            "snapshot_count": iterations, "logical_file_bytes": _logical_file_bytes(copies_path)}
        disk = shutil.disk_usage(root)
        return {
            "schema_version": 1,
            "description": "Verified local filesystem microbenchmark; no VM or Dedalus measurements",
            "context": {"python": sys.version, "os": platform.platform(),
                        "machine": platform.machine(), "processor": platform.processor() or "unknown",
                        "logical_cpu_count": os.cpu_count(), "filesystem": _filesystem_type(root),
                        "temporary_parent": str(root.parent), "device_id": root.stat().st_dev,
                        "volume_total_bytes": disk.total, "volume_free_bytes": disk.free},
            "workload": {"seed": seed, "file_count": files, "file_size_bytes": size_kib * 1024,
                         "total_file_bytes": files * size_kib * 1024,
                         "directory_count": sum(e["type"] == "directory" for e in expected.values()),
                         "iterations_per_backend": iterations, "mutations_per_sample": mutation_counts},
            "methodology": [
                "Both backends receive the same deterministic fixture and mutations; order alternates.",
                "All retained snapshots contain the same baseline; the content store can deduplicate them.",
                "The first content-store snapshot starts with an empty store; later samples reuse it.",
                "OS caches are not flushed; these are local observed timings, not cold-cache guarantees.",
                "Snapshot and rollback timers include each backend's normal integrity and durability work.",
                "The full-copy baseline uses copytree/copy2, without extra integrity hashing or explicit fsync.",
                "Independent correctness checks and mutation setup are outside timing intervals.",
                "Verification compares every relative path, file bytes, mode, and mtime_ns, including empty directories.",
                "Percentiles use linear interpolation; few samples do not establish tail-latency guarantees.",
                "Storage reports logical file sizes, not allocated disk blocks; all samples are retained.",
                "This measures regular files/directories, not symlinks, concurrent writers, VMs, or external effects."
            ],
            "results": results,
            "all_restorations_verified": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--size-kib", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--temp-parent", type=Path, help="Existing directory for disposable test data")
    parser.add_argument("--json", type=Path, help="Save context, raw samples, and summaries")
    args = parser.parse_args(argv)
    if min(args.files, args.size_kib, args.iterations) < 1:
        parser.error("--files, --size-kib, and --iterations must be positive")
    result = run_benchmark(files=args.files, size_kib=args.size_kib,
                           iterations=args.iterations, seed=args.seed, temp_parent=args.temp_parent)
    print("Verified filesystem benchmark (milliseconds; local observations)")
    for name, values in result["results"].items():
        print(f"{name}:")
        for metric in ("snapshot_ms", "rollback_ms", "snapshot_plus_rollback_ms"):
            summary = values[metric]
            print(f"  {metric}: p50={summary['p50']:.3f}, p95={summary['p95']:.3f}, "
                  f"min={summary['min']:.3f}, max={summary['max']:.3f}")
        print(f"  retained snapshot bytes (logical): {values['storage']['logical_file_bytes']}")
    print("Every restoration verified. Caches were not flushed; no universal latency claim.")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Full report: {args.json.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
