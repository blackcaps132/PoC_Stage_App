"""Human-facing time-bounded viewer inspection; not a model tool."""

import json

from .common import iso, ts
from .knowledge import facts_for
from .store import Store


def inspect_viewer(database, user_id, at=None, limit=10):
    """Reconstruct one viewer at a chosen time without treating final state as past knowledge."""
    store = Store(database)
    try:
        config = store.value("agent_config")
        if config is None:
            raise ValueError("Not a final replay database")
        if at is None:
            at = (
                store.value("followup_available_at")
                or store.db.execute("SELECT MAX(at) FROM observations").fetchone()[0]
                or store.value("last_processed_at")
                or store.value("baseline")
            )
        now = ts(at)
        exists = store.db.execute("SELECT 1 FROM viewers WHERE user_id=?", (user_id,)).fetchone()
        if exists is None:
            return {"user_id": user_id, "found": False}
        facts = facts_for(store, user_id, now, config)
        events = facts.pop("events")
        for key in ("plays", "completions", "paid_history"):
            facts[key + "_count"] = len(facts.pop(key))
        decisions = [
            json.loads(row[0])
            for row in store.db.execute(
                "SELECT payload FROM decisions WHERE user_id=? AND published_at<=? ORDER BY published_at",
                (user_id, iso(now)),
            )
        ]
        actions = [
            json.loads(row[0])
            for row in store.db.execute(
                "SELECT payload FROM actions WHERE user_id=? AND published_at<=? ORDER BY published_at",
                (user_id, iso(now)),
            )
        ]
        transitions = [
            {"at": row["at"], **json.loads(row["payload"])}
            for row in store.db.execute(
                "SELECT * FROM audit WHERE user_id=? AND at<=? AND kind='goal_transition' ORDER BY at,id",
                (user_id, iso(now)),
            )
        ]
        return {
            "user_id": user_id,
            "found": True,
            "as_of": iso(now),
            "facts": facts,
            "event_count": len(events),
            "recent_events": events[-limit:] if limit > 0 else [],
            "decisions": decisions,
            "actions": actions,
            "goal_transitions": transitions,
            "note": "No final state snapshot is returned for a past cutoff. Batch simulator observations enter context only at the recorded follow-up phase, not retroactively during replay.",
        }
    finally:
        store.close()
