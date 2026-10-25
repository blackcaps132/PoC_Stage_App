# One User, One Agent

A Python CLI for the OTT lifecycle assignment. It keeps one goal and plan per viewer, makes decisions from evidence available at the trigger time, checks actions independently, and evaluates outcomes against the randomized holdout.

I focused on the decide–act–measure loop and kept the implementation in small Python modules with SQLite. There is no web UI or agent framework.

## Setup and key-free replay

Python 3.10+ is intended; development and tests used Python 3.14.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m src replay --config config/replay.json --out runs/offline
```

The replay configuration needs no API key and sends nothing to a model service. It reproduces the pack's model faults. Because this implementation stops when the provider is unavailable, the injected outage can produce a **partial run**, with exit status **2**. The simulator still runs on actions already committed, and the reports explain the stop. That is intentional behavior, not a successful full replay.

`sample_run/` contains a generated offline sample with its actual completion status. The original pack is unchanged and is required to rebuild a run.

## Live models

Live mode uses **Groq → Gemini**. Put the keys you have in `.env`:

```dotenv
GROQ_API_KEY=your-groq-key
GOOGLE_API_KEY=your-gemini-key
```

Before sending pack-derived data to hosted providers, check the PDF's privacy constraint and the provider/account retention settings. The free Gemini tier does not establish no-training/no-retention compliance. See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

```sh
# One small synthetic request, without the assignment's viewer history:
.venv/bin/python -m src.smoke

# Live replay, after provider settings and quota are suitable:
.venv/bin/python -m src replay --out runs/live
```

The configured models are `openai/gpt-oss-20b` on Groq and `gemini-3.5-flash-lite`. Both passed small live checks. A smoke check does not establish enough quota for a full run. `GEMINI_API_KEY` is also accepted; shell variables take precedence over `.env`.

## Decisions, retries and partial runs

Models select from eligible goals and a filtered shortlist of title IDs. Structured schemas restrict those IDs. Model text never establishes eligibility, consent or entitlement; the independent verifier and publication gate make those checks.

Transient errors and invalid responses get up to five attempts per provider, configurable from 2–5 in `config/agent.json`. Backoff starts at one second and doubles to an eight-second cap, with jitter. Invalid output goes back to the same model with its validation error. All attempts share the 60-second/eight-model-call decision budget. Long Retry-After delays move to the next model; invalid credentials are not repeatedly retried.

There is no local recommendation fallback in live mode. If no model returns a valid proposal, the unfinished trigger rolls back. Earlier actions are simulated and evaluated on a database copy, so future outcomes cannot leak into a later resume. Reports distinguish completed decisions, published actions, remaining inputs and provider errors. A valid no-action decision is completed work; a failed request is not.

An unchanged run can resume with the same command and output directory. Changed code, configuration, pack inputs or simulator seed require a new directory. `--skip-simulation` explicitly skips simulation/evaluation.

## Outputs

The visible layout follows PDF page 5, section 8, plus the Markdown KPI report:

```text
sample_run/
  decisions.jsonl
  viewer_state/states.jsonl
  actions.jsonl
  outcomes.jsonl
  latency_report.json
  impact_report.md
  final_report.md
  kpi_report.md
```

Working databases, audit records and raw simulator files live under `.runtime/` for newly generated runs. They are ignored by Git. Duplicate JSON summaries are stored in database metadata rather than exported. `latency_report.json` remains because the PDF requests it.

KPIs separate handler latency, time through SQLite commit, summed decision time, replay wall time, simulator/evaluation time, calls and retries. Snapshots update after arrivals finish and on graceful interruption. A blocked request cannot refresh its snapshot; a hard kill can lose recent timing.

The impact report uses the original fixed triggered cohort, including unserved agent-arm viewers. Partial results measure partial serving. Goal attainment is descriptive; neither a successful goal nor a synthetic arm difference proves real-world uplift.

## Tests and code map

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m src inspect --db runs/live/.runtime/agent.sqlite3 --user U-03057
```

Start with `__main__.py`, then `replay.py` and `engine.py`. `store.py` owns persistence; `knowledge.py` resolves evidence; `goals.py` defines eligibility and attainment; `provider.py`, `groq.py` and `gemini.py` handle model proposals; `verification.py` checks publication; `evaluation.py` and `metrics.py` write results. `ReplayProvider` is an explicit test/evaluator mode and is rejected by the live router.

[ARCHITECTURE.md](ARCHITECTURE.md) explains the choices, [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) lists gaps, and [AI_COLLABORATION.md](AI_COLLABORATION.md) records the AI assistance.
