"""ci_failures harvester: log parsing, classification, harvest flow, self-checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidateminer.github import GitHubClient
from candidateminer.harvesters import ci_failures
from candidateminer.harvesters.base import HarvestDefect

_LOG = """\
2026-06-15T13:20:00.0000000Z > Task :spring-core:compileJava
2026-06-15T13:22:57.2289946Z RetryTemplateTests > TimeoutTests > retryableWithTimeoutExceededAfterSecondRetry() FAILED
2026-06-15T13:22:57.2329473Z     java.lang.AssertionError at RetryTemplateTests.java:802
2026-06-15T13:23:00.0000000Z 5089 tests completed, 1 failed, 12 skipped
2026-06-15T13:23:01.0000000Z > Task :spring-core:java21Test FAILED
2026-06-15T13:24:00.0000000Z https://ge.spring.io/s/abc123xyz
"""

_LOG_PARAMETERISED = """\
2026-06-15T13:35:13.9520856Z RestClientProxyRegistryIntegrationTests > basic(Class) > [1] configClass = class a.ListingConfig FAILED
2026-06-15T13:35:13.9541912Z     java.net.BindException at RestClientProxyRegistryIntegrationTests.java:58
2026-06-15T13:35:13.9592509Z RestClientProxyRegistryIntegrationTests > basic(Class) > [2] configClass = class a.DetectConfig FAILED
2026-06-15T13:35:13.9601711Z     java.net.BindException at RestClientProxyRegistryIntegrationTests.java:58
2026-06-15T13:36:00.0000000Z 3878 tests completed, 5 failed
2026-06-15T13:36:01.0000000Z > Task :spring-web:test FAILED
"""

_LOG_ANTORA = """\
2026-07-03T10:22:40.6160057Z > Task :framework-docs:antora FAILED
2026-07-03T10:22:40.7244762Z BUILD FAILED in 1m 4s
"""


class ParseLogTests(unittest.TestCase):
    def test_extracts_nested_test_with_cause_and_line(self):
        parsed = ci_failures.parse_log(_LOG)
        self.assertEqual(parsed["failed_tasks"], [":spring-core:java21Test"])
        self.assertEqual(parsed["scan_url"], "https://ge.spring.io/s/abc123xyz")
        self.assertEqual(
            parsed["test_summaries"], [{"completed": 5089, "failed": 1, "skipped": 12}]
        )
        (test,) = parsed["failing_tests"]
        self.assertEqual(test["test_class"], "RetryTemplateTests")
        self.assertEqual(test["nested_classes"], ["TimeoutTests"])
        self.assertEqual(test["method"], "retryableWithTimeoutExceededAfterSecondRetry")
        self.assertEqual(test["cause"], "java.lang.AssertionError")
        self.assertEqual((test["at_file"], test["at_line"]), ("RetryTemplateTests.java", 802))

    def test_parameterised_invocations_are_kept_separately(self):
        """The corpus records what happened; collapsing to identity is the miner's job."""
        parsed = ci_failures.parse_log(_LOG_PARAMETERISED)
        self.assertEqual(len(parsed["failing_tests"]), 2)
        methods = {t["method"] for t in parsed["failing_tests"]}
        self.assertEqual(methods, {"basic"})

    def test_task_failed_lines_are_not_tests(self):
        parsed = ci_failures.parse_log(_LOG_ANTORA)
        self.assertEqual(parsed["failed_tasks"], [":framework-docs:antora"])
        self.assertEqual(parsed["failing_tests"], [])

    def test_deterministic(self):
        self.assertEqual(ci_failures.parse_log(_LOG), ci_failures.parse_log(_LOG))


class ClassifyTests(unittest.TestCase):
    def test_test_task_variants(self):
        for task in (":spring-core:test", ":spring-core:java21Test", ":x:java25Test"):
            self.assertEqual(ci_failures._classify([task], []), "test", task)

    def test_non_test_kinds(self):
        self.assertEqual(ci_failures._classify([":framework-api:javadoc"], []), "javadoc")
        self.assertEqual(ci_failures._classify([":framework-docs:antora"], []), "antora")
        self.assertEqual(ci_failures._classify([":checkstyleNohttp"], []), "nohttp")
        self.assertEqual(ci_failures._classify([":spring-core:weird"], []), "other")
        self.assertEqual(ci_failures._classify([], []), "unknown")

    def test_failing_tests_dominate(self):
        tests = [{"test_class": "X", "method": "y"}]
        self.assertEqual(ci_failures._classify([":framework-api:javadoc"], tests), "test")


class _FakeClient:
    """Enough of GitHubClient for a harvest: canned runs, jobs, and logs.

    Deliberately has no ``get``: everything list-shaped must go through
    ``paginate``, so a regression back to a single-page ``get`` fails loudly.
    Jobs are served one page per dict, mirroring the real endpoint's envelope.
    """

    def __init__(self, runs, jobs_by_run, logs_by_job, *, jobs_page_size=100):
        self.runs = runs
        self.jobs_by_run = jobs_by_run
        self.logs_by_job = logs_by_job
        self.jobs_page_size = jobs_page_size
        self.truncated = []

    def paginate(self, path, **_):
        if "/workflows/" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            yield {"workflow_runs": self.runs.get(workflow, [])}
            return
        run_id = int(path.split("/runs/")[1].split("/")[0])
        jobs = self.jobs_by_run.get(run_id, [])
        size = self.jobs_page_size
        for start in range(0, max(len(jobs), 1), size):
            yield {"jobs": jobs[start : start + size]}

    def get_text(self, path):
        job_id = int(path.split("/jobs/")[1].split("/")[0])
        return self.logs_by_job.get(job_id)


def _run(run_id, conclusion, date="2026-06-15T13:00:00Z"):
    return {
        "id": run_id,
        "conclusion": conclusion,
        "run_started_at": date,
        "head_sha": "cafe",
        "html_url": f"https://example.test/runs/{run_id}",
    }


class HarvestTests(unittest.TestCase):
    def test_end_to_end_classifies_and_extracts(self):
        client = _FakeClient(
            runs={
                "ci.yml": [_run(1, "success"), _run(2, "failure")],
                "build-and-deploy-snapshot.yml": [_run(3, "failure"), _run(4, "success")],
            },
            jobs_by_run={
                2: [{"id": 20, "name": "Linux | Java 25", "conclusion": "failure"}],
                3: [{"id": 30, "name": "Build", "conclusion": "failure"}],
            },
            logs_by_job={20: _LOG, 30: _LOG_ANTORA},
        )
        records, stats = ci_failures.harvest(client, "o/r", limit=10)
        self.assertEqual(stats.runs_listed, 4)
        self.assertEqual(stats.failures_seen, 2)
        self.assertEqual(stats.logs_fetched, 2)
        by_class = {r.run_id: r.classification for r in records}
        self.assertEqual(by_class, {2: "test", 3: "antora"})

    def test_expired_log_is_recorded_not_fatal(self):
        client = _FakeClient(
            runs={"ci.yml": [_run(2, "failure")], "build-and-deploy-snapshot.yml": []},
            jobs_by_run={2: [{"id": 20, "name": "j", "conclusion": "failure"}]},
            logs_by_job={},  # get_text -> None
        )
        records, stats = ci_failures.harvest(client, "o/r", limit=10)
        (record,) = records
        self.assertFalse(record.log_available)
        self.assertEqual(record.classification, "unknown")
        self.assertEqual(stats.logs_unavailable, 1)

    def test_parser_that_reads_nothing_is_a_defect(self):
        """3+ logs fetched, zero failed tasks parsed => the parser broke."""
        garbage = "no gradle output here at all\n"
        client = _FakeClient(
            runs={
                "ci.yml": [_run(1, "failure"), _run(2, "failure"), _run(3, "failure")],
                "build-and-deploy-snapshot.yml": [],
            },
            jobs_by_run={
                i: [{"id": i * 10, "name": "j", "conclusion": "failure"}] for i in (1, 2, 3)
            },
            logs_by_job={10: garbage, 20: garbage, 30: garbage},
        )
        with self.assertRaises(HarvestDefect):
            ci_failures.harvest(client, "o/r", limit=10)

    def test_failed_job_beyond_the_first_jobs_page_is_still_found(self):
        """The regression: a matrix run whose sole failed job sat past page one
        was recorded as log-expired because the jobs lookup read one page."""
        filler = [{"id": 1000 + i, "name": f"ok-{i}", "conclusion": "success"} for i in range(3)]
        failed = {"id": 20, "name": "Linux | Java 25", "conclusion": "failure"}
        client = _FakeClient(
            runs={"ci.yml": [_run(2, "failure")], "build-and-deploy-snapshot.yml": []},
            jobs_by_run={2: filler + [failed]},
            logs_by_job={20: _LOG},
            jobs_page_size=2,  # the failed job lands on page 2
        )
        records, stats = ci_failures.harvest(client, "o/r", limit=10)
        (record,) = records
        self.assertTrue(record.log_available)
        self.assertEqual(record.classification, "test")
        self.assertEqual(stats.logs_fetched, 1)

    def test_limit_is_per_workflow(self):
        client = _FakeClient(
            runs={
                "ci.yml": [_run(i, "success") for i in range(1, 6)],
                "build-and-deploy-snapshot.yml": [_run(i, "success") for i in range(6, 11)],
            },
            jobs_by_run={},
            logs_by_job={},
        )
        _, stats = ci_failures.harvest(client, "o/r", limit=3)
        self.assertEqual(stats.runs_listed, 6)


class GetTextCacheTests(unittest.TestCase):
    """The cache layer of get_text, exercised without any network."""

    def test_cached_body_and_gone_sentinel(self):
        cache = Path(tempfile.mkdtemp())
        client = GitHubClient(token=None, cache_dir=cache)
        url = "https://api.github.com/repos/o/r/actions/jobs/1/logs"
        client._cache_write_text(url, "the log body")
        self.assertEqual(client.get_text(url), "the log body")

        gone_url = "https://api.github.com/repos/o/r/actions/jobs/2/logs"
        from candidateminer.github import _TEXT_GONE

        client._cache_write_text(gone_url, _TEXT_GONE)
        self.assertIsNone(client.get_text(gone_url))


if __name__ == "__main__":
    unittest.main()
