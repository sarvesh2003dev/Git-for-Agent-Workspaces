"""Compatibility CLI. The after-write watcher has been retired.

Filesystem notifications cannot implement pre-action checkpoints. Use:
python watcher.py --workspace ./workspace run -- python action.py
"""
from workspace_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
