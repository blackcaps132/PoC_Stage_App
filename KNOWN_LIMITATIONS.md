# Known limitations

## Coverage and live providers

The live runs completed only part of the replay. One stopped after Groq rate limiting and a rejected Gemini title selection; retries and candidate-specific schemas were improved afterward. Small live API checks passed, but they do not establish full-run quota, reliability or decision quality. The included sample uses the offline replay provider and keeps its actual completion status.

Live mode deliberately stops when no model can return a valid proposal. This preserves the no-local-recommendation-fallback choice, but differs from the PDF's suggested degraded action/no-action path during an extended outage. Offline replay handles injected outages as recorded no-action decisions and continues through the full stream. Partial reports show what was and was not processed.

Hosted-provider privacy is an unresolved assignment requirement. The PDF says to treat the pack as personal data and not upload it to services that retain or train on it. Google's unpaid-service terms permit product-improvement use; `store=False` only disables interaction storage. Groq offers Zero Data Retention controls, but this repo cannot establish which account settings are enabled. Use the key-free replay configuration for evaluation; verify acceptable provider settings before sending pack-derived context to hosted models. Sources: [Gemini terms](https://ai.google.dev/gemini-api/terms), [Groq data controls](https://console.groq.com/docs/your-data).

## Decisions and evidence

- G06 has no recency bound, G10 lacks a measurable definition, and G11 has no observable TV signal. I excluded them rather than inventing rules.
- First-play/completion goals rely on a corroborated signup and sufficiently complete post-signup reporting. Finite history cannot prove lifetime absence.
- The 600-second phone-clock check and 48-hour profile threshold are conservative defaults. Receipt-based lookbacks can miss old behavior uploaded late.
- Confidence is qualitative and explained; numeric scores are null. I have not calibrated probabilities for this dataset.
- The model sees an evidence-filtered shortlist, not rich title descriptions. Local rules still handle catalogue ordering and page layout. This is not a trained recommender.
- Premium content is promoted only with known active entitlement. That is conservative and limits the payment-goal strategy.
- Home pages and immediate push are supported. WhatsApp, in-app messages, future scheduling and re-consent flows are not implemented.
- The action API cannot recall already-published pages. The agent invalidates affected plans and blocks later promotion; display TTL belongs to the simulator.

## Runtime and measurement

There is one serial SQLite writer. No production concurrency or million-viewer scale test was run. Client timeouts and the publication gate bound normal work, but they are not hard operating-system/disk guarantees. A model retry may be skipped when its delay exceeds the remaining decision budget.

The store-degradation and model-timeout fixtures emulate a client abandoning a slow request at its shorter configured timeout. They do not reproduce a full service latency distribution.

The simulator has simplified playback/subscription behavior and a fixed replay calendar. It does not establish real-world effectiveness. Only triggered users receive follow-up; impact is reported for that cohort, with approximate intervals and one synthetic seed. Goal success is descriptive, and partial-serving results must not be called full-replay uplift.

Facts and tools enforce visibility in application code. The staged SQLite database itself contains future fixtures. Do not give a model unrestricted SQL or clock access.
