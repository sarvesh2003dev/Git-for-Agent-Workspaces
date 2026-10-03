# Git for Agent Workspaces

Verified filesystem checkpoints around agent commands. Maintained by **Sarvesh Tamse**.

An agent edits a working project, deletes a configuration file, or leaves a failed
experiment behind. This tool records the workspace **before** the action and
restores that checkpoint if the command exits unsuccessfully.

This is a Python systems prototype with a defined recovery contract and executable
failure tests. It uses content-addressed storage and staged directory replacement.
It does not use OverlayFS or promise a fixed rollback latency.

## Try it

Requires Python 3.10 or newer. No third-party runtime dependencies.

```bash
git clone https://github.com/sarvesh2003dev/Git-for-Agent-Workspaces.git
cd Git-for-Agent-Workspaces
python demo.py
python -m unittest discover -s tests -v
```

The demo runs a real child process that damages a temporary fixture, observes
failure, restores the pre-action checkpoint, checks the recovered tree, and reruns
the original tests. It is a deterministic scripted agent action, not an LLM call.
It leaves your own files alone.

## Use it

Run these commands from **outside** the workspace directory. The workspace must
be owned by this workflow, with no other processes writing to it.

```bash
python workspace_cli.py --workspace ./workspace init
python workspace_cli.py --workspace ./workspace snapshot --label before-edit
python workspace_cli.py --workspace ./workspace list
python workspace_cli.py --workspace ./workspace run -- python action.py
python workspace_cli.py --workspace ./workspace verify SNAPSHOT_ID
python workspace_cli.py --workspace ./workspace restore SNAPSHOT_ID
python workspace_cli.py --workspace ./workspace stats
python workspace_cli.py --workspace ./workspace prune --keep 10
```

Put your project and `action.py` inside the workspace before running the action.
The child command runs with that directory as its current directory. A successful
command keeps its changes. A failed command is restored and its original nonzero
exit code is returned. Recovery errors return exit code 2 and print a diagnostic.
An interrupted invocation retains its checkpoint for manual recovery after all
writers have stopped.

To install the optional command-line entry point:

```bash
python -m pip install .
workspace-checkpoint --workspace ./workspace list
```

The store defaults to a sibling directory, `workspace.snapshots`. Override it
with `--snapshot-dir`; the store and workspace must not contain one another.
All workspace files are included, even ignored files and secrets. Protect the
store with the same access controls as the workspace.

## Python API

```python
from snapshot_daemon import WorkspaceSnapshotter
from workspace_cli import run_checkpointed

snap = WorkspaceSnapshotter("./workspace")  # must already exist
result = run_checkpointed(snap, ["python", "action.py"])
print(result["snapshot_id"], result["returncode"], result["restored"])

# Explicit checkpoints for another tool integration:
with snap.transaction():
    checkpoint = snap.create_snapshot("before-tool")
    # Run your foreground tool here; wait for all writes to finish.
    # On failure:
    snap.rollback(checkpoint)
```

Use the transaction across checkpoint/action/restore when multiple cooperating
clients share a store. The lock does not stop an unrelated editor or agent.

## What is implemented

- Full SHA-256 content addressing; identical bytes share one stored object.
- Fresh content hashing on every checkpoint, without an mtime-only cache.
- Manifests record files, directory structure (including empty directories), and
  basic mode bits and modification times. Snapshot IDs are collision-resistant.
- A cross-process store lock and on-disk manifests avoid stale client inventories.
- Missing or corrupt content fails verification before restoration changes the
  workspace.
- A staged tree and recovery journal protect the previous tree during replacement.
- Symlinks, Windows reparse points, and special files are rejected rather than
  followed. Workspace/store overlap is rejected.
- Retention removes expired checkpoints and reclaims unreferenced content.

See [the design and failure contract](docs/DESIGN.md) for the limits behind these
statements. See the [tests](tests) for executable evidence.

## Measure it

```bash
python benchmark.py --files 200 --size-kib 16 --iterations 10 --seed 42 --json benchmark-results.json
```

The benchmark compares measured checkpoint and restore work against an actual
directory-copy baseline on the same seeded fixture. It checks recovered state
for each sample and records raw samples and latency distributions.

There is no simulated VM baseline and no claimed Dedalus speedup. Timing depends
on file count, bytes, storage, OS, cache state, and integrity checks. Snapshotting
reads the full tree; restoration materializes the full checkpoint. Deduplication
reduces stored bytes for repeated content, not the amount of data a restore must
write.

Measured local results, raw samples, and test environments are in
[VALIDATION.md](docs/VALIDATION.md). The content store was slower than the
copy baseline in that experiment and used less retained storage.

## Scope and limits

This is for an **owned, idle filesystem tree** between foreground actions.

- It is not a sandbox or a security boundary against malicious concurrent writers.
- It does not snapshot RAM, running processes, databases, network calls, or external
  service state. Restoring files cannot undo those side effects.
- Processes with open files, memory mappings, or working directories inside the
  workspace must stop before restoration. The workspace directory is replaced.
- The rename sequence has a brief visibility gap. It is recoverable, not a single
  atomic switch for concurrent readers.
- POSIX basic permissions are tracked; Windows permissions are more limited.
  Ownership, ACLs, extended attributes, access/creation times, hard-link identity, and sparse
  file layout are outside the supported metadata contract.
- Local filesystems only. Network filesystems and sudden power-loss recovery have
  not been validated. Keep independent backups of important data.
- Version 1 stores are not silently upgraded. Use a fresh version 2 store after
  separately preserving any old snapshots.

The original automatic watcher was removed: filesystem notifications arrive
after changes and cannot establish an exact pre-action boundary. `watcher.py`
now delegates to the explicit CLI; `--daemon` is no longer supported.

## Why this problem

Workspace-level recovery is relevant to persistent computers used by coding
agents. The useful question is whether a tool can provide a trustworthy pre-action
checkpoint with acceptable overhead on a specified workload.

This repository investigates that question. It is not affiliated with Dedalus,
has not been benchmarked on Dedalus Machines, and does not claim to fix a verified
deficiency in their platform.

## License

[MIT](LICENSE) — Sarvesh Tamse.
