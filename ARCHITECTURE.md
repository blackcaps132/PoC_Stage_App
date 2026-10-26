# Architecture

I kept this as a Python CLI with SQLite. The main problem is maintaining a viewer's goal and evidence over time; I did not need a framework to express that workflow.

## Decision flow

```text
arrival -> visible evidence -> eligible goals + content candidates
        -> model proposal -> independent checks -> committed decision/action
        -> simulator follow-up -> goal judgment + impact report
```

`__main__.py` runs the phases. `replay.py` orders arrivals. `engine.py` manages the viewer lifecycle. The other modules separate storage, evidence resolution, goal rules, model calls, verification and measurement.

## One owner per viewer

One shared `LifecycleAgent` implementation manages separate state rows for each viewer. This does not mean running 5,000 model processes. Each viewer has facts, one active goal, one plan, decision history and lessons. A partial unique SQLite index prevents two OPEN goals for the same viewer.

New events can close a goal or invalidate a plan. An existing goal keeps its original start and deadline. Duplicate triggers reuse their recorded decision. The agent does not repeatedly restart the same goal ID during a run.

## Evidence and source precedence

The pack is loaded into shared evidence tables, with original payloads and source references. Each lookup receives the replay cutoff from the scheduler. A model cannot request a future time, execute SQL or access the database directly.

I use server receipt/delivery time to decide when evidence becomes available. Phone timestamps are used only when they pass the clock-plausibility check. Conflicting payloads with the same event ID stop import; repeated deliveries of the same event are retained separately.

For dialect, the latest valid app-setting observation takes precedence over the profile or campaign link. Disagreements remain in the decision. Buffered or ambiguous settings can cause abstention.

For subscription state, the latest available profile is a starting point. Visible payment/lapse events can change it. A paid event contradicts a profile's `never` label, but uncertain expiry does not establish an active entitlement. Missing consent or a known opt-out blocks messaging.

These are conservative policies, not learned rules. The 600-second clock threshold and 48-hour profile age are configurable assumptions.

## Goal and content selection

`goals.py` builds an ordered list of goals supported by evidence. G06, G10 and G11 are excluded because their definitions or observable success signals are insufficient. `knowledge.py` filters content for dialect, availability, entitlement and duplicate content slugs, then shortlists at most 31 titles using goal fit, observed genres and episode duration.

The model selects an eligible goal and a subset of those title IDs. It does not receive catalogue descriptions or raw viewer histories. The engine builds the page using the selected subset and the local catalogue ordering; it does not treat the model's ordering as a learned ranking.

Home pages have an optional hero and up to three genre rows. A row needs at least three titles. A valid page can be reused for two hours. Push messages use fixed catalogue-based copy and are sent immediately only when consent, quiet hours, entitlement, frequency caps and the goal-age rule permit them. There is no real notification integration.

## Model calls and failure handling

The live configuration uses Groq, then Gemini. Both receive structured-output schemas restricted to the supplied goal and title IDs. Output validation also rejects malformed selections, duplicates and inconsistent null-goal responses. Model explanations and confidence do not establish facts.

Transient errors and invalid responses get bounded exponential retries. A bad response goes back to the same provider with the validation error and current candidates. Prior output is labelled untrusted. There are up to five attempts per provider, but all attempts share the 60-second, eight-model-call decision budget. Backoff starts at one second and caps at eight seconds, with jitter. Retry-After is respected; a long reset moves to the next provider. Authentication failures are not repeatedly retried.

There is no local recommendation fallback in live mode. If neither model returns a valid proposal, the unfinished trigger rolls back and replay stops. Previously committed actions are still simulated. Reports show partial coverage and the actual failure codes rather than calling every failure a free-tier quota problem.

`ReplayProvider` is an explicit, key-free evaluator/test mode. It reproduces the supplied model-fault fixtures, including probes at stale or holdout triggers. It is not part of the live fallback chain. Injected provider failures in this mode produce recorded no-action decisions, so the offline replay can finish the full stream. Real provider exhaustion still stops live mode.

## Independent verification

`verification.py` checks eligibility separately from candidate generation. It also checks assignment arm, provenance, current goal rule, content availability, dialect, entitlement, row structure, consent, quiet hours, daily caps, copy and duplicate actions. A model cannot authorize publication.

The final publication gate checks for evidence, rule, availability or time-boundary changes while the model was working. Unsafe actions are discarded. Missing evidence can produce a recorded no-action decision; that is different from inventing a replacement model proposal.

## Transactions, timing and recovery

The serial scheduler merges controls, live events and triggers. At equal times, controls run first and triggers last. One transaction commits the input's state changes, decision, actions and checkpoint. A failed transaction is retried on resume. Input, configuration and implementation fingerprints prevent accidental reuse after changes.

`decided_in_ms` measures handler processing through the publication gate. Arrival-to-commit includes the final SQLite commit. KPIs distinguish summed decision time from replay wall time and simulator/evaluation time. They update periodically and on graceful interruption. A hard kill can lose timing since the last snapshot.

This is a single-process replay, not a production queue or load test. A distributed version would need per-viewer ownership, partitioning and a transactional publishing boundary.

## Outcomes and impact

The supplied simulator consumes only exported actions. Follow-up events are deduplicated against known events. A goal succeeds only with a matching signal inside its window and a plausible occurrence time. Missing follow-up or ambiguous clocks can make a result inconclusive. Goal attainment alone does not establish causation.

Partial runs are evaluated on a database copy. This prevents future simulator observations from changing the live checkpoint before a later resume.

The primary comparison is any playback within seven days of the viewer's first trigger; the secondary is a non-trial purchase in the same window. Both use the same fixed triggered cohort and randomized assignment, including unserved agent-arm viewers. A partial run therefore measures partial serving, not the effect among only the eight or ten viewers who happened to receive an action. Intervals are approximate and results are synthetic.

## Files and trust boundaries

Public exports follow the PDF: decisions, actions, viewer state, outcomes, latency, impact and final reports, plus a Markdown KPI report. `.runtime/` holds databases and raw simulator files. Summaries and audit history remain queryable in SQLite.

The database contains staged future inputs, so it is not a security sandbox for unrestricted database access. Models receive only bounded candidate data. Keys come from the environment or ignored `.env` file. Provider retention settings must be verified separately; `store=False` is not proof of zero retention or no training. See `KNOWN_LIMITATIONS.md` before using hosted providers with the assignment pack.
