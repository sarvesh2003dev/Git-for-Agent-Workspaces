# Git-for-Agent-Workspaces
Git-for-Agent-Workspaces (Sub-Second Filesystem Rollback)A lightweight daemon using overlayfs or btrfs that snapshots only the /workspace directory before every major agent tool call. If the agent breaks the build, your daemon reverts the filesystem in &lt;50 milliseconds without touching the VM's memory state.
