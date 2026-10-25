# Run KPIs

| KPI | Value |
|---|---|
| Replay complete | False |
| Processed arrivals | 1723 / 3380 |
| Unique decisions | 1278 |
| Completed decisions with model responses | 455 |
| Published / no-action decisions | 303 / 975 |
| Actions | 331 |
| Arrival → decision mean / p50 / p95 / p99 / max (ms) | 1.547 / 0.914 / 4.203 / 4.895 / 8.135 |
| Total decision processing (seconds) | 1.976546 |
| Total decision processing through commit (seconds) | 4.36367 |
| Replay wall time across sessions (seconds) | 31.716898 |
| New decisions per wall second | 40.293978 |
| Decisions over configured budget | 0 |
| Model calls / tool calls | 458 / 1883 |
| Provider fallbacks | 0 |
| Latest command wall time (seconds) | 39.17503362500429 |
| Latest simulator time (seconds) | 5.037136917002499 |
| Latest evaluation time (seconds) | 2.2670235830009915 |

## Partial run / model stop

Reason: `all_models_unavailable`. Unfinished trigger: `df07c579-6db5-4653-b5b8-340b5aec96f9`.

| Provider | Model | Attempt | Error | Backoff seconds |
|---|---|---|---|---|
| replay |  |  | model_provider_HTTP_503 |  |

## Recorded decision reasons

- `insufficient_valid_content`: 37
- `evidence_backed_goal_and_action`: 303
- `holdout_or_unknown_viewer`: 634
- `effective_dialect_uncertain`: 40
- `no_supported_eligible_goal`: 35
- `independent_verification_rejected`: 26
- `continue_current_plan`: 53
- `late_or_clock_skewed_trigger_not_a_live_visit`: 149
- `model_goal_claim_has_no_eligibility_evidence`: 1

## Measurement definitions

- **arrival_to_decision_ms**: Measured handler processing until the publication gate; also published_at minus trigger.deliver_at. Excludes final database writes/commit.
- **arrival_to_commit_ms**: Measured trigger-handler entry through successful SQLite transaction commit, for new decisions only. Includes final persistence.
- **total_decision_processing_seconds**: Sum of arrival-to-decision latency across unique decisions, including abstentions. Not the total process runtime.
- **replay_wall_seconds**: Sum of recorded replay invocation durations, including initialization, live/control inputs, commits and progress exports. Excludes simulator/evaluation and time between invocations.
- **decisions_per_wall_second**: New unique decisions divided by recorded replay wall time; resuming a completed run does not count old decisions again.
- **interrupted_sessions**: Graceful interruption records elapsed time. Hard-killed sessions retain only the last periodic snapshot, so their wall time may be understated.
- **queueing**: Single serial fixture replay; no production queue wait is simulated. Arrival-to-decision describes handler latency, not event-receipt age or production end-to-end latency.
