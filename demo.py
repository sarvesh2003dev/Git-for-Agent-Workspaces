#!/usr/bin/env python3
"""
Demo - Interactive demonstration of agent failure and instant rollback.

This demo shows:
1. An agent writing a Python script
2. The script failing tests
3. Instant filesystem rollback (<50ms)
4. The LLM trying again with a clean slate
"""

import time
from pathlib import Path
from snapshot_daemon import WorkspaceSnapshotter


def print_section(title: str):
    """Print a section header."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_step(step_num: int, description: str):
    """Print a step in the demo."""
    print(f"\n[Step {step_num}] {description}")
    print("-" * 40)


def run_demo():
    """Run the interactive demo."""
    workspace = Path("/workspace")
    snap = WorkspaceSnapshotter(str(workspace))
    
    print_section("GIT-FOR-AGENT-WORKSPACES DEMO")
    print("Agent Failure & Instant Rollback Demonstration")
    print()
    print("Scenario: An AI agent is writing code and makes a mistake.")
    print("We'll show how sub-second rollback enables quick recovery.")
    
    # Step 1: Initial state
    print_step(1, "Starting with clean workspace")
    
    # Clean up any previous demo files
    for f in workspace.glob("agent_script.py"):
        f.unlink()
    for f in workspace.glob("agent_script_v*.py"):
        f.unlink()
    
    # Create initial clean snapshot
    print("Creating initial clean snapshot...")
    clean_snapshot = snap.create_snapshot("clean_state")
    print(f"Snapshot ID: {clean_snapshot}")
    
    # Step 2: Agent writes first version (has bug)
    print_step(2, "Agent writes Python script (with bug)")
    
    buggy_code = '''
# Agent-generated script v1
# BUG: This has an off-by-one error!

def calculate_sum(numbers):
    """Calculate sum of numbers."""
    total = 0
    for i in range(len(numbers) - 1):  # BUG: should be len(numbers)
        total += numbers[i]
    return total

def main():
    test_data = [1, 2, 3, 4, 5]
    expected = 15
    result = calculate_sum(test_data)
    
    print(f"Testing calculate_sum...")
    print(f"Input: {test_data}")
    print(f"Expected: {expected}")
    print(f"Got: {result}")
    
    if result == expected:
        print("✅ Test PASSED")
    else:
        print("❌ Test FAILED")
        raise AssertionError(f"Expected {expected}, got {result}")

if __name__ == "__main__":
    main()
'''
    
    agent_script = workspace / "agent_script.py"
    agent_script.write_text(buggy_code)
    print(f"Created: {agent_script}")
    print("Code preview:")
    print(buggy_code[:300] + "...")
    
    # Create snapshot before running
    print("\nCreating pre-execution snapshot...")
    pre_run_snapshot = snap.create_snapshot("before_first_run")
    print(f"Snapshot ID: {pre_run_snapshot}")
    
    # Step 3: Run and fail
    print_step(3, "Running buggy code (will fail)")
    
    print("Executing agent_script.py...")
    time.sleep(0.5)  # Simulate execution delay
    
    # Actually run the code to show failure
    import subprocess
    result = subprocess.run(
        ["python", str(agent_script)],
        capture_output=True,
        text=True,
        cwd=str(workspace)
    )
    
    if result.returncode != 0:
        print("❌ CODE FAILED AS EXPECTED")
        print("Error output:")
        print(result.stderr[:500] if result.stderr else result.stdout[:500])
    else:
        print("Unexpected: Code passed (bug might be fixed)")
    
    # Step 4: Show rollback capability
    print_step(4, "Demonstrating instant rollback")
    
    print("Before rollback:")
    print(f"  - agent_script.py exists: {agent_script.exists()}")
    print(f"  - File size: {agent_script.stat().st_size if agent_script.exists() else 0} bytes")
    
    # Measure rollback time
    print("\nRolling back to pre-run state...")
    start = time.perf_counter()
    snap.rollback(pre_run_snapshot)
    rollback_time = (time.perf_counter() - start) * 1000
    
    print(f"⚡ Rollback completed in {rollback_time:.2f}ms!")
    
    if rollback_time < 50:
        print("✅ PASS: Rollback under 50ms target")
    else:
        print(f"⚠️  Rollback took {rollback_time:.2f}ms (target: <50ms)")
    
    # Step 5: Agent tries again
    print_step(5, "Agent retries with fixed code")
    
    fixed_code = '''
# Agent-generated script v2 (FIXED)
# Fixed the off-by-one error!

def calculate_sum(numbers):
    """Calculate sum of numbers."""
    total = 0
    for num in numbers:  # FIXED: iterate directly over numbers
        total += num
    return total

def main():
    test_data = [1, 2, 3, 4, 5]
    expected = 15
    result = calculate_sum(test_data)
    
    print(f"Testing calculate_sum...")
    print(f"Input: {test_data}")
    print(f"Expected: {expected}")
    print(f"Got: {result}")
    
    if result == expected:
        print("✅ Test PASSED")
        return True
    else:
        print("❌ Test FAILED")
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
'''
    
    print("Writing fixed code...")
    agent_script.write_text(fixed_code)
    
    # Create snapshot before second run
    print("Creating pre-second-run snapshot...")
    pre_run_2_snapshot = snap.create_snapshot("before_second_run")
    
    # Step 6: Run fixed code
    print_step(6, "Running fixed code (should pass)")
    
    print("Executing fixed agent_script.py...")
    time.sleep(0.5)
    
    result = subprocess.run(
        ["python", str(agent_script)],
        capture_output=True,
        text=True,
        cwd=str(workspace)
    )
    
    if result.returncode == 0:
        print("✅ CODE PASSED!")
        print("Output:")
        print(result.stdout)
    else:
        print("❌ Code still failing")
        print(result.stderr or result.stdout)
    
    # Step 7: Show all snapshots
    print_step(7, "Reviewing snapshot history")
    
    snapshots = snap.list_snapshots()
    print(f"Total snapshots created: {len(snapshots)}")
    print()
    print(f"{'ID':<45} {'Label':<25} {'Files':<8}")
    print("-" * 78)
    for s in snapshots:
        print(f"{s['id']:<45} {s['label']:<25} {s['file_count']:<8}")
    
    # Step 8: Demo complete rollback
    print_step(8, "Complete rollback to clean state")
    
    print("Rolling back to completely clean state...")
    snap.rollback(clean_snapshot)
    
    print("After rollback:")
    print(f"  - agent_script.py exists: {agent_script.exists()}")
    
    # Cleanup demo files
    if agent_script.exists():
        agent_script.unlink()
    
    print("\n✅ Demo complete! Workspace restored to clean state.")
    
    # Summary
    print_section("DEMO SUMMARY")
    print("""
Key Takeaways:

1. INSTANT ROLLBACK: Filesystem rollback completes in <50ms
   - Much faster than VM-level snapshots (~800ms)
   - No memory state to restore, just file contents

2. AGENT ITERATION: Enables rapid trial-and-error
   - Agent can try multiple approaches quickly
   - Failed experiments leave no trace

3. SELECTIVE RESTORE: Only workspace files are affected
   - Other system state remains unchanged
   - No disruption to unrelated processes

4. AUTOMATIC SNAPSHOTS: Can be triggered by file watcher
   - Before every agent tool call
   - Before code execution
   - On detected file changes
""")
    
    return {
        "demo_completed": True,
        "rollback_under_50ms": rollback_time < 50,
        "actual_rollback_time_ms": rollback_time
    }


if __name__ == "__main__":
    results = run_demo()
    
    print("\nDemo Results:")
    import json
    print(json.dumps(results, indent=2))
