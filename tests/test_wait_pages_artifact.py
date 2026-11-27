import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("wait_pages_artifact", Path(__file__).parents[1] / "scripts" / "wait_pages_artifact.py")
waiter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waiter)


class Clock:
    def __init__(self):
        self.value = 0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def artifact(**updates):
    return {"id":123, "name":"github-pages", "expired":False, "size_in_bytes":1000, "workflow_run":{"id":456}, **updates}


class PagesArtifactTests(unittest.TestCase):
    def wait(self, fetcher, timeout=120):
        self.clock = Clock()
        self.messages = []
        return waiter.wait_for_pages_artifact("owner/repository", "456", timeout_seconds=timeout,
                                             fetcher=fetcher, clock=self.clock.now, sleeper=self.clock.sleep,
                                             emit=self.messages.append)

    def test_visible_artifact_still_waits_ten_seconds_after_upload(self):
        calls = []
        result = self.wait(lambda *args: calls.append(args) or [artifact()])
        self.assertEqual(result, 123)
        self.assertEqual(self.clock.value, 10)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(self.messages[-1])["pages_artifact"], "visible")

    def test_metadata_visibility_race_is_retried(self):
        responses = iter([[], [], [artifact()]])
        self.assertEqual(self.wait(lambda *args: next(responses)), 123)
        self.assertEqual(self.clock.sleeps, [10, 10])

    def test_wrong_expired_empty_and_other_run_artifacts_do_not_succeed(self):
        bad = [artifact(name="unrelated"), artifact(expired=True), artifact(size_in_bytes=0), artifact(workflow_run={"id":999})]
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            self.wait(lambda *args: bad)
        self.assertEqual(self.clock.value, 120)

    def test_api_errors_time_out_without_response_or_secret_logging(self):
        def failed(*args):
            raise RuntimeError("sensitive provider response")
        with self.assertRaisesRegex(RuntimeError, "saved catalog state is unchanged"):
            self.wait(failed)
        self.assertNotIn("sensitive", " ".join(self.messages))
        self.assertEqual(self.clock.value, 120)

    def test_multiple_matching_artifacts_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Multiple active"):
            self.wait(lambda *args: [artifact(), artifact(id=124)])

    def test_fetch_uses_only_fixed_github_current_run_metadata_endpoint(self):
        response = types.SimpleNamespace(returncode=0, stdout=json.dumps({"artifacts":[artifact(archive_download_url="https://malicious.invalid/file")], "next":"https://malicious.invalid/next"}))
        with patch.object(waiter.subprocess, "run", return_value=response) as run:
            rows = waiter.fetch_artifacts("owner/repository", "456", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(run.call_args.args[0], ["gh", "api", "--hostname", "github.com", "--method", "GET", "repos/owner/repository/actions/runs/456/artifacts?per_page=100"])
        self.assertEqual(run.call_args.kwargs["env"]["GH_HOST"], "github.com")
        self.assertEqual(run.call_count, 1)

    def test_external_host_or_path_context_is_rejected_before_any_request(self):
        for repository, run_id in [("https://malicious.invalid/x", "456"), ("owner/../repo", "456"), ("owner/repo", "456?host=other")]:
            with self.subTest(repository=repository), patch.object(waiter.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    waiter.fetch_artifacts(repository, run_id, 10)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
