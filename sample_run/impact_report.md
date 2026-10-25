# Impact report

PARTIAL REPLAY: only committed actions before the model stop were simulated. The fixed cohort includes unprocessed viewers; this is partial-serving impact, not a full-replay result.

Practice seed: `oua-practice`. Cohort: 1746 triggered viewers with complete seven-day follow-up.

The primary endpoint is any playback in seven days after the first delivered trigger. The secondary endpoint is any non-trial purchase in that same window. Endpoints and denominators are shared by both randomized arms; unserved agent-arm viewers remain in the agent arm.

| Endpoint | Agent | Holdout | Difference (percentage points) | Approximate 95% interval |
|---|---|---|---|---|
| Any playback, 7 days | 552/863 (63.96%) | 571/883 (64.67%) | -0.703 | [-5.198, +3.792] |
| Any paid purchase, 7 days | 22/863 (2.55%) | 32/883 (3.62%) | -1.075 | [-2.695, +0.546] |

The primary interval includes zero. This run does not establish a clear playback uplift.

## Goal attainment and exposure

Goal statuses: `{'FAILED': 240, 'SUCCEEDED': 26, 'INCONCLUSIVE': 2}`. These are descriptive outcomes, not individualized causal claims.

Personalized-page exposure: 226 users. Message delivery: 22 users; clicks: 0. Opt-outs: 0 users.

Simulator policy problems: 0. Publication is not exposure; requested messages are not assumed delivered.

## Limits

- Synthetic single-seed replay; not real-world causal evidence.
- Assignment-level comparison in a fixed triggered cohort; not served versus unserved.
- Goal-specific attainment is descriptive because goals are chosen adaptively.
- Normal-approximation confidence intervals assume independent randomized viewers.
- Occurrence timing uses conservative phone-clock plausibility checks; late ambiguous reports are not successes.
