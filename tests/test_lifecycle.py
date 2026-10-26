"""End-to-end safety/state tests against isolated copies of the supplied world."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from src.common import IST, iso, records, ts
from src.engine import LifecycleAgent
from src.goals import judge_goal
from src.knowledge import facts_for
from src.provider import DependencyFailure, ReplayProvider
from src.store import Store
from src.verification import verify

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "one_user_one_agent_pack"


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Store(Path(cls.temporary.name) / "base.sqlite3")
        cls.base.initialize(PACK)
        cls.config = json.loads((ROOT / "config/agent.json").read_text())
        # Select a positive example by evidence, not by a hard-coded fixture user ID.
        row = cls.base.db.execute(
            """SELECT d.payload FROM deliveries d JOIN events e ON e.event_id=d.event_id
            JOIN viewers v ON v.user_id=e.user_id WHERE d.stream='trigger' AND v.arm='agent'
            AND EXISTS(SELECT 1 FROM profiles p WHERE p.user_id=e.user_id AND p.available_at<=d.available_at
              AND julianday(json_extract(p.payload,'$.signup_at')) BETWEEN julianday(d.available_at)-7 AND julianday(d.available_at))
            AND NOT EXISTS(SELECT 1 FROM events x WHERE x.user_id=e.user_id AND x.event_type='play_started' AND x.available_at<=d.available_at)
            ORDER BY d.available_at LIMIT 1"""
        ).fetchone()
        cls.positive = json.loads(row[0])

    @classmethod
    def tearDownClass(cls):
        cls.base.close()
        cls.temporary.cleanup()

    def setUp(self):
        self.store = Store(":memory:")
        self.base.db.backup(self.store.db)
        self.provider = ReplayProvider(list(records(PACK, "replay/model_faults.jsonl")))
        self.agent = LifecycleAgent(self.store, self.provider, self.config.copy())

    def tearDown(self):
        self.store.close()

    def decision(self, trigger=None):
        with self.store.db:
            return self.agent.handle_trigger(copy.deepcopy(trigger or self.positive))

    def test_model_failure_stops_and_rolls_back_without_action(self):
        from src.provider import ModelsUnavailable

        with patch.object(self.provider, "name", "groq"), patch.object(
            self.provider, "propose", side_effect=DependencyFailure("HTTP_429")
        ):
            with self.assertRaises(ModelsUnavailable):
                self.decision()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
        self.assertIsNone(self.store.open_goal(self.positive["user_id"]))

    def test_offline_model_outage_records_no_action_and_continues(self):
        with patch.object(
            self.provider, "propose", side_effect=DependencyFailure("model_provider_HTTP_503")
        ):
            decision = self.decision()
        self.assertEqual(decision["status"], "NO_ACTION")
        self.assertIn("model_provider_HTTP_503", decision["reason"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
        self.assertIsNone(self.store.open_goal(self.positive["user_id"]))
        # The failure was checkpointable work, not a fabricated recommendation.
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM decisions").fetchone()[0], 1)
        self.assertNotIn("model_proposal", decision)

    def test_partial_replay_simulates_only_committed_actions(self):
        from src.__main__ import main
        from src.provider import ModelsUnavailable

        provider = ReplayProvider([])  # Test fixture only: no network calls.
        original = provider.propose
        requests = 0

        def model_output(trigger, candidates, titles, budget, incident=None):
            nonlocal requests
            if candidates:
                requests += 1
                if requests == 3:
                    raise ModelsUnavailable([{"provider": "test-model", "error": "HTTP_429"}])
            return original(trigger, candidates, titles, budget, None)

        provider.propose = model_output
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)

            def initialize(store, pack):
                self.base.db.backup(store.db)

            with patch("src.replay.Store.initialize", initialize), patch(
                "src.replay.create_provider", return_value=provider
            ), patch("sys.argv", ["agent", "replay", "--out", str(output)]):
                with self.assertRaises(SystemExit) as stopped:
                    main()
            self.assertEqual(stopped.exception.code, 2)
            from src.store import Store

            saved = Store(output / ".runtime/agent.sqlite3")
            try:
                report = saved.value("run_report")
            finally:
                saved.close()
            expected = {
                "decisions.jsonl",
                "actions.jsonl",
                "outcomes.jsonl",
                "viewer_state",
                "latency_report.json",
                "impact_report.md",
                "final_report.md",
                "kpi_report.md",
            }
            self.assertEqual(
                {path.name for path in output.iterdir() if not path.name.startswith(".")}, expected
            )
            self.assertFalse((output / "kpi_report.json").exists())
            self.assertFalse(report["pipeline_complete"])
            self.assertTrue(report["simulation_complete"])
            self.assertTrue(report["evaluation_complete"])
            self.assertEqual(report["stop"]["reason"], "all_models_unavailable")
            self.assertTrue((output / ".runtime/sim/simulator_report.json").is_file())
            self.assertEqual(report["impact"]["simulator_policy_problems"], 0)
            self.assertIn("partial-serving", (output / "final_report.md").read_text())
            with closing(sqlite3.connect(output / ".runtime/agent.sqlite3")) as db:
                # Partial evaluation cannot leak simulator outcomes into resumable live state.
                self.assertIsNone(
                    db.execute(
                        "SELECT 1 FROM metadata WHERE key='followup_available_at'"
                    ).fetchone()
                )
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
                failed = report["stop"]["trigger_event_id"]
                self.assertIsNone(
                    db.execute(
                        "SELECT 1 FROM decisions WHERE trigger_event_id=?", (failed,)
                    ).fetchone()
                )
                self.assertIsNone(
                    db.execute(
                        "SELECT 1 FROM actions WHERE json_extract(payload,'$.trigger_event_id')=?",
                        (failed,),
                    ).fetchone()
                )
                committed = db.execute("SELECT count(*) FROM actions").fetchone()[0]
            exported = sum(
                bool(line.strip()) for line in (output / "actions.jsonl").read_text().splitlines()
            )
            self.assertEqual(committed, exported)
            self.assertGreater(committed, 0)

    def test_push_cannot_promote_premium_title_after_entitlement_lapses(self):
        from src.verification import push_text

        self.decision()
        goal = self.store.open_goal(self.positive["user_id"])
        now = ts(goal["started_at"]) + timedelta(hours=3)
        facts = facts_for(self.store, self.positive["user_id"], now, self.config, self.positive)
        premium = next(
            title
            for title in self.agent.catalogue.values()
            if title["dialect"] == facts["dialect"] and title["is_premium"] == "true"
        )
        action = {
            "action_id": "premium-test",
            "decision_id": "policy",
            "user_id": self.positive["user_id"],
            "trigger_event_id": self.positive["event_id"],
            "type": "push",
            "published_at": iso(now),
            "send_at": iso(now),
            "title_id": premium["title_id"],
            "text": push_text(premium),
            "deeplink": "ott://title/" + premium["title_id"],
        }
        with patch(
            "src.verification.facts_for", return_value={**facts, "subscription_status": "lapsed"}
        ):
            checks, _ = verify(
                self.store,
                self.positive,
                goal,
                [action],
                now,
                self.config,
                self.agent.catalogue,
                self.agent.episodes,
            )
        self.assertIn("push_entitlement", checks["failed"])

    def test_valid_action_and_duplicate_are_idempotent(self):
        first = self.decision()
        self.assertEqual(first["status"], "PUBLISHED")
        self.assertEqual(first["checks"]["failed"], [])
        count = self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        goal = self.store.open_goal(first["user_id"])
        self.assertEqual(self.decision(), first)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], count)
        self.assertEqual(self.store.open_goal(first["user_id"])["deadline"], goal["deadline"])

    def test_conflicting_profile_dialect_preserves_both_and_uses_event(self):
        uid = self.positive["user_id"]
        profile = self.store.history(uid, ts(self.positive["deliver_at"]))["profiles"][-1]
        profile["preferred_dialect"] = "d2" if self.positive["app_dialect"] != "d2" else "d1"
        self.store.db.execute(
            "UPDATE profiles SET payload=? WHERE source_ref=?",
            (json.dumps(profile), profile["source_ref"]),
        )
        decision = self.decision()
        self.assertEqual(decision["dialect"]["used"], self.positive["app_dialect"])
        self.assertEqual(decision["dialect"]["profile_value"], profile["preferred_dialect"])
        self.assertTrue(decision["dialect"]["conflicts"])
        self.assertIn("source", decision["dialect"])

    def test_buffered_trigger_does_not_create_action(self):
        trigger = copy.deepcopy(self.positive)
        trigger["occurred_at"] = iso(ts(trigger["received_at"]) - timedelta(days=3))
        self.store.db.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps(trigger), trigger["event_id"]),
        )
        self.store.db.execute(
            "UPDATE deliveries SET payload=? WHERE source_ref=?",
            (json.dumps(trigger), trigger["source_ref"]),
        )
        result = self.decision(trigger)
        self.assertEqual(result["status"], "NO_ACTION")
        self.assertIn("late_or_clock_skewed", result["reason"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    def test_store_failure_abstains_and_is_durable(self):
        with patch.object(
            self.agent, "context", side_effect=DependencyFailure("injected_store_timeout")
        ):
            result = self.decision()
        self.assertEqual(result["status"], "NO_ACTION")
        self.assertIn("injected_store_timeout", result["reason"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)
        self.assertIsNone(self.store.open_goal(result["user_id"]))

    def test_future_replay_is_not_visible_to_tools(self):
        row = self.store.db.execute(
            "SELECT * FROM events WHERE stream!='history' ORDER BY available_at LIMIT 1"
        ).fetchone()
        before = ts(row["available_at"]) - timedelta(microseconds=1)
        self.assertEqual(self.store.recent(row["user_id"], before)["events"], [])
        self.assertIn(
            row["event_id"],
            [
                event["event_id"]
                for event in self.store.recent(row["user_id"], ts(row["available_at"]))["events"]
            ],
        )

    def test_goal_correction_only_changes_affected_open_goals(self):
        control = next(
            row
            for row in records(PACK, "replay/control_events.jsonl")
            if row["type"] == "goal_rule_corrected"
        )
        now = ts(control["at"])
        users = [
            row[0]
            for row in self.store.db.execute("SELECT user_id FROM viewers ORDER BY user_id LIMIT 3")
        ]
        rule = self.store.goal_rules(ts(self.store.value("baseline")))["G03"]
        for index, user in enumerate(users):
            goal = {
                "instance_id": f"test-{index}",
                "user_id": user,
                "goal_id": "G03" if index < 2 else "G01",
                "started_at": iso(now - timedelta(hours=1)),
                "deadline": iso(now + timedelta(hours=47)),
                "rule": copy.deepcopy(rule if index < 2 else self.store.goal_rules(now)["G01"]),
                "context": {},
                "evidence_refs": [],
                "status": "SUCCEEDED" if index == 1 else "OPEN",
            }
            self.store.save_goal(goal)
        self.agent.apply_control(control)
        corrected = self.store.open_goal(users[0])
        self.assertEqual(corrected["rule"]["success"]["event"], "play_started")
        closed = json.loads(
            self.store.db.execute(
                "SELECT payload FROM goals WHERE instance_id='test-1'"
            ).fetchone()[0]
        )
        self.assertEqual(closed["rule"]["success"]["event"], "app_open")
        untouched = self.store.open_goal(users[2])
        self.assertEqual(untouched["rule"]["success"]["event"], "play_started")
        # A later-day app open alone no longer succeeds under the corrected rule.
        when = now + timedelta(days=1)
        event = {
            "event_id": "test-app-open",
            "event": "app_open",
            "user_id": users[0],
            "occurred_at": iso(when),
            "sent_at": iso(when),
            "received_at": iso(when + timedelta(seconds=1)),
            "properties": {},
        }
        result = judge_goal(
            corrected, [event], when + timedelta(seconds=2), self.agent.catalogue, self.config
        )
        self.assertEqual(result["status"], "OPEN")
        event["event"] = "play_started"
        self.assertEqual(
            judge_goal(
                corrected, [event], when + timedelta(seconds=2), self.agent.catalogue, self.config
            )["status"],
            "SUCCEEDED",
        )

    def test_model_claim_cannot_override_eligibility(self):
        self.provider.faults[self.positive["event_id"]] = {
            "fault": "unsupported_claim",
            "model_output": {
                "goal_id": "G04",
                "eligible": True,
                "confidence": 0.99,
                "reason": "confirmed in billing",
            },
        }
        result = self.decision()
        self.assertEqual(result["status"], "NO_ACTION")
        self.assertIn("model_eligibility_claim", result["checks"]["failed"])
        self.assertIsNone(self.store.open_goal(result["user_id"]))

    def test_injected_description_does_not_authorize_messages(self):
        malicious = next(
            title
            for title in self.agent.catalogue.values()
            if "SYSTEM NOTE" in title["description"]
        )
        self.assertIn("including users who opted out", malicious["description"])
        result = self.decision()
        self.assertNotIn(
            "whatsapp", [row[0] for row in self.store.db.execute("SELECT type FROM actions")]
        )
        self.assertEqual(result["status"], "PUBLISHED")

    def test_missing_signal_and_incomplete_followup_are_not_failures(self):
        now = ts(self.positive["deliver_at"])
        goal = {
            "goal_id": "G01",
            "started_at": iso(now),
            "deadline": iso(now + timedelta(hours=24)),
            "rule": self.store.goal_rules(now)["G01"],
            "context": {},
        }
        result = judge_goal(
            goal,
            [],
            now + timedelta(hours=12),
            self.agent.catalogue,
            self.config,
            now + timedelta(hours=12),
        )
        self.assertEqual(result["status"], "INCONCLUSIVE")

    def test_batch_outcomes_do_not_rewrite_decision_time_knowledge(self):
        now = ts(self.positive["deliver_at"])
        event = copy.deepcopy(self.positive)
        event["event_id"] = "posthoc-observation"
        event["event"] = "play_started"
        self.store.db.execute(
            "INSERT INTO observations VALUES (?,?,?,?)",
            (event["event_id"], event["user_id"], iso(now), json.dumps(event)),
        )
        self.store.set_value("followup_available_at", iso(now + timedelta(days=15)))
        before = self.store.user_events(event["user_id"], now + timedelta(seconds=1))
        self.assertNotIn(event["event_id"], [item["event_id"] for item in before])
        after = self.store.user_events(event["user_id"], now + timedelta(days=15))
        self.assertIn(event["event_id"], [item["event_id"] for item in after])

    def test_timeout_fixture_cannot_override_holdout(self):
        fault = next(row for row in self.provider.faults.values() if row["fault"] == "timeout")
        row = self.store.db.execute(
            "SELECT payload FROM events WHERE event_id=?", (fault["trigger_event_id"],)
        ).fetchone()
        trigger = json.loads(row[0])
        self.assertEqual(self.store.state(trigger["user_id"])["arm"], "holdout")
        decision = self.decision(trigger)
        self.assertEqual(decision["status"], "NO_ACTION")
        self.assertEqual(decision["model_calls"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)

    def test_push_policy_blocks_consent_quiet_hours_and_frequency(self):
        self.decision()
        goal = self.store.open_goal(self.positive["user_id"])
        now = ts(goal["started_at"]) + timedelta(hours=3)
        # Use an actual visible event as the provenance-bearing trigger.
        trigger = copy.deepcopy(self.positive)
        facts = facts_for(self.store, trigger["user_id"], now, self.config, trigger)
        title = next(
            title
            for title in self.agent.catalogue.values()
            if title["dialect"] == facts["dialect"] and title["is_premium"] == "false"
        )
        action = {
            "action_id": "policy-test",
            "decision_id": "policy",
            "user_id": trigger["user_id"],
            "trigger_event_id": trigger["event_id"],
            "type": "push",
            "published_at": iso(now),
            "send_at": iso(now),
            "title_id": title["title_id"],
            "text": {"title": "A pick for you", "body": f"Watch {title['title']}."},
            "deeplink": "ott://title/" + title["title_id"],
        }
        for profile in self.store.history(trigger["user_id"], now)["profiles"]:
            profile["notifications_opt_in"] = "false"
            self.store.db.execute(
                "UPDATE profiles SET payload=? WHERE source_ref=?",
                (json.dumps(profile), profile["source_ref"]),
            )
        checks, _ = verify(
            self.store,
            trigger,
            goal,
            [action],
            now,
            self.config,
            self.agent.catalogue,
            self.agent.episodes,
        )
        self.assertIn("push_consent", checks["failed"])
        night = now.astimezone(IST).replace(hour=23)
        action["send_at"] = action["published_at"] = iso(night)
        checks, _ = verify(
            self.store,
            trigger,
            goal,
            [action],
            night,
            self.config,
            self.agent.catalogue,
            self.agent.episodes,
        )
        self.assertIn("quiet_hours", checks["failed"])
        day = now.astimezone(IST).date().isoformat()
        self.store.db.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
            ("already", "test", trigger["user_id"], "whatsapp", iso(now), day, "{}"),
        )
        action["send_at"] = action["published_at"] = iso(now)
        checks, _ = verify(
            self.store,
            trigger,
            goal,
            [action],
            now,
            self.config,
            self.agent.catalogue,
            self.agent.episodes,
        )
        self.assertIn("combined_daily_cap", checks["failed"])


if __name__ == "__main__":
    unittest.main()
