"""Replay arrivals in order, checkpoint progress, and export decisions and KPIs."""

import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import runtime_dir
from .common import dump_json, dump_jsonl, iso, records, ts
from .engine import LifecycleAgent
from .metrics import write_kpis
from .provider import ModelsUnavailable, create_provider
from .store import Store


def export_replay(store, out):
    """Write human-readable exports without changing any decisions."""
    out = Path(out)
    decisions = [
        json.loads(row[0])
        for row in store.db.execute(
            "SELECT payload FROM decisions ORDER BY published_at,trigger_event_id"
        )
    ]
    actions = [
        json.loads(row[0])
        for row in store.db.execute("SELECT payload FROM actions ORDER BY published_at,action_id")
    ]
    dump_jsonl(out / "decisions.jsonl", decisions)
    dump_jsonl(out / "actions.jsonl", actions)
    state_dir = out / "viewer_state"
    state_dir.mkdir(exist_ok=True)
    dump_jsonl(
        state_dir / "states.jsonl",
        [
            json.loads(row[0])
            for row in store.db.execute("SELECT state_json FROM viewer_state ORDER BY user_id")
        ],
    )

    # Keep the original latency report format for existing report consumers.
    values = sorted(decision["decided_in_ms"] for decision in decisions)

    def percentile(fraction):
        return values[int((len(values) - 1) * fraction)] if values else None

    processing = [
        json.loads(row[0])
        for row in store.db.execute("SELECT payload FROM audit WHERE kind='decision_processing'")
    ]
    report = {
        "replay_complete": bool(store.value("replay_complete")),
        "stop": store.value("run_stop"),
        "unique_decisions": len(decisions),
        "actions": len(actions),
        "decision_ms": {
            "p50": percentile(0.5),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": max(values, default=0),
        },
        "over_60_seconds": sum(value > 60000 for value in values),
        "over_5_minutes": sum(value > 300000 for value in values),
        "max_through_persistence_ms": max(
            (row["through_persistence_ms"] for row in processing), default=0
        ),
        "model_calls": sum(decision["model_calls"] for decision in decisions),
        "max_model_calls": max((decision["model_calls"] for decision in decisions), default=0),
        "max_tool_calls": max((decision["tool_calls"] for decision in decisions), default=0),
        "reason_counts": dict(Counter(decision["reason"] for decision in decisions)),
        "timestamp_convention": "published_at = deliver_at + measured decision/verification processing; transaction/persistence time is separately measured",
        "load_scope": "Single-process faster-than-real-time fixture replay; not a production concurrency benchmark.",
    }
    dump_json(out / "latency_report.json", report)
    write_kpis(store, out)
    return report


def validate_run(store, config):
    """Prevent changed inputs/code from silently reusing old decisions."""
    previous = store.value("agent_config")
    if previous is not None and previous != config:
        raise ValueError("Configuration changed; select a new output directory")
    implementation = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(__file__).parent.glob("*.py")
    }
    previous_implementation = store.value("agent_implementation")
    if previous_implementation is not None and previous_implementation != implementation:
        raise ValueError(
            "Implementation changed; select a new output directory instead of reusing old decisions"
        )
    with store.db:
        store.set_value("agent_config", config)
        store.set_value("agent_implementation", implementation)


def load_arrivals(store):
    """At equal times, apply controls first, live events second, triggers last."""
    arrivals = []
    baseline = ts(store.value("baseline"))
    for row in store.db.execute("SELECT * FROM deliveries WHERE available_at>=?", (iso(baseline),)):
        priority = 1 if row["stream"] == "trigger" else 0
        arrivals.append(
            (
                row["available_at"],
                priority,
                row["source_ref"],
                row["stream"],
                json.loads(row["payload"]),
            )
        )
    for row in store.db.execute("SELECT * FROM controls"):
        arrivals.append((row["at"], -1, row["source_ref"], "control", json.loads(row["payload"])))
    return sorted(arrivals, key=lambda arrival: arrival[:3])


def apply_arrival(agent, kind, payload, at):
    """Only triggers produce decisions; other inputs update knowledge or goals."""
    if kind == "control":
        agent.apply_control(payload)
    elif kind == "trigger":
        return agent.handle_trigger(payload)
    else:
        agent.refresh_goals(payload["user_id"], ts(at))
    return None


def run_replay(pack, out, config):
    """Resume committed inputs and measure this invocation separately from past ones."""
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    provider = create_provider(config, list(records(pack, "replay/model_faults.jsonl")))
    store = Store(runtime_dir(out) / "agent.sqlite3")
    session = None
    sessions = []

    def save_session(status):
        session["status"] = status
        session["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        session["updated_at"] = datetime.now(timezone.utc).isoformat()
        with store.db:
            store.set_value("replay_sessions", sessions)
        write_kpis(store, out)

    try:
        store.initialize(pack)
        validate_run(store, config)
        agent = LifecycleAgent(store, provider, config)
        arrivals = load_arrivals(store)
        processed = {row[0] for row in store.db.execute("SELECT source_ref FROM processed")}
        sessions = store.value("replay_sessions") or []
        known_decisions = {
            row[0] for row in store.db.execute("SELECT trigger_event_id FROM decisions")
        }
        session = {
            "started_at": started_at,
            "elapsed_seconds": 0,
            "status": "running",
            "new_decisions": 0,
            "new_arrivals": 0,
            "decision_commits": [],
        }
        sessions.append(session)
        with store.db:
            store.set_value("total_arrivals", len(arrivals))
            store.set_value("run_stop", None)
            store.set_value("replay_complete", False)
        save_session("running")
        last_update = time.monotonic()

        for index, (at, priority, source_ref, kind, payload) in enumerate(arrivals, 1):
            if source_ref in processed:
                continue
            arrival_started = time.perf_counter()
            decision = None
            with store.db:
                # Input handling and its checkpoint succeed or roll back together.
                decision = apply_arrival(agent, kind, payload, at)
                store.db.execute("INSERT INTO processed VALUES (?)", (source_ref,))
                store.set_value("last_processed_at", at)
            # Measure AFTER leaving the transaction: SQLite commit time is included.
            committed_ms = (time.perf_counter() - arrival_started) * 1000
            session["new_arrivals"] += 1
            if decision is not None and payload["event_id"] not in known_decisions:
                session["new_decisions"] += 1
                session["decision_commits"].append(
                    {
                        "decision_id": decision["decision_id"],
                        "through_commit_ms": round(committed_ms, 3),
                    }
                )
                known_decisions.add(payload["event_id"])
            processed.add(source_ref)
            if time.monotonic() - last_update > 15:
                save_session("running")
                print(
                    f"Processed {index}/{len(arrivals)} arrivals; {session['new_decisions']} new decisions this session",
                    flush=True,
                )
                last_update = time.monotonic()

        with store.db:
            store.set_value("replay_complete", True)
        report = export_replay(store, out)
        save_session("completed")
        print(
            f"Replay complete: {report['unique_decisions']} unique decisions, {report['actions']} actions",
            flush=True,
        )
        return report
    except ModelsUnavailable as error:
        # The failed trigger transaction has rolled back. Export only committed actions.
        session["failed_model_calls"] = error.model_calls
        session["failed_decision_seconds"] = error.elapsed_seconds
        stop = {
            "reason": "all_models_unavailable",
            "trigger_event_id": error.trigger_event_id,
            "attempts": error.attempts,
            "model_calls": error.model_calls,
            "failed_decision_seconds": round(error.elapsed_seconds, 6),
            "message": "Replay stopped. No local recommendation was substituted.",
        }
        with store.db:
            store.set_value("replay_complete", False)
            store.set_value("run_stop", stop)
        save_session("partial")
        report = export_replay(store, out)
        print(
            f"Replay partial: {report['unique_decisions']} completed decisions; all models unavailable. Exported committed actions for simulation.",
            flush=True,
        )
        return report
    except BaseException:
        # Ctrl+C still leaves an honest partial KPI snapshot and resumable checkpoints.
        if session is not None:
            save_session("interrupted")
        raise
    finally:
        store.close()
        if hasattr(provider, "close"):
            provider.close()
