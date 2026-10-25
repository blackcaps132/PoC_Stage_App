# AI collaboration log

I used Codex during data exploration, implementation, debugging and review. It helped write and refactor Python modules, tests and documentation. This was substantial implementation assistance, not just autocomplete.

## Decisions I directed

- Keep the code modular and simple enough to explain without adding an agent framework.
- Use SQLite for evidence and per-viewer memory rather than relying on separate viewer JSON files.
- Prefer the current app settings over profile and campaign dialect, while retaining disagreements.
- Move the reusable logic out of exploratory notebooks into a runnable CLI.
- Use Groq first and Gemini second. Keep the offline provider for evaluation and tests.
- Remove local recommendation fallbacks from live mode. If models cannot return a valid proposal, stop new decisions and simulate only the actions already committed.
- Add bounded retries, correction requests, and timing KPIs. Keep submission outputs close to the PDF's layout.

## Suggestions and output that needed correction

The first live Gemini setup used a model unavailable to the account. Small API checks exposed this before another full run. A later run also showed that a successful smoke check did not establish sufficient quota for replay: Groq returned a rate-limit error, and Gemini returned an unsupported title ID. The invalid action was blocked. Candidate-specific schemas, correction retries and cooldown handling were then added.

Earlier documentation still described a local fallback after that behavior had been removed. The pre-commit review replaced those notes and removed obsolete notebook history. The same review found a missing independent premium-entitlement check for push messages; a regression test now covers it.

## How the work was checked

- Run the unit tests for duplicates, conflicting sources, tool failure, corrected rules, publication policy, retries and partial evaluation.
- Run the supplied simulator against exported actions and inspect its policy results.
- Test each live adapter with a small synthetic structured request before a replay.
- Check that a failed model request rolls back, while earlier actions remain available for simulation.
- Keep partial simulation separate from the resumable live database so future outcomes cannot affect resumed decisions.
- Keep generated text out of eligibility and publication checks, and avoid presenting goal success as proof of causal uplift.

The included sample is an offline test run and is labelled with its completion status. Live provider checks and partial runs are not evidence of a successful full live replay. The code, assumptions and limitations still need to be understood and defended in the technical discussion.
