"""Small file/time helpers shared by the runnable pipeline."""

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
DIALECTS = {f"d{i}" for i in range(1, 7)}


def ts(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A timezone is required")
    return parsed.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def records(pack, relative):
    path = Path(pack) / relative
    with path.open(encoding="utf-8", newline="") as file:
        if path.suffix == ".csv":
            iterator = enumerate(csv.DictReader(file), 2)
        else:
            iterator = (
                (number, json.loads(line)) for number, line in enumerate(file, 1) if line.strip()
            )
        for number, row in iterator:
            yield {**row, "source_ref": f"{relative}:{number}"}


def dump_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def dump_jsonl(path, rows):
    with Path(path).open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def activity_time(event, tolerance=600):
    """Conservative device-time plausibility check, not a clock correction."""
    occurred, sent, received = (ts(event[key]) for key in ("occurred_at", "sent_at", "received_at"))
    if (
        0 <= (received - occurred).total_seconds() <= tolerance
        and 0 <= (sent - occurred).total_seconds() <= tolerance
    ):
        return occurred
    return None


def reference(source, at, now, detail=None):
    return {
        "source_ref": source,
        "observed_at": iso(at),
        "age_seconds": max(0, (now - at).total_seconds()),
        "detail": detail,
    }


def fingerprint(pack):
    paths = sorted(
        path for path in Path(pack).rglob("*") if path.is_file() and not path.name.startswith(".")
    )
    return {
        str(path.relative_to(pack)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    }
