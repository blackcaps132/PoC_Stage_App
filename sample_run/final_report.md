# Partial run report

Submission sample generated with `config/replay.json`. The supplied provider-outage fixture stopped replay; these are offline test results, not live-model results. To reproduce with the current code, use a new output directory.

Replay stopped because no configured model could return a valid proposal.

Completed decisions: 1278; decisions with model responses: 455; published actions simulated: 331; remaining arrivals: 1657.

Provider failures: `[{"provider": "replay", "error": "model_provider_HTTP_503"}]`.

Quota-limited/free-tier runs may stop early. No local recommendations replaced failed model calls. Failed and unprocessed triggers produced no actions.

The supplied simulator still follows its predefined triggered cohort. Impact below measures this partial-serving policy; it is not a full-replay result or a served-only causal estimate.

Completed 1278 unique decisions and 331 actions with the replay provider.

Goals: {'FAILED': 240, 'SUCCEEDED': 26, 'INCONCLUSIVE': 2}. Simulator policy problems: 0.

Maximum local decision processing: 8.135 ms. Decisions over 60 seconds: 0.

Total decision processing: 1.977 seconds. Replay wall time: 31.717 seconds.

See [kpi_report.md](kpi_report.md) for timings, percentiles and measurement definitions.

Primary assigned-arm playback difference: -0.703 percentage points.

See impact_report.md for denominators, intervals and limitations. A positive synthetic result is not a claim of real-world effectiveness.

Replay kept all data local. Push actions are simulator inputs only.

Model-fault probes belong to replay mode. Probe calls are counted and cannot authorize actions.

Operational audit counts: `{"control_applied": 3, "decision_processing": 1278, "duplicate_trigger": 35, "fixture_model_output": 3}`.
