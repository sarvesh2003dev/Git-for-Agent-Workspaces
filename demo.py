#!/usr/bin/env python3
"""Scripted recovery demo: checkpoint, run a bad command, validate, restore.

No LLM is used. All subprocesses and file mutations run inside a disposable
fixture. A successful result requires exact recovery and passing fixture checks.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from benchmark import assert_restored, tree_inventory
from snapshot_daemon import WorkspaceSnapshotter


CHECK_COMMAND = """\
import json
from pathlib import Path
from app import add
assert add(2, 3) == 5, 'addition behavior changed'
assert json.loads(Path('config.json').read_text(encoding='utf-8')) == {'enabled': True}
assert Path('state.txt').is_file()
assert Path('state.txt').read_text(encoding='utf-8') == 'ready'
assert Path('assets').is_dir()
assert Path('assets/info.txt').read_text(encoding='utf-8') == 'original asset'
assert Path('empty_directory').is_dir()
assert not Path('unexpected.txt').exists()
print('fixture checks passed')
"""

MUTATION_COMMAND = """\
import shutil
from pathlib import Path
Path('app.py').write_text('def add(a, b):\\n    return a - b\\n', encoding='utf-8')
Path('config.json').unlink()
Path('unexpected.txt').write_text('unwanted new file', encoding='utf-8')
Path('state.txt').unlink()
Path('state.txt').mkdir()
Path('state.txt/nested.txt').write_text('file replaced by directory', encoding='utf-8')
shutil.rmtree('assets')
Path('assets').write_text('directory replaced by file', encoding='utf-8')
Path('empty_directory').rmdir()
print('scripted mutations completed')
"""


def _run_python(workspace: Path, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", "-c", source], cwd=workspace,
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


def run_demo(*, temp_parent: str | Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="workspace-demo-", dir=temp_parent) as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (workspace / "config.json").write_text('{"enabled": true}\n', encoding="utf-8")
        (workspace / "state.txt").write_text("ready", encoding="utf-8")
        (workspace / "assets").mkdir()
        (workspace / "assets/info.txt").write_text("original asset", encoding="utf-8")
        (workspace / "empty_directory").mkdir()
        before = _run_python(workspace, CHECK_COMMAND)
        if before.returncode != 0:
            raise AssertionError(f"Initial fixture is invalid: {before.stderr}")
        expected = tree_inventory(workspace)
        snapshotter = WorkspaceSnapshotter(str(workspace), str(root / "snapshots"))
        start = time.perf_counter_ns()
        checkpoint = snapshotter.create_snapshot("before_scripted_tool_action")
        snapshot_ms = (time.perf_counter_ns() - start) / 1_000_000
        snapshotter.verify_snapshot(checkpoint)

        action = _run_python(workspace, MUTATION_COMMAND)
        if action.returncode != 0:
            raise AssertionError(f"Mutation command did not complete: {action.stderr}")
        failed = _run_python(workspace, CHECK_COMMAND)
        if failed.returncode == 0 or "addition behavior changed" not in failed.stderr:
            raise AssertionError(f"Expected behavioral failure was not demonstrated: {failed.stderr}")

        start = time.perf_counter_ns()
        if snapshotter.rollback(checkpoint) is not True:
            raise AssertionError("Rollback did not report success")
        rollback_ms = (time.perf_counter_ns() - start) / 1_000_000
        assert_restored(workspace, expected)
        recovered = _run_python(workspace, CHECK_COMMAND)
        if recovered.returncode != 0:
            raise AssertionError(f"Recovered fixture failed its checks: {recovered.stderr}")
        return {
            "description": "Scripted filesystem recovery; no LLM or Dedalus integration",
            "uses_llm": False, "checkpoint_before_action": True,
            "initial_checks_exit_code": before.returncode,
            "mutation_command_exit_code": action.returncode,
            "checks_after_mutation_exit_code": failed.returncode,
            "checks_after_mutation_stderr": failed.stderr.strip(),
            "checks_after_rollback_exit_code": recovered.returncode,
            "restoration_verified": True,
            "verified_properties": ["relative paths", "file contents", "empty directories",
                                    "file and directory modes", "mtime_ns",
                                    "file-to-directory and directory-to-file recovery"],
            "snapshot_ms": snapshot_ms, "rollback_ms": rollback_ms,
            "timing_scope": "One local observation; includes engine work, excludes independent verification",
            "scope": "Filesystem entries only; cannot undo memory, network, or external side effects",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-parent", type=Path, help="Existing directory for disposable fixture")
    parser.add_argument("--json", type=Path, help="Save the verified demo result")
    args = parser.parse_args(argv)
    result = run_demo(temp_parent=args.temp_parent)
    print("Scripted filesystem recovery demo; no LLM is used.")
    print("Initial checks passed. The child command edited, deleted, added, and replaced fixture entries.")
    print("Checks failed after the command. Rollback restored the pre-command checkpoint.")
    print("Exact inventory verified; the original checks passed again.")
    print(f"Observed snapshot: {result['snapshot_ms']:.3f} ms; rollback: {result['rollback_ms']:.3f} ms.")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Full report: {args.json.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
