import sys
import unittest
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PY_DIR = REPO_ROOT / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from gui import chart_render_pool  # noqa: E402


class _FakeFuture:
    def __init__(self):
        self.callback = None
        self.value = None
        self.error = None

    def add_done_callback(self, callback):
        self.callback = callback

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value

    def complete(self, value):
        self.value = value
        self.callback(self)

    def fail(self, error):
        self.error = error
        self.callback(self)


class _FakeExecutor:
    def __init__(self):
        self.submissions = []
        self.futures = []

    def submit(self, _function, request):
        future = _FakeFuture()
        self.submissions.append(request)
        self.futures.append(future)
        return future

    def shutdown(self, **_kwargs):
        return


class ChartRenderPoolTests(unittest.TestCase):
    def test_only_newest_pending_zone_request_is_submitted(self):
        executor = _FakeExecutor()
        with patch.object(chart_render_pool, "ProcessPoolExecutor", return_value=executor):
            pool = chart_render_pool.ChartRenderPool()
            try:
                pool.submit(1, {"value": "running"}, request_id=1)
                pool.submit(1, {"value": "obsolete"}, request_id=2)
                pool.submit(2, {"value": "newest"}, request_id=3)

                self.assertEqual(len(executor.submissions), 1)
                executor.futures[0].complete({"png": b"first"})
                self.assertEqual(pool.poll_results(), [(1, {"png": b"first", "request_id": 1})])
                self.assertEqual(executor.submissions[1], {"value": "newest"})
                executor.futures[1].complete({"png": b"newest"})
                self.assertEqual(pool.poll_results(), [(2, {"png": b"newest", "request_id": 3})])
            finally:
                pool.shutdown()

    def test_broken_worker_restarts_with_latest_pending_request(self):
        first = _FakeExecutor()
        replacement = _FakeExecutor()
        with patch.object(
            chart_render_pool,
            "ProcessPoolExecutor",
            side_effect=(first, replacement),
        ):
            pool = chart_render_pool.ChartRenderPool()
            try:
                pool.submit(1, {"value": "running"}, request_id=1)
                pool.submit(2, {"value": "pending"}, request_id=2)
                first.futures[0].fail(BrokenProcessPool("worker exited"))

                self.assertTrue(pool.is_broken())
                pool.restart()
                self.assertEqual(replacement.submissions, [{"value": "pending"}])
                replacement.futures[0].complete({"png": b"recovered"})
                self.assertEqual(
                    pool.poll_results(),
                    [(2, {"png": b"recovered", "request_id": 2})],
                )
            finally:
                pool.shutdown()


if __name__ == "__main__":
    unittest.main()
