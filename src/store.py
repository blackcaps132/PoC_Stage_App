"""SQLite evidence storage. Tools always receive a harness-owned cutoff."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

from .common import IST, fingerprint, iso, records, ts


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=1000")

    def close(self):
        self.db.close()

    def value(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_value(self, key, value):
        self.db.execute(
            "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def initialize(self, pack):
        """Import the pack once; preserve source records and reject changed inputs on resume."""
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS viewers(user_id TEXT PRIMARY KEY,arm TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profiles(source_ref TEXT PRIMARY KEY,user_id TEXT,available_at TEXT,updated_at TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS profile_lookup ON profiles(user_id,available_at,updated_at);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,user_id TEXT,event_type TEXT,available_at TEXT,stream TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS event_lookup ON events(user_id,available_at);
        CREATE TABLE IF NOT EXISTS deliveries(source_ref TEXT PRIMARY KEY,event_id TEXT,available_at TEXT,stream TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS delivery_lookup ON deliveries(event_id,available_at);
        CREATE TABLE IF NOT EXISTS catalogue(title_id TEXT PRIMARY KEY,slug TEXT,dialect TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS content_dialect ON catalogue(dialect);
        CREATE TABLE IF NOT EXISTS episodes(episode_id TEXT PRIMARY KEY,slug TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS rules(name TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE IF NOT EXISTS controls(source_ref TEXT PRIMARY KEY,at TEXT,type TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS control_time ON controls(at);
        CREATE TABLE IF NOT EXISTS viewer_state(user_id TEXT PRIMARY KEY,state_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS goals(instance_id TEXT PRIMARY KEY,user_id TEXT,goal_id TEXT,started_at TEXT,deadline TEXT,status TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS open_goal_lookup ON goals(user_id,status);
        CREATE INDEX IF NOT EXISTS goals_by_rule ON goals(goal_id,status);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_goal ON goals(user_id) WHERE status='OPEN';
        CREATE TABLE IF NOT EXISTS decisions(trigger_event_id TEXT PRIMARY KEY,user_id TEXT,published_at TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS actions(action_id TEXT PRIMARY KEY,decision_id TEXT,user_id TEXT,type TEXT,published_at TEXT,send_day TEXT,payload TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_outbound_per_day ON actions(user_id,send_day) WHERE type IN ('push','whatsapp');
        CREATE TABLE IF NOT EXISTS processed(source_ref TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,user_id TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS observations(event_id TEXT PRIMARY KEY,user_id TEXT,at TEXT,payload TEXT);
        CREATE INDEX IF NOT EXISTS observation_user_time ON observations(user_id,at);
        """)
        manifest = fingerprint(pack)
        previous = self.value("input_fingerprint")
        if previous is not None:
            if previous != manifest:
                raise ValueError("Pack changed; choose a new output directory")
            return
        schedule = []
        for relative, stream in [
            ("replay/triggers.jsonl", "trigger"),
            ("replay/live_events.jsonl", "live"),
        ]:
            schedule.extend((ts(row["deliver_at"]), stream, row) for row in records(pack, relative))
        if not schedule:
            raise ValueError("Replay contains no events")
        day = min(item[0] for item in schedule).astimezone(IST).date()
        baseline = datetime.combine(day, time.min, tzinfo=IST)
        history = list(records(pack, "data/events.jsonl"))
        schedule.extend((ts(row["received_at"]), "history", row) for row in history)
        schedule.sort(key=lambda item: (item[0], item[2]["source_ref"]))
        payload_by_id = {}
        with self.db:
            for row in records(pack, "replay/arms.csv"):
                self.db.execute("INSERT INTO viewers VALUES (?,?)", (row["user_id"], row["arm"]))
                self.save_state(
                    row["user_id"],
                    {
                        "user_id": row["user_id"],
                        "arm": row["arm"],
                        "active_goal_id": None,
                        "active_plan": None,
                        "facts": {},
                        "lessons": [],
                        "knowledge_as_of": iso(baseline),
                    },
                )
            for row in records(pack, "data/profiles_snapshot.csv"):
                available = max(ts(row["snapshot_at"]), ts(row["updated_at"]))
                self.db.execute(
                    "INSERT INTO profiles VALUES (?,?,?,?,?)",
                    (
                        row["source_ref"],
                        row["user_id"],
                        iso(available),
                        iso(ts(row["updated_at"])),
                        json.dumps(row),
                    ),
                )
            for at, stream, row in schedule:
                payload = {
                    key: value
                    for key, value in row.items()
                    if key not in ("received_at", "deliver_at", "source_ref")
                }
                event_id = row["event_id"]
                if event_id in payload_by_id and payload_by_id[event_id] != payload:
                    raise ValueError(f"Conflicting event payload: {event_id}")
                payload_by_id[event_id] = payload
                self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                    (event_id, row["user_id"], row["event"], iso(at), stream, json.dumps(row)),
                )
                self.db.execute(
                    "INSERT INTO deliveries VALUES (?,?,?,?,?)",
                    (row["source_ref"], event_id, iso(at), stream, json.dumps(row)),
                )
            for row in records(pack, "data/catalogue.csv"):
                self.db.execute(
                    "INSERT INTO catalogue VALUES (?,?,?,?)",
                    (row["title_id"], row["content_slug"], row["dialect"], json.dumps(row)),
                )
            for row in records(pack, "data/episodes.csv"):
                self.db.execute(
                    "INSERT INTO episodes VALUES (?,?,?)",
                    (row["episode_id"], row["content_slug"], json.dumps(row)),
                )
            for name in ("goals", "interventions", "pricing"):
                self.db.execute(
                    "INSERT INTO rules VALUES (?,?)",
                    (name, (Path(pack) / f"config/{name}.json").read_text()),
                )
            self.db.execute(
                "INSERT INTO rules VALUES (?,?)",
                (
                    "messaging_policy",
                    json.dumps((Path(pack) / "config/messaging_policy.md").read_text()),
                ),
            )
            for row in records(pack, "replay/control_events.jsonl"):
                self.db.execute(
                    "INSERT INTO controls VALUES (?,?,?,?)",
                    (row["source_ref"], iso(ts(row["at"])), row["type"], json.dumps(row)),
                )
            self.set_value("baseline", iso(baseline))
            self.set_value("history_start", min(iso(ts(row["received_at"])) for row in history))
            self.set_value("input_fingerprint", manifest)
        self.catalogue = None

    def state(self, user_id):
        row = self.db.execute(
            "SELECT state_json FROM viewer_state WHERE user_id=?", (user_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_state(self, user_id, state):
        self.db.execute(
            "INSERT INTO viewer_state VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET state_json=excluded.state_json",
            (user_id, json.dumps(state, allow_nan=False)),
        )

    def history(self, user_id, now):
        """Return profiles and historical events known by this cutoff, never future facts."""
        baseline = self.value("baseline")
        return {
            "events": self.user_events(user_id, now, before=baseline),
            "profiles": [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload FROM profiles WHERE user_id=? AND available_at<=? ORDER BY updated_at,source_ref",
                    (user_id, iso(now)),
                )
            ],
        }

    def recent(self, user_id, now):
        """Return non-history arrivals available to the viewer at this replay time."""
        return {
            "events": self.user_events(user_id, now, since=self.value("baseline")),
            "controls": self.visible_controls(now),
        }

    def user_events(self, user_id, now, before=None, since=None):
        """Combine visible event evidence, adding simulator observations only after follow-up."""
        sql = "SELECT payload FROM events WHERE user_id=? AND available_at<=?"
        parameters = [user_id, iso(now)]
        if before:
            sql += " AND available_at<?"
            parameters.append(before)
        if since:
            sql += " AND available_at>=?"
            parameters.append(since)
        sql += " ORDER BY available_at,event_id"
        result = {
            event["event_id"]: event
            for event in (json.loads(row[0]) for row in self.db.execute(sql, parameters))
        }
        # The batch simulator runs after replay. Do not retroactively expose its
        # generated sessions in an audit of what the decision agent actually knew.
        followup_at = self.value("followup_available_at")
        if followup_at is None or now < ts(followup_at):
            return sorted(
                result.values(),
                key=lambda event: (
                    event.get("deliver_at", event["received_at"]),
                    event["event_id"],
                ),
            )
        # In the follow-up phase, retain the event's own receipt-time boundary too.
        observed_sql = "SELECT payload FROM observations WHERE user_id=? AND at<=?"
        observed_params = [user_id, iso(now)]
        if before:
            observed_sql += " AND at<?"
            observed_params.append(before)
        if since:
            observed_sql += " AND at>=?"
            observed_params.append(since)
        for row in self.db.execute(observed_sql, observed_params):
            event = json.loads(row[0])
            result.setdefault(event["event_id"], event)
        return sorted(
            result.values(),
            key=lambda event: (event.get("deliver_at", event["received_at"]), event["event_id"]),
        )

    def visible_controls(self, now):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM controls WHERE at<=? ORDER BY at,source_ref", (iso(now),)
            )
        ]

    def rule(self, name):
        return json.loads(
            self.db.execute("SELECT payload FROM rules WHERE name=?", (name,)).fetchone()[0]
        )

    def goal_rules(self, now):
        """Start from pack rules, then apply only corrections already visible at this time."""
        result = {goal["goal_id"]: goal for goal in self.rule("goals")["goals"]}
        for control in self.visible_controls(now):
            if control["type"] == "goal_rule_corrected":
                goal = result[control["goal_id"]]
                goal["success"].update(control["new_success"])
                goal["correction"] = control
        return result

    def content(self):
        return {
            row["title_id"]: json.loads(row["payload"])
            for row in self.db.execute("SELECT * FROM catalogue")
        }

    def episode_map(self):
        """Group episodes by content slug so dubbed title variants share the same story."""
        result = defaultdict(list)
        for row in self.db.execute("SELECT * FROM episodes"):
            result[row["slug"]].append(json.loads(row["payload"]))
        for rows in result.values():
            rows.sort(key=lambda item: (int(item["season"]), int(item["episode"])))
        return dict(result)

    def open_goal(self, user_id):
        row = self.db.execute(
            "SELECT payload FROM goals WHERE user_id=? AND status='OPEN'", (user_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_goal(self, goal):
        """Persist one goal instance; the database enforces at most one OPEN goal per viewer."""
        self.db.execute(
            """INSERT INTO goals VALUES (?,?,?,?,?,?,?) ON CONFLICT(instance_id)
            DO UPDATE SET status=excluded.status,payload=excluded.payload,deadline=excluded.deadline""",
            (
                goal["instance_id"],
                goal["user_id"],
                goal["goal_id"],
                goal["started_at"],
                goal["deadline"],
                goal["status"],
                json.dumps(goal, allow_nan=False),
            ),
        )

    def log(self, now, kind, payload, user_id=None):
        self.db.execute(
            "INSERT INTO audit(at,kind,user_id,payload) VALUES (?,?,?,?)",
            (iso(now), kind, user_id, json.dumps(payload, allow_nan=False)),
        )
