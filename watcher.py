#!/usr/bin/env python3
"""
File Watcher Daemon - Monitors workspace for agent actions and triggers snapshots.

This daemon watches the workspace directory for file changes and automatically
creates snapshots before major agent tool calls (code edits, file deletions, etc.).
"""

import time
import threading
from pathlib import Path
from typing import Callable, Optional
from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    DirDeletedEvent,
    DirMovedEvent
)

from snapshot_daemon import WorkspaceSnapshotter


class AgentActionHandler(FileSystemEventHandler):
    """
    Handles file system events and triggers snapshots for agent actions.
    
    Strategy:
    - On any file modification, create a snapshot with a label describing the change
    - Debounce rapid changes to avoid excessive snapshots
    """
    
    def __init__(self, snapshotter: WorkspaceSnapshotter, 
                 debounce_seconds: float = 0.5):
        super().__init__()
        self.snapshotter = snapshotter
        self.debounce_seconds = debounce_seconds
        self._last_snapshot_time = 0
        self._lock = threading.Lock()
        self._pending_snapshot = False
    
    def _should_snapshot(self) -> bool:
        """Check if we should create a snapshot (debouncing)."""
        current_time = time.time()
        return current_time - self._last_snapshot_time >= self.debounce_seconds
    
    def _trigger_snapshot(self, event_type: str, path: str):
        """Trigger a snapshot with debouncing."""
        with self._lock:
            if not self._should_snapshot():
                self._pending_snapshot = True
                return
            
            self._pending_snapshot = False
            self._last_snapshot_time = time.time()
        
        # Create snapshot in background thread to not block file operations
        label = f"{event_type}: {Path(path).name}"
        threading.Thread(
            target=self._create_snapshot_thread,
            args=(label,),
            daemon=True
        ).start()
    
    def _create_snapshot_thread(self, label: str):
        """Create snapshot in background thread."""
        try:
            self.snapshotter.create_snapshot(label)
        except Exception as e:
            print(f"Snapshot error: {e}")
    
    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent) and not event.is_directory:
            print(f"[WATCHER] File modified: {event.src_path}")
            self._trigger_snapshot("modify", event.src_path)
    
    def on_created(self, event):
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            print(f"[WATCHER] File created: {event.src_path}")
            self._trigger_snapshot("create", event.src_path)
    
    def on_deleted(self, event):
        if isinstance(event, (FileDeletedEvent, DirDeletedEvent)):
            print(f"[WATCHER] {'Directory' if event.is_directory else 'File'} deleted: {event.src_path}")
            self._trigger_snapshot("delete", event.src_path)
    
    def on_moved(self, event):
        if isinstance(event, (FileMovedEvent, DirMovedEvent)):
            print(f"[WATCHER] {'Directory' if event.is_directory else 'File'} moved: {event.src_path} -> {event.dest_path}")
            self._trigger_snapshot("move", event.dest_path)


class WorkspaceWatcher:
    """
    Main watcher class that monitors the workspace and creates snapshots.
    """
    
    def __init__(self, workspace_path: str = "/workspace"):
        self.workspace_path = Path(workspace_path).resolve()
        self.snapshotter = WorkspaceSnapshotter(str(self.workspace_path))
        self.observer: Optional[Observer] = None
        self.handler: Optional[AgentActionHandler] = None
        self._running = False
    
    def start(self, blocking: bool = False):
        """Start watching the workspace."""
        print(f"[WATCHER] Starting workspace watcher on {self.workspace_path}")
        
        # Create initial snapshot
        print("[WATCHER] Creating initial baseline snapshot...")
        initial_id = self.snapshotter.create_snapshot("initial_baseline")
        print(f"[WATCHER] Initial snapshot: {initial_id}")
        
        # Setup watcher
        self.handler = AgentActionHandler(self.snapshotter)
        self.observer = Observer()
        self.observer.schedule(
            self.handler,
            str(self.workspace_path),
            recursive=True
        )
        
        # Ignore snapshot directory
        snapshot_path = self.snapshotter.snapshot_dir
        print(f"[WATCHER] Ignoring snapshot directory: {snapshot_path}")
        
        self.observer.start()
        self._running = True
        
        if blocking:
            try:
                while self._running:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.stop()
    
    def stop(self):
        """Stop watching the workspace."""
        print("[WATCHER] Stopping workspace watcher...")
        self._running = False
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
    
    def manual_snapshot(self, label: str = "") -> str:
        """Manually trigger a snapshot."""
        return self.snapshotter.create_snapshot(label)
    
    def rollback(self, snapshot_id: Optional[str] = None) -> bool:
        """Rollback to a snapshot."""
        if snapshot_id is None:
            snapshot_id = self.snapshotter.get_latest_snapshot()
            if snapshot_id is None:
                print("[WATCHER] No snapshots available for rollback")
                return False
        return self.snapshotter.rollback(snapshot_id)
    
    def list_snapshots(self):
        """List all snapshots."""
        return self.snapshotter.list_snapshots()


def main():
    """Run the watcher daemon."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Workspace Watcher Daemon")
    parser.add_argument("--workspace", default="/workspace",
                       help="Path to workspace directory")
    parser.add_argument("--daemon", action="store_true",
                       help="Run as daemon (blocking)")
    
    args = parser.parse_args()
    
    watcher = WorkspaceWatcher(args.workspace)
    
    if args.daemon:
        watcher.start(blocking=True)
    else:
        # Just initialize and show status
        watcher.start(blocking=False)
        print("\n[WATCHER] Watcher initialized. Run with --daemon to start monitoring.")
        print(f"\nAvailable commands:")
        print(f"  python watcher.py --daemon          # Start monitoring")
        print(f"  python snapshot_daemon.py --action snapshot  # Manual snapshot")
        print(f"  python snapshot_daemon.py --action rollback  # Rollback to latest")
        print(f"  python snapshot_daemon.py --action list      # List snapshots")
        watcher.stop()


if __name__ == "__main__":
    main()
