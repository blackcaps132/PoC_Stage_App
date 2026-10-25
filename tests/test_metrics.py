"""Check KPI units, empty runs, percentiles and resume accounting."""

import unittest

from src.metrics import build_kpis, duration_summary


def decision(milliseconds, published=False):
    return {
        "decided_in_ms": milliseconds,
        "status": "PUBLISHED" if published else "NO_ACTION",
        "actions": ["action"] if published else [],
        "model_calls": 1,
        "tool_calls": 3,
        "reason": "example",
    }


class MetricsTests(unittest.TestCase):
    def test_empty_run_has_no_latency_or_throughput(self):
        report = build_kpis([], [], {}, 0, 0, 60)
        self.assertIsNone(report["arrival_to_decision_ms"]["mean"])
        self.assertIsNone(report["decisions_per_wall_second"])
        self.assertEqual(report["total_decision_processing_seconds"], 0)
        self.assertFalse(report["replay_complete"])

    def test_percentiles_use_nearest_rank(self):
        result = duration_summary(list(range(1, 101)))
        self.assertEqual(result, {"mean": 50.5, "p50": 50, "p95": 95, "p99": 99, "max": 100})

    def test_processing_time_and_wall_time_have_distinct_meanings(self):
        sessions = [
            {
                "status": "completed",
                "elapsed_seconds": 10,
                "new_decisions": 2,
                "decision_commits": [{"through_commit_ms": 1200}, {"through_commit_ms": 2300}],
            }
        ]
        report = build_kpis(
            [decision(1000, True), decision(2000)], sessions, {"timeout": 1}, 5, 5, 1.5
        )
        self.assertEqual(report["total_decision_processing_seconds"], 3)
        self.assertEqual(report["total_decision_through_commit_seconds"], 3.5)
        self.assertEqual(report["replay_wall_seconds"], 10)
        self.assertEqual(report["decisions_per_wall_second"], 0.2)
        self.assertEqual(report["decisions_over_budget"], 1)
        self.assertEqual(report["published_decisions"], 1)
        self.assertEqual(report["no_action_decisions"], 1)
        self.assertEqual(report["actions"], 1)
        self.assertEqual(report["fallback_count"], 1)
        self.assertTrue(report["replay_complete"])

    def test_resume_does_not_count_old_decisions_as_new_work(self):
        sessions = [
            {
                "status": "interrupted",
                "elapsed_seconds": 4,
                "new_decisions": 1,
                "decision_commits": [{"through_commit_ms": 1100}],
            },
            {
                "status": "completed",
                "elapsed_seconds": 6,
                "new_decisions": 1,
                "decision_commits": [{"through_commit_ms": 2100}],
            },
            {
                "status": "completed",
                "elapsed_seconds": 2,
                "new_decisions": 0,
                "decision_commits": [],
            },
        ]
        report = build_kpis([decision(1000), decision(2000)], sessions, {}, 5, 5, 60)
        self.assertEqual(report["unique_decisions"], 2)
        self.assertEqual(report["total_decision_processing_seconds"], 3)
        self.assertEqual(report["commit_timing_samples"], 2)
        self.assertEqual(report["replay_wall_seconds"], 12)
        self.assertEqual(report["decisions_per_wall_second"], round(2 / 12, 6))
        self.assertTrue(all("decision_commits" not in item for item in report["sessions"]))

    def test_interrupted_run_is_not_complete(self):
        sessions = [{"status": "interrupted", "elapsed_seconds": 2, "new_decisions": 1}]
        report = build_kpis([decision(1000)], sessions, {}, 5, 1, 60)
        self.assertFalse(report["replay_complete"])
        self.assertEqual(report["processed_arrivals"], 1)
        self.assertEqual(report["total_arrivals"], 5)
