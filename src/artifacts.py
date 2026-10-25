"""Keep assignment deliverables visible and runtime state in one hidden folder."""

import json
from pathlib import Path

from .store import Store


def runtime_dir(out):
    path = Path(out) / ".runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_report(out, name, report):
    """Persist machine-readable summaries without generating duplicate JSON files."""
    store = Store(runtime_dir(out) / "agent.sqlite3")
    try:
        with store.db:
            store.set_value(name, report)
    finally:
        store.close()


def consolidate_existing_run(out):
    """Migrate a finished legacy run without changing its decisions or results."""
    out = Path(out)
    internal = runtime_dir(out)
    for name in ["agent.sqlite3", "evaluation.sqlite3", "sim", "audit.jsonl"]:
        old, new = out / name, internal / name
        if old.exists():
            if new.exists():
                raise ValueError(f"Both old and new runtime paths exist: {name}")
            old.rename(new)
    for name in ["run_report", "impact_report", "kpi_report", "interrupted_run"]:
        old = out / (name + ".json")
        if old.exists():
            save_report(out, name, json.loads(old.read_text()))
            old.unlink()
