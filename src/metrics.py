"""Timing and operational KPIs, kept separate from decision-making rules.

Replay timestamps belong to the fixture's calendar. Wall-clock durations measure
this Python process. Never subtract today's time from a fixture timestamp.
"""

import json
import math
from collections import Counter
from pathlib import Path


def duration_summary(values):
    """Summarize milliseconds using nearest-rank percentiles; empty samples are null."""
    values = sorted(values)
    if not values:
        return {name: None for name in ("mean", "p50", "p95", "p99", "max")}

    def percentile(fraction):
        return values[max(0, math.ceil(len(values) * fraction) - 1)]

    return {
        "mean": round(sum(values) / len(values), 3),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": values[-1],
    }


def build_kpis(
    decisions, sessions, fallback_reasons, total_arrivals, processed_arrivals, budget_seconds
):
    """Pure calculation so timing semantics can be tested without replaying the pack."""
    latencies = [decision["decided_in_ms"] for decision in decisions]
    committed = [
        timing["through_commit_ms"]
        for session in sessions
        for timing in session.get("decision_commits", [])
    ]
    wall_seconds = sum(session["elapsed_seconds"] for session in sessions)
    new_decisions = sum(session.get("new_decisions", 0) for session in sessions)
    published = sum(decision["status"] == "PUBLISHED" for decision in decisions)
    return {
        "replay_complete": bool(sessions and sessions[-1]["status"] == "completed"),
        "total_arrivals": total_arrivals,
        "processed_arrivals": processed_arrivals,
        "unique_decisions": len(decisions),
        "model_response_decisions": sum(
            bool(decision.get("model_proposal")) and decision.get("model_calls", 0) > 0
            for decision in decisions
        ),
        "published_decisions": published,
        "no_action_decisions": len(decisions) - published,
        "actions": sum(len(decision["actions"]) for decision in decisions),
        "arrival_to_decision_ms": duration_summary(latencies),
        "arrival_to_commit_ms": duration_summary(committed),
        "commit_timing_samples": len(committed),
        "total_decision_processing_seconds": round(sum(latencies) / 1000, 6),
        "total_decision_through_commit_seconds": round(sum(committed) / 1000, 6),
        "replay_wall_seconds": round(wall_seconds, 6),
        "decisions_per_wall_second": (
            round(new_decisions / wall_seconds, 6) if wall_seconds else None
        ),
        "decision_budget_seconds": budget_seconds,
        "decisions_over_budget": sum(value > budget_seconds * 1000 for value in latencies),
        "model_calls": sum(decision["model_calls"] for decision in decisions)
        + sum(session.get("failed_model_calls", 0) for session in sessions),
        "failed_decision_model_calls": sum(
            session.get("failed_model_calls", 0) for session in sessions
        ),
        "failed_decision_processing_seconds": sum(
            session.get("failed_decision_seconds", 0) for session in sessions
        ),
        "tool_calls": sum(decision["tool_calls"] for decision in decisions),
        "fallback_count": sum(fallback_reasons.values()),
        "fallback_reasons": fallback_reasons,
        "decision_reasons": dict(Counter(decision["reason"] for decision in decisions)),
        "session_count": len(sessions),
        "sessions": [
            {key: value for key, value in session.items() if key != "decision_commits"}
            for session in sessions
        ],
        "definitions": {
            "arrival_to_decision_ms": "Measured handler processing until the publication gate; also published_at minus trigger.deliver_at. Excludes final database writes/commit.",
            "arrival_to_commit_ms": "Measured trigger-handler entry through successful SQLite transaction commit, for new decisions only. Includes final persistence.",
            "total_decision_processing_seconds": "Sum of arrival-to-decision latency across unique decisions, including abstentions. Not the total process runtime.",
            "replay_wall_seconds": "Sum of recorded replay invocation durations, including initialization, live/control inputs, commits and progress exports. Excludes simulator/evaluation and time between invocations.",
            "decisions_per_wall_second": "New unique decisions divided by recorded replay wall time; resuming a completed run does not count old decisions again.",
            "interrupted_sessions": "Graceful interruption records elapsed time. Hard-killed sessions retain only the last periodic snapshot, so their wall time may be understated.",
            "queueing": "Single serial fixture replay; no production queue wait is simulated. Arrival-to-decision describes handler latency, not event-receipt age or production end-to-end latency.",
        },
    }


def write_kpis(store, out):
    """Export from committed state; safe for periodic snapshots and final reports."""
    decisions = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM decisions")]
    fallbacks = Counter()
    for row in store.db.execute("SELECT payload FROM audit WHERE kind='model_fallback'"):
        fallbacks[json.loads(row[0])["reason"]] += 1
    sessions = store.value("replay_sessions") or []
    config = store.value("agent_config") or {}
    report = build_kpis(
        decisions,
        sessions,
        dict(fallbacks),
        store.value("total_arrivals") or 0,
        store.db.execute("SELECT COUNT(*) FROM processed").fetchone()[0],
        config.get("max_decision_seconds", 60),
    )
    # Evaluation may re-export replay data. Keep the separately measured pipeline duration.
    report["pipeline_timings"] = store.value("pipeline_timings")
    report["stop"] = store.value("run_stop")
    with store.db:
        store.set_value("kpi_report", report)
    write_kpi_markdown(out, report)
    return report


def write_kpi_markdown(out, report):
    """Small KPI table for reading results without opening a large JSON file."""
    latency = report["arrival_to_decision_ms"]
    rows = [
        ("Replay complete", report["replay_complete"]),
        ("Processed arrivals", f"{report['processed_arrivals']} / {report['total_arrivals']}"),
        ("Unique decisions", report["unique_decisions"]),
        ("Completed decisions with model responses", report["model_response_decisions"]),
        (
            "Published / no-action decisions",
            f"{report['published_decisions']} / {report['no_action_decisions']}",
        ),
        ("Actions", report["actions"]),
        (
            "Arrival → decision mean / p50 / p95 / p99 / max (ms)",
            " / ".join(str(latency[key]) for key in ("mean", "p50", "p95", "p99", "max")),
        ),
        ("Total decision processing (seconds)", report["total_decision_processing_seconds"]),
        (
            "Total decision processing through commit (seconds)",
            report["total_decision_through_commit_seconds"],
        ),
        ("Replay wall time across sessions (seconds)", report["replay_wall_seconds"]),
        ("New decisions per wall second", report["decisions_per_wall_second"]),
        ("Decisions over configured budget", report["decisions_over_budget"]),
        ("Model calls / tool calls", f"{report['model_calls']} / {report['tool_calls']}"),
        ("Provider fallbacks", report["fallback_count"]),
    ]
    pipeline = report.get("pipeline_timings")
    if pipeline:
        rows.extend(
            (label, pipeline[key])
            for label, key in [
                ("Latest command wall time (seconds)", "total_wall_seconds"),
                ("Latest simulator time (seconds)", "simulator_seconds"),
                ("Latest evaluation time (seconds)", "evaluation_seconds"),
            ]
        )
    lines = ["# Run KPIs", "", "| KPI | Value |", "|---|---|"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    stop = report.get("stop")
    if stop:
        lines.extend(
            [
                "",
                "## Partial run / model stop",
                "",
                f"Reason: `{stop.get('reason')}`. Unfinished trigger: `{stop.get('trigger_event_id')}`.",
                "",
                "| Provider | Model | Attempt | Error | Backoff seconds |",
                "|---|---|---|---|---|",
            ]
        )
        for attempt in stop.get("attempts", []):
            lines.append(
                f"| {attempt.get('provider', '')} | {attempt.get('model', '')} | {attempt.get('attempt', '')} | {attempt.get('error', '')} | {attempt.get('backoff_seconds', '')} |"
            )
    lines.extend(["", "## Recorded decision reasons", ""])
    lines.extend(f"- `{reason}`: {count}" for reason, count in report["decision_reasons"].items())
    lines.extend(["", "## Measurement definitions", ""])
    lines.extend(
        f"- **{name}**: {definition}" for name, definition in report["definitions"].items()
    )
    temporary = Path(out) / "kpi_report.md.tmp"
    temporary.write_text("\n".join(lines) + "\n")
    temporary.replace(Path(out) / "kpi_report.md")
