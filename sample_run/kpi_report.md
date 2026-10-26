# Run KPIs

| KPI | Value |
|---|---|
| Replay complete | True |
| Processed arrivals | 3380 / 3380 |
| Unique decisions | 2679 |
| Completed decisions with model responses | 1024 |
| Published / no-action decisions | 649 / 2030 |
| Actions | 734 |
| Arrival → decision mean / p50 / p95 / p99 / max (ms) | 2.033 / 1.267 / 4.76 / 6.789 / 57.577 |
| Total decision processing (seconds) | 5.445699 |
| Total decision processing through commit (seconds) | 10.512169 |
| Replay wall time across sessions (seconds) | 38.25859 |
| New decisions per wall second | 70.02349 |
| Decisions over configured budget | 0 |
| Model calls / tool calls | 1039 / 4142 |
| Provider fallbacks | 0 |
| Latest command wall time (seconds) | 45.54971354099689 |
| Latest simulator time (seconds) | 4.428474332999031 |
| Latest evaluation time (seconds) | 2.754980332996638 |

## Recorded decision reasons

- `insufficient_valid_content`: 76
- `evidence_backed_goal_and_action`: 649
- `holdout_or_unknown_viewer`: 1325
- `effective_dialect_uncertain`: 91
- `no_supported_eligible_goal`: 112
- `independent_verification_rejected`: 67
- `continue_current_plan`: 119
- `late_or_clock_skewed_trigger_not_a_live_visit`: 220
- `model_goal_claim_has_no_eligibility_evidence`: 1
- `safe_abstention: model_provider_HTTP_503`: 11
- `safe_abstention: malformed_model_output`: 1
- `safe_abstention: user_data_store_read_timeout`: 7

## Measurement definitions

- **arrival_to_decision_ms**: Measured handler processing until the publication gate; also published_at minus trigger.deliver_at. Excludes final database writes/commit.
- **arrival_to_commit_ms**: Measured trigger-handler entry through successful SQLite transaction commit, for new decisions only. Includes final persistence.
- **total_decision_processing_seconds**: Sum of arrival-to-decision latency across unique decisions, including abstentions. Not the total process runtime.
- **replay_wall_seconds**: Sum of recorded replay invocation durations, including initialization, live/control inputs, commits and progress exports. Excludes simulator/evaluation and time between invocations.
- **decisions_per_wall_second**: New unique decisions divided by recorded replay wall time; resuming a completed run does not count old decisions again.
- **interrupted_sessions**: Graceful interruption records elapsed time. Hard-killed sessions retain only the last periodic snapshot, so their wall time may be understated.
- **queueing**: Single serial fixture replay; no production queue wait is simulated. Arrival-to-decision describes handler latency, not event-receipt age or production end-to-end latency.
