"""CLI entry point: read options, replay inputs, simulate outcomes, write reports."""

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

from .artifacts import runtime_dir, save_report
from .common import fingerprint
from .evaluation import evaluate
from .metrics import write_kpis
from .replay import run_replay
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay", "inspect"])
    parser.add_argument("--pack", type=Path, default=ROOT / "one_user_one_agent_pack")
    parser.add_argument("--out", type=Path, default=ROOT / "sample_run")
    parser.add_argument("--config", type=Path, default=ROOT / "config/agent.json")
    parser.add_argument("--seed", default="oua-practice")
    parser.add_argument("--skip-simulation", action="store_true")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--user")
    parser.add_argument("--at")
    parser.add_argument("--limit", type=int, default=10)
    return parser


def read_config(path, parser):
    """Reject invalid budgets before opening the replay database."""
    config = json.loads(path.read_text())
    if config.get("provider") not in ("replay", "gemini", "groq", "models"):
        parser.error("provider must be models, groq, gemini, or explicit replay test mode")
    for key in ["max_model_calls", "max_tool_calls"]:
        if type(config.get(key)) is not int or config[key] < 0:
            parser.error(f"{key} must be a nonnegative integer")
    for key in ["max_decision_seconds", "dependency_timeout_seconds"]:
        value = config.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            parser.error(f"{key} must be positive")
    return config


def run_simulator(pack, out, seed):
    """Actions are simulator inputs; this never sends real push notifications."""
    subprocess.run(
        [
            sys.executable,
            str(pack / "simulator/simulate_outcomes.py"),
            "--pack",
            str(pack),
            "--actions",
            str(out / "actions.jsonl"),
            "--out",
            str(runtime_dir(out) / "sim"),
            "--seed",
            seed,
        ],
        check=True,
    )


def save_pipeline_timings(out, timings):
    """Keep whole-command and per-phase durations separate from decision latency."""
    store = Store(runtime_dir(out) / "agent.sqlite3")
    try:
        with store.db:
            store.set_value("pipeline_timings", timings)
        return write_kpis(store, out)
    finally:
        store.close()


def write_final_report(out, config, seed, latency, impact, kpis):
    """Combine operational timing, policy results and impact in one reviewable report."""
    store = Store(runtime_dir(out) / "agent.sqlite3")
    try:
        with store.db:
            store.set_value("evaluation_seed", seed)
            store.set_value("evaluation_complete", True)
        audit_counts = {
            row[0]: row[1]
            for row in store.db.execute("SELECT kind,COUNT(*) FROM audit GROUP BY kind")
        }
        implementation = store.value("agent_implementation")
    finally:
        store.close()
    save_report(
        out,
        "run_report",
        {
            "pipeline_complete": latency["replay_complete"],
            "replay_complete": latency["replay_complete"],
            "simulation_complete": True,
            "evaluation_complete": True,
            "stop": latency.get("stop"),
            "seed": seed,
            "latency": latency,
            "kpis": kpis,
            "impact": impact,
            "audit_counts": audit_counts,
            "inputs_unchanged": True,
            "implementation_fingerprint": implementation,
            "provider": config["provider"],
        },
    )
    difference = impact["primary_any_play_7d"].get("risk_difference_pp")
    effect = (
        f"Primary assigned-arm playback difference: {difference:+.3f} percentage points."
        if difference is not None
        else "Primary effect unavailable: insufficient comparison cohort."
    )
    local_note = (
        "Replay kept all data local."
        if config["provider"] == "replay"
        else "Live models received bounded candidate evidence; API requests may be billed."
    )
    lines = [
        "# Final sample report" if latency["replay_complete"] else "# Partial run report",
        "",
        f"Completed {latency['unique_decisions']} unique decisions and {latency['actions']} actions with the {config['provider']} provider.",
        "",
        f"Goals: {impact['goal_status_counts']}. Simulator policy problems: {impact['simulator_policy_problems']}.",
        "",
        f"Maximum local decision processing: {latency['decision_ms']['max']:.3f} ms. Decisions over 60 seconds: {latency['over_60_seconds']}.",
        "",
        f"Total decision processing: {kpis['total_decision_processing_seconds']:.3f} seconds. Replay wall time: {kpis['replay_wall_seconds']:.3f} seconds.",
        "",
        "See [kpi_report.md](kpi_report.md) for timings, percentiles and measurement definitions.",
        "",
        effect,
        "",
        "See impact_report.md for denominators, intervals and limitations. A positive synthetic result is not a claim of real-world effectiveness.",
        "",
        local_note + " Push actions are simulator inputs only.",
        "",
        "Model-fault probes belong to replay mode. Probe calls are counted and cannot authorize actions.",
        "",
        "Operational audit counts: `" + json.dumps(audit_counts, sort_keys=True) + "`.",
        "",
    ]
    if not latency["replay_complete"]:
        lines[2:2] = [
            "Replay stopped because no configured model could return a valid proposal.",
            "",
            f"Completed decisions: {latency['unique_decisions']}; decisions with model responses: {kpis['model_response_decisions']}; published actions simulated: {latency['actions']}; remaining arrivals: {kpis['total_arrivals'] - kpis['processed_arrivals']}.",
            "",
            "Provider failures: `" + json.dumps(latency.get("stop", {}).get("attempts", [])) + "`.",
            "",
            "Quota-limited/free-tier runs may stop early. No local recommendations replaced failed model calls. Failed and unprocessed triggers produced no actions.",
            "",
            "The supplied simulator still follows its predefined triggered cohort. Impact below measures this partial-serving policy; it is not a full-replay result or a served-only causal estimate.",
            "",
        ]
    (out / "final_report.md").write_text("\n".join(lines))


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "inspect":
        if not args.db or not args.db.is_file() or not args.user:
            parser.error("inspect requires an existing --db and --user")
        from .audit import inspect_viewer

        print(
            json.dumps(
                inspect_viewer(args.db, args.user, args.at, args.limit),
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    started = time.perf_counter()
    pack, out = args.pack.resolve(), args.out.resolve()
    config = read_config(args.config, parser)
    inputs_before = fingerprint(pack)
    phase_started = time.perf_counter()
    try:
        latency = run_replay(pack, out, config)
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    timings = {
        "scope": "Latest CLI invocation; excludes previous invocations and final report-writing overhead.",
        "replay_seconds": time.perf_counter() - phase_started,
        "simulator_seconds": None,
        "evaluation_seconds": None,
        "simulation_skipped": args.skip_simulation,
    }
    if args.skip_simulation or not latency["unique_decisions"]:
        timings["total_wall_seconds"] = time.perf_counter() - started
        save_pipeline_timings(out, timings)
        if not latency["replay_complete"]:
            save_report(
                out,
                "run_report",
                {
                    "pipeline_complete": False,
                    "replay_complete": False,
                    "simulation_complete": False,
                    "stop": latency.get("stop"),
                },
            )
            (out / "final_report.md").write_text(
                "# Partial run report\n\nNo simulation was run. See kpi_report.md for progress and provider errors.\n"
            )
            parser.exit(2, "Partial replay. Reports saved; simulation skipped.\n")
        print("Simulation/evaluation skipped explicitly. KPIs saved.")
        return

    store = Store(runtime_dir(out) / "agent.sqlite3")
    try:
        old_seed = store.value("evaluation_seed")
        if old_seed is not None and old_seed != args.seed:
            parser.error("Use a new --out directory when changing the simulator seed")
    finally:
        store.close()

    phase_started = time.perf_counter()
    run_simulator(pack, out, args.seed)
    timings["simulator_seconds"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    evaluation_database = None
    if not latency["replay_complete"]:
        # Follow-up must not close goals in the resumable replay's past.
        # Evaluate a copy so resuming after quota reset sees original live state.
        evaluation_database = runtime_dir(out) / "evaluation.sqlite3"
        source, target = Store(runtime_dir(out) / "agent.sqlite3"), Store(evaluation_database)
        try:
            source.db.backup(target.db)
        finally:
            source.close()
            target.close()
    impact = evaluate(pack, out, config, args.seed, database=evaluation_database)
    timings["evaluation_seconds"] = time.perf_counter() - phase_started
    assert fingerprint(pack) == inputs_before, "Supplied pack was modified"
    timings["total_wall_seconds"] = time.perf_counter() - started
    kpis = save_pipeline_timings(out, timings)
    write_final_report(out, config, args.seed, latency, impact, kpis)
    if not latency["replay_complete"]:
        parser.exit(
            2, f"Partial replay: completed actions simulated and evaluated. Reports: {out}\n"
        )
    print(f"Pipeline complete. Reports: {out}", flush=True)


if __name__ == "__main__":
    main()
