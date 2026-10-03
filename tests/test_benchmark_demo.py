"""Verify the public demonstrations reject incorrect or merely fast recovery."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark
import demo


class BenchmarkDemoTests(unittest.TestCase):
    def test_demo_recovers_real_command_damage(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = demo.run_demo(temp_parent=temporary)
            self.assertEqual(result["initial_checks_exit_code"], 0)
            self.assertEqual(result["mutation_command_exit_code"], 0)
            self.assertNotEqual(result["checks_after_mutation_exit_code"], 0)
            self.assertEqual(result["checks_after_rollback_exit_code"], 0)
            self.assertTrue(result["restoration_verified"])
            self.assertFalse(result["uses_llm"])
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_benchmark_verifies_both_backends_and_writes_raw_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = benchmark.main(["--files", "4", "--size-kib", "1",
                                            "--iterations", "2", "--seed", "9",
                                            "--temp-parent", temporary, "--json", str(output)])
            self.assertEqual(exit_code, 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["workload"]["total_file_bytes"], 4096)
            self.assertEqual(set(result["results"]), {"content_store", "full_copy"})
            for backend in result["results"].values():
                self.assertEqual(len(backend["raw_samples"]), 2)
                self.assertTrue(all(sample["restoration_verified"] for sample in backend["raw_samples"]))
                self.assertLessEqual(backend["rollback_ms"]["p50"], backend["rollback_ms"]["p95"])
                self.assertEqual(backend["storage"]["snapshot_count"], 2)
            self.assertEqual(list(Path(temporary).iterdir()), [output])

    def test_benchmark_rejects_engine_claiming_success_without_restoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("benchmark.WorkspaceSnapshotter") as engine:
                engine.return_value.rollback.return_value = True
                with self.assertRaisesRegex(AssertionError, "Restore verification failed"):
                    benchmark.run_benchmark(files=2, size_kib=1, iterations=1, temp_parent=temporary)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_verifier_detects_corruption_even_when_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "data.bin"
            path.write_bytes(b"good")
            expected = benchmark.tree_inventory(root)
            mtime_ns = path.stat().st_mtime_ns
            path.write_bytes(b"evil")
            os.utime(path, ns=(mtime_ns, mtime_ns))
            with self.assertRaisesRegex(AssertionError, "Restore verification failed"):
                benchmark.assert_restored(root, expected)


if __name__ == "__main__":
    unittest.main()
