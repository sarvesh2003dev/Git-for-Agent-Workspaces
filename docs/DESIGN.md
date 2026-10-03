# Design and failure contract

## Boundary

A checkpoint is a copy of an owned filesystem tree taken while all independent
writers are stopped. A foreground action may then modify that tree. After the
action finishes and all its writers stop, the checkpoint can be restored.

The system intentionally does not infer action boundaries from filesystem events.
The CLI's `run` command makes the boundary explicit and preserves the child exit
code so a caller can distinguish a failed action from a successful one.

A store lock serializes cooperating processes. It does not make a live, changing
directory into a consistent snapshot. A tool that spawns persistent background
writers violates this contract. Database, process, and external side effects need
their own coordination.

## Representation

Each regular file is read and addressed by its complete SHA-256 digest. A snapshot
manifest records paths, content references, directory entries, and basic mode
bits and modification times. Repeated file bytes reuse the same object. Every checkpoint reads fresh
content; modification time alone is not evidence that bytes are unchanged.

Manifests are independently persisted. Clients discover current state under the
shared store lock rather than rewriting an old in-memory inventory. A complete
manifest is the publication point for a checkpoint. Interrupted creation can
leave unused objects, which retention/garbage collection may reclaim.

## Restoration

Restoration validates the manifest and stored content, constructs a sibling
staging tree, and records replacement intent. It moves the current workspace to
a sibling backup before publishing the staged tree. The journal allows a later
operation to recover an interrupted replacement.

The staging tree is on the workspace's filesystem so directory renames do not
cross filesystem boundaries. Old and new trees coexist during restoration;
capacity planning must include staging, backup, and the content store.

This is not a single atomic exchange. The workspace pathname can briefly be
absent between renames. Other processes must not access it during restoration.
Open handles and process working directories do not automatically move to the new
tree. The caller runs from outside the workspace.

A reported restoration failure must be investigated before a tool retries work.
Recovery can restore the previous tree or finish cleanup depending on how far
replacement progressed. Power interruption, unsupported filesystem semantics, and
hardware failure are not covered by a claim of crash-proof storage.

## Paths and permissions

Snapshot and workspace roots cannot contain one another. Paths must stay within
their designated tree. Symbolic links, Windows reparse points, and special files
are unsupported. The store must be private to the application; the checks are
defense in depth, not a substitute for access control.

Basic mode bits are tracked separately from content because identical bytes can
belong to files with different permissions. POSIX ownership, ACLs, extended
attributes, access/creation times, sparse extents, hard-link identity, and Windows security
descriptors are outside the contract. Unsupported link/special-file entries cause
an explicit error; they are not silently omitted.

## Failure matrix

| Event | Expected behavior |
| --- | --- |
| Foreground action exits successfully | Keep changes and retain checkpoint |
| Foreground action exits unsuccessfully | Restore pre-action tree; retain failing exit code |
| Action launch fails | Report error; retain checkpoint |
| User interrupts command | Retain checkpoint; stop writers before manual restoration |
| Stored object missing or corrupt | Fail verification before workspace replacement |
| Tracked file changed into directory | Reconstruct correct type in staging |
| Workspace contains symlink/reparse/special entry | Fail explicitly without following it |
| Two clients use the same store | Serialize; preserve published manifests |
| Checkpoint creation is interrupted | No partial manifest advertised as a checkpoint |
| Replacement is interrupted | Recover using journal and backup on next store operation |
| Retention removes last reference | Reclaim unused content under the lock |

## Performance choices

The implementation chooses verifiable restoration over a fixed latency target.
Snapshotting scans and hashes all files. Restoration writes the full tree and
verifies stored bytes. Hash-based deduplication helps repeated checkpoint storage,
but does not make this a copy-on-write filesystem.

The baseline is a measured full directory copy, not a made-up VM latency. The
benchmark separately reports checkpoint and restoration costs, includes raw
samples, and checks results. It does not establish performance on another
machine, larger workloads, or a cloud provider.

A future OverlayFS or filesystem-native backend should be justified by measured
workloads and a compatible failure contract. Simply changing languages or adding
a daemon would not address correctness.

## Validation

The automated suite covers normal recovery plus corruption, path containment,
file/directory replacement, concurrent clients, interrupted replacement, retention,
and command exit codes. CI exercises Linux and Windows. Capability-dependent tests
may skip when the host cannot create the relevant filesystem object; CI output
makes those skips visible.

The benchmark is a workload experiment, not a replacement for correctness tests.
Real production usage and integration with a persistent VM service remain future
validation work.
