#!/usr/bin/env python3
"""
Initialize the workspace snapshot infrastructure.

This script sets up the initial snapshot system for the workspace.
Run this once before starting the watcher daemon.
"""

from pathlib import Path
from snapshot_daemon import WorkspaceSnapshotter


def main():
    """Initialize the snapshot system."""
    workspace = Path("/workspace")
    
    print("=" * 60)
    print("GIT-FOR-AGENT-WORKSPACES INITIALIZATION")
    print("=" * 60)
    print()
    
    print(f"Workspace: {workspace}")
    print()
    
    # Initialize snapshotter
    print("Initializing snapshot system...")
    snap = WorkspaceSnapshotter(str(workspace))
    
    print(f"Snapshot directory: {snap.snapshot_dir}")
    print()
    
    # Create initial baseline snapshot
    print("Creating initial baseline snapshot...")
    baseline_id = snap.create_snapshot("initial_baseline")
    print(f"Baseline snapshot ID: {baseline_id}")
    print()
    
    # Show status
    snapshots = snap.list_snapshots()
    print(f"Total snapshots: {len(snapshots)}")
    print()
    
    print("=" * 60)
    print("INITIALIZATION COMPLETE")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Start the watcher daemon: python watcher.py --daemon")
    print("  2. Run benchmark: python benchmark.py")
    print("  3. Run demo: python demo.py")
    print()
    print("Manual commands:")
    print("  python snapshot_daemon.py --action snapshot --label 'my_label'")
    print("  python snapshot_daemon.py --action rollback")
    print("  python snapshot_daemon.py --action list")


if __name__ == "__main__":
    main()
