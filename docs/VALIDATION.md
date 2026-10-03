# Observed validation — 2026-10-03

These are reproducible local observations, not a cloud-service comparison or an
under-50-ms guarantee. The Windows and Linux benchmark invocations ran sequentially
on one host. Neither run controlled other host activity or flushed OS caches.

## Correctness

- Windows 11 / CPython 3.13.2: 40 tests, 37 passed, 3 POSIX-only skips.
- Ubuntu on WSL2 / CPython 3.14.4: all 40 tests passed.
- Tests create their data in temporary directories (NTFS on Windows, tmpfs on WSL).
- Tests cover actual process death between directory renames, multi-process
  checkpoint publication, corrupted/missing objects, symlink escapes, metadata,
  read-only directories, retention, and command exit codes.
- The demo independently verifies the restored fixture and reruns its checks.
- A Python wheel builds successfully. The CI configuration also targets Python
  3.10 and 3.13 on GitHub-hosted Linux and Windows; the local runs above do not
  establish those remote jobs' outcomes.

## Measured workload

100 regular files of 4 KiB each (400 KiB total), 11 directories, seed 42.
Each sample edits 25 files, deletes 25, creates 26 files, and replaces an empty
directory with a file. Five snapshots are retained per backend. Every restoration
is independently checked for paths, bytes, basic modes, and modification times.

| Environment | Backend | Checkpoint p50 / p95 (ms) | Restore p50 / p95 (ms) | Retained logical file bytes |
| --- | --- | --- | --- | --- |
| Windows / NTFS | Content store | 228.506 / 243.150 | 243.228 / 250.435 | 527,973 |
| Windows / NTFS | Directory copy | 52.616 / 62.421 | 61.404 / 63.436 | 2,048,000 |
| WSL2 / tmpfs | Content store | 18.623 / 20.048 | 12.685 / 14.549 | 527,938 |
| WSL2 / tmpfs | Directory copy | 3.820 / 4.405 | 4.018 / 7.587 | 2,048,000 |

**The content store is slower on this workload.** It performs integrity hashing,
fsync calls, manifest validation, and recovery-journal work that the simple copy
baseline does not. It uses about 74% fewer retained logical bytes here, including
its manifests. Repeated identical checkpoints favor deduplication; this is not a
general storage-saving estimate for arbitrary workloads.

The WSL tmpfs results are memory-backed and are not evidence of persistent-disk
performance. Do not use the difference between rows as a Windows/Linux comparison.
Five samples are insufficient to characterize production tail latency.

Raw samples and context:
[Windows](results/windows-ntfs.json) · [Linux](results/linux-wsl-tmpfs.json).
The Windows temporary-directory username is redacted; measurement values are not.

## Reproduce

```bash
python -m unittest discover -s tests -v
python demo.py
python benchmark.py --files 100 --size-kib 4 --iterations 5 --seed 42 --json benchmark-results.json
```

Use `--temp-parent PATH` to select a filesystem to measure. Record the reported
filesystem and cache conditions. A claim about Dedalus Machines still requires an
actual deployment and measurement there.
