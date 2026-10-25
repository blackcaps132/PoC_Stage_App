"""Goal outcomes, exposure diagnostics and randomized-assignment comparisons."""

import json
import math
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path

from .artifacts import runtime_dir
from .common import IST, activity_time, dump_jsonl, iso, records, ts
from .engine import LifecycleAgent
from .knowledge import facts_for
from .provider import ReplayProvider
from .replay import export_replay
from .store import Store


def comparison(users, arms, successes):
    """Compare the same endpoint across all randomized users in each assigned arm."""
    groups = {arm: [user for user in users if arms[user] == arm] for arm in ("agent", "holdout")}
    result = {
        arm: {"users": len(group), "successes": sum(user in successes for user in group)}
        for arm, group in groups.items()
    }
    for row in result.values():
        row["rate"] = row["successes"] / row["users"] if row["users"] else None
    if all(result[arm]["users"] for arm in result):
        p, q = result["agent"]["rate"], result["holdout"]["rate"]
        se = math.sqrt(
            p * (1 - p) / result["agent"]["users"] + q * (1 - q) / result["holdout"]["users"]
        )
        result["risk_difference_pp"] = 100 * (p - q)
        result["approx_95_ci_pp"] = [100 * (p - q - 1.96 * se), 100 * (p - q + 1.96 * se)]
    return result


def evaluate(pack, out, config, seed, database=None):
    """Import simulator follow-up, judge goals, then calculate shared assigned-arm endpoints."""
    out = Path(out)
    store = Store(database or runtime_dir(out) / "agent.sqlite3")
    try:
        agent = LifecycleAgent(store, ReplayProvider([]), config)
        observed = list(records(runtime_dir(out) / "sim", "outcome_events.jsonl"))
        # Simulator copies live events; prefer already-stored original IDs instead of double counting.
        unique = {row["event_id"]: row for row in observed}
        baseline = ts(store.value("baseline"))
        coverage_end = baseline + timedelta(days=15)
        max_received = max((ts(row["received_at"]) for row in unique.values()), default=baseline)
        # The supplied simulator covers replay day plus fourteen full following IST days.
        last_observed_day = max_received.astimezone(IST).date()
        conservative_calendar_end = datetime.combine(
            last_observed_day + timedelta(days=1), time.min, tzinfo=IST
        )
        coverage_end = min(coverage_end, conservative_calendar_end)
        with store.db:
            store.set_value("followup_available_at", iso(coverage_end))
            for event in unique.values():
                store.db.execute(
                    "INSERT OR IGNORE INTO observations VALUES (?,?,?,?)",
                    (
                        event["event_id"],
                        event["user_id"],
                        iso(ts(event["received_at"])),
                        json.dumps(event),
                    ),
                )
            goal_users = [row[0] for row in store.db.execute("SELECT DISTINCT user_id FROM goals")]
            for user_id in goal_users:
                agent.refresh_goals(user_id, coverage_end, coverage_end)
                state = store.state(user_id)
                facts = facts_for(store, user_id, coverage_end, config)
                state["facts"] = {
                    key: value
                    for key, value in facts.items()
                    if key not in ("events", "plays", "completions", "paid_history")
                }
                state["observed_counts_after_followup"] = {
                    "plays": len(facts["plays"]),
                    "completions": len(facts["completions"]),
                    "paid_events": len(facts["paid_history"]),
                }
                store.save_state(user_id, state)
        goals = [
            json.loads(row[0])
            for row in store.db.execute("SELECT payload FROM goals ORDER BY started_at,instance_id")
        ]
        # Link observed exposure/delivery to the goal window without claiming causation.
        for goal in goals:
            events = agent.goal_events(goal["user_id"], coverage_end)
            exposures = [
                event
                for event in events
                if event["event"] in ("home_page_shown", "message_delivered")
                and ts(goal["started_at"]) <= ts(event["received_at"]) <= ts(goal["deadline"])
                and (event["event"] != "home_page_shown" or event["properties"].get("personalised"))
            ]
            goal["judgement"]["exposure_event_ids"] = [event["event_id"] for event in exposures]
            goal["judgement"][
                "exposure_interpretation"
            ] = "Temporal exposure evidence only; success may still be organic."
            with store.db:
                store.save_goal(goal)
        dump_jsonl(out / "outcomes.jsonl", goals)
        arms = {row["user_id"]: row["arm"] for row in store.db.execute("SELECT * FROM viewers")}
        first_trigger = {}
        for row in store.db.execute(
            "SELECT * FROM deliveries WHERE stream='trigger' ORDER BY available_at"
        ):
            event = json.loads(row["payload"])
            first_trigger.setdefault(event["user_id"], ts(event["deliver_at"]))
        cohort = {
            user
            for user, start in first_trigger.items()
            if start + timedelta(days=7) <= coverage_end
        }
        played = set()
        paid = set()
        exposed = set()
        delivered = set()
        clicked = set()
        opted_out = set()
        ambiguous = Counter()
        for event in unique.values():
            user = event["user_id"]
            if event["event"] == "home_page_shown" and event["properties"].get("personalised"):
                exposed.add(user)
            if event["event"] == "message_delivered":
                delivered.add(user)
            if event["event"] == "message_clicked":
                clicked.add(user)
            if event["event"] == "notification_opt_out":
                opted_out.add(user)
            if user not in cohort:
                continue
            at = activity_time(event, config["fresh_event_seconds"])
            if at is None:
                ambiguous[event["event"]] += 1
                continue
            if not first_trigger[user] <= at <= first_trigger[user] + timedelta(days=7):
                continue
            if event["event"] == "play_started":
                played.add(user)
            if (
                event["event"] == "purchase_completed"
                and event["properties"].get("is_trial") is False
            ):
                paid.add(user)
        play_impact = comparison(cohort, arms, played)
        paid_impact = comparison(cohort, arms, paid)
        simulator_report = json.loads((runtime_dir(out) / "sim/simulator_report.json").read_text())
        summary = {
            "replay_complete": bool(store.value("replay_complete")),
            "run_scope": "full_replay" if store.value("replay_complete") else "partial_serving",
            "stop": store.value("run_stop"),
            "seed": seed,
            "cohort": "all unique triggered users with complete seven-day simulated followup, irrespective of actions served",
            "cohort_users": len(cohort),
            "triggered_users": len(first_trigger),
            "coverage_end_exclusive": iso(coverage_end),
            "primary_any_play_7d": play_impact,
            "secondary_any_paid_purchase_7d": paid_impact,
            "goal_status_counts": dict(Counter(goal["status"] for goal in goals)),
            "exposure_users": len(exposed),
            "message_delivered_users": len(delivered),
            "message_clicked_users": len(clicked),
            "opt_out_users": len(opted_out),
            "ambiguous_event_counts": dict(ambiguous),
            "simulator_policy_problems": simulator_report["policy_problem_count"],
            "causal_limits": [
                "Synthetic single-seed replay; not real-world causal evidence.",
                "Assignment-level comparison in a fixed triggered cohort; not served versus unserved.",
                "Goal-specific attainment is descriptive because goals are chosen adaptively.",
                "Normal-approximation confidence intervals assume independent randomized viewers.",
                "Occurrence timing uses conservative phone-clock plausibility checks; late ambiguous reports are not successes.",
            ],
        }
        with store.db:
            store.set_value("impact_report", summary)
        lines = [
            "# Impact report",
            "",
            f"Practice seed: `{seed}`. Cohort: {len(cohort)} triggered viewers with complete seven-day follow-up.",
            "",
            "The primary endpoint is any playback in seven days after the first delivered trigger. The secondary endpoint is any non-trial purchase in that same window. Endpoints and denominators are shared by both randomized arms; unserved agent-arm viewers remain in the agent arm.",
            "",
            "| Endpoint | Agent | Holdout | Difference (percentage points) | Approximate 95% interval |",
            "|---|---|---|---|---|",
        ]
        for label, result in [
            ("Any playback, 7 days", play_impact),
            ("Any paid purchase, 7 days", paid_impact),
        ]:
            a, h = result["agent"], result["holdout"]

            def rate(row):
                if row["rate"] is None:
                    return "No users"
                return f"{row['successes']}/{row['users']} ({100 * row['rate']:.2f}%)"

            difference = (
                f"{result['risk_difference_pp']:+.3f}" if "risk_difference_pp" in result else "n/a"
            )
            interval = ", ".join(f"{value:+.3f}" for value in result.get("approx_95_ci_pp", []))
            lines.append(f"| {label} | {rate(a)} | {rate(h)} | {difference} | [{interval}] |")
        if (
            "approx_95_ci_pp" in play_impact
            and play_impact["approx_95_ci_pp"][0] <= 0 <= play_impact["approx_95_ci_pp"][1]
        ):
            lines += [
                "",
                "The primary interval includes zero. This run does not establish a clear playback uplift.",
            ]
        lines += [
            "",
            "## Goal attainment and exposure",
            "",
            f"Goal statuses: `{summary['goal_status_counts']}`. These are descriptive outcomes, not individualized causal claims.",
            "",
            f"Personalized-page exposure: {len(exposed)} users. Message delivery: {len(delivered)} users; clicks: {len(clicked)}. Opt-outs: {len(opted_out)} users.",
            "",
            f"Simulator policy problems: {simulator_report['policy_problem_count']}. Publication is not exposure; requested messages are not assumed delivered.",
            "",
            "## Limits",
            "",
        ]
        lines += ["- " + limit for limit in summary["causal_limits"]]
        if not store.value("replay_complete"):
            lines[2:2] = [
                "PARTIAL REPLAY: only committed actions before the model stop were simulated. The fixed cohort includes unprocessed viewers; this is partial-serving impact, not a full-replay result.",
                "",
            ]
        (out / "impact_report.md").write_text("\n".join(lines) + "\n")
        export_replay(store, out)
        return summary
    finally:
        store.close()
