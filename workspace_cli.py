"""Explicit checkpoints around foreground commands in an owned workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Sequence

from snapshot_daemon import SnapshotError, WorkspaceSnapshotter


def run_checkpointed(snapshotter: WorkspaceSnapshotter, command: Sequence[str],
                     label: str = "", *, quiet: bool = False) -> dict:
    """Checkpoint, run, and restore on failure; preserve the command exit code.

    The command must not leave background writers behind. The store lock only
    serializes cooperating clients. stdout/stderr remain attached to the caller.
    """
    if not command:
        raise ValueError("A command is required")
    with snapshotter.transaction():
        checkpoint = snapshotter.create_snapshot(label or "before: " + " ".join(command))
        if not quiet:
            print(f"Checkpoint: {checkpoint}", file=sys.stderr, flush=True)
        completed = subprocess.run(list(command), cwd=snapshotter.workspace, check=False)
        restored = completed.returncode != 0
        if restored:
            snapshotter.rollback(checkpoint)
            if not quiet:
                print(f"Command failed ({completed.returncode}); restored {checkpoint}",
                      file=sys.stderr, flush=True)
        return {"snapshot_id": checkpoint, "returncode": completed.returncode,
                "restored": restored}


def _create_workspace(path: Path) -> None:
    # Check ancestors before mkdir; resolve() would conceal links.
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise SnapshotError(f"Symlink/reparse workspace path is unsupported: {candidate}")
    path.mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verified checkpoints for agent workspaces")
    parser.add_argument("--workspace", default="./workspace", help="Owned workspace directory")
    parser.add_argument("--snapshot-dir", help="Store outside workspace (default: PATH.snapshots)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="Create the workspace and an initial checkpoint")
    snapshot = sub.add_parser("snapshot", help="Checkpoint an idle workspace")
    snapshot.add_argument("--label", default="")
    sub.add_parser("list", help="List checkpoints as JSON")
    restore = sub.add_parser("restore", help="Restore a checkpoint; requires no active writers")
    restore.add_argument("snapshot_id")
    verify = sub.add_parser("verify", help="Validate a checkpoint and its stored contents")
    verify.add_argument("snapshot_id")
    sub.add_parser("stats", help="Show content-store usage")
    prune = sub.add_parser("prune", help="Keep newest N checkpoints and collect unused data")
    prune.add_argument("--keep", type=int, required=True)
    run = sub.add_parser("run", help="Checkpoint, run, restore on nonzero exit")
    run.add_argument("--label", default="")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="Use -- before the executable and its arguments")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            _create_workspace(Path(args.workspace))
        if args.action == "prune" and args.keep < 0:
            parser.error("--keep must be nonnegative")
        command = getattr(args, "command", [])
        if command and command[0] == "--":
            command = command[1:]
        if args.action == "run" and not command:
            parser.error("run requires a command after --")
        snap = WorkspaceSnapshotter(args.workspace, args.snapshot_dir)
        if args.action == "init":
            result = {"snapshot_id": snap.create_snapshot("initial_baseline")}
        elif args.action == "snapshot":
            result = {"snapshot_id": snap.create_snapshot(args.label)}
        elif args.action == "list":
            result = snap.list_snapshots()
        elif args.action == "restore":
            snap.rollback(args.snapshot_id)
            result = {"restored": args.snapshot_id}
        elif args.action == "verify":
            result = snap.verify_snapshot(args.snapshot_id)
        elif args.action == "stats":
            result = snap.storage_stats()
        elif args.action == "prune":
            snap.cleanup_old_snapshots(args.keep)
            result = snap.storage_stats()
        else:
            result = run_checkpointed(snap, command, args.label)
            code = result["returncode"]
            return code if code >= 0 else 128 - code
        print(json.dumps(result, indent=2))
        return 0
    except (SnapshotError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Checkpoint retained; stop all writers before restoring.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
