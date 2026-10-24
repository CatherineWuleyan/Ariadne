"""Run identification: what produced this artifact, and can we prove it?"""
from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ariadne.util import file_digest, git_state, map_ordered, parallel_workers


class FileDigestTests(unittest.TestCase):
    def test_matches_hashlib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_bytes(b'{"nodes": []}\n')
            self.assertEqual(
                file_digest(path),
                hashlib.sha256(b'{"nodes": []}\n').hexdigest(),
            )

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(file_digest(Path(tmp) / "absent.json"))


class ParallelMapTests(unittest.TestCase):
    def test_results_follow_input_order_not_completion_order(self) -> None:
        def finish_backwards(value: int) -> int:
            # The first item is the slowest, so completion order is reversed.
            time.sleep(0.01 * (4 - value))
            return value * 10

        results, failures = map_ordered(finish_backwards, [1, 2, 3], workers=3)
        self.assertEqual(results, [10, 20, 30])
        self.assertEqual(failures, [])

    def test_one_failure_does_not_drop_the_rest(self) -> None:
        def maybe_fail(value: int) -> int:
            if value == 2:
                raise ValueError("boom")
            return value

        results, failures = map_ordered(maybe_fail, [1, 2, 3], workers=3)
        self.assertEqual(results, [1, 3])
        self.assertEqual(len(failures), 1)
        item, exc = failures[0]
        self.assertEqual(item, 2)
        self.assertIsInstance(exc, ValueError)

    def test_single_worker_runs_sequentially(self) -> None:
        seen: list[int] = []
        map_ordered(lambda value: seen.append(threading.current_thread().ident), [1, 2, 3], workers=1)
        self.assertEqual(len(set(seen)), 1)

    def test_workers_actually_overlap(self) -> None:
        barrier = threading.Barrier(2, timeout=5)

        def wait_for_peer(_: int) -> str:
            # Deadlocks (and fails) unless both items are in flight at once.
            barrier.wait()
            return "ok"

        results, failures = map_ordered(wait_for_peer, [1, 2], workers=2)
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(failures, [])

    def test_worker_count_is_clamped_to_batch_size(self) -> None:
        self.assertEqual(parallel_workers(8, ceiling=2), 2)
        self.assertEqual(parallel_workers(0), 1)


class GitStateTests(unittest.TestCase):
    def test_shape_is_stable_even_outside_a_checkout(self) -> None:
        # A tarball install has no .git; callers must not have to special-case it.
        available = git_state()
        self.assertEqual(
            set(available), {"available", "commit", "dirty", "describe"}
        )
        if not available["available"]:
            self.assertIsNone(available["commit"])
            return
        self.assertRegex(available["commit"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(available["dirty"], bool)

    def test_missing_directory_is_reported_not_raised(self) -> None:
        state = git_state(Path("/nonexistent/ariadne-checkout"))
        self.assertFalse(state["available"])
        self.assertIsNone(state["commit"])


if __name__ == "__main__":
    unittest.main()


class InputDigestTests(unittest.TestCase):
    def test_relative_and_absolute_paths_are_hashed_the_same(self) -> None:
        from ariadne.full import _input_digests

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            graph = run_dir / "01_retrieval" / "graph.json"
            graph.parent.mkdir(parents=True)
            graph.write_text('{"nodes": []}', encoding="utf-8")
            text = run_dir / "01_retrieval" / "ocr.md"
            text.write_text("# paper", encoding="utf-8")

            relative = _input_digests(
                [{"lkm_id": "p1", "graph_path": "01_retrieval/graph.json",
                  "ocr_path": "01_retrieval/ocr.md"}], run_dir)
            absolute = _input_digests(
                [{"lkm_id": "p1", "graph_path": str(graph), "ocr_path": str(text)}], run_dir)
            expected_graph_digest = file_digest(graph)

        self.assertEqual(relative, absolute)
        self.assertEqual(relative["papers"]["p1"]["graph_sha256"], expected_graph_digest)
        self.assertEqual(len(relative["corpus_sha256"]), 64)

    def test_missing_input_is_reported_as_none(self) -> None:
        from ariadne.full import _input_digests

        with tempfile.TemporaryDirectory() as tmp:
            digests = _input_digests([{"lkm_id": "p1", "graph_path": "absent.json"}], Path(tmp))
        self.assertIsNone(digests["papers"]["p1"]["graph_sha256"])
