# Final sample report

Generated with `config/replay.json`: a complete offline replay, including injected failures recorded as no-action decisions. These are mock-provider results, not live-model results.

Completed 2679 unique decisions and 734 actions with the replay provider.

Goals: {'FAILED': 460, 'SUCCEEDED': 43, 'INCONCLUSIVE': 2}. Simulator policy problems: 0.

Maximum local decision processing: 57.577 ms. Decisions over 60 seconds: 0.

Total decision processing: 5.446 seconds. Replay wall time: 38.259 seconds.

See [kpi_report.md](kpi_report.md) for timings, percentiles and measurement definitions.

Primary assigned-arm playback difference: +1.267 percentage points.

See impact_report.md for denominators, intervals and limitations. A positive synthetic result is not a claim of real-world effectiveness.

Replay kept all data local. Push actions are simulator inputs only.

Model-fault probes belong to replay mode. Probe calls are counted and cannot authorize actions.

Operational audit counts: `{"control_applied": 4, "decision_processing": 2679, "duplicate_trigger": 90, "fixture_model_failure": 1, "fixture_model_output": 4, "goal_transition": 505}`.
