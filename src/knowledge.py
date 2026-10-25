"""Turn visible records into explicitly sourced viewer facts."""

import re
from collections import Counter
from datetime import timedelta

from .common import DIALECTS, activity_time, iso, reference, ts


def facts_for(store, user_id, now, config, trigger=None):
    """Resolve viewer facts from visible evidence; retain conflicts instead of inventing certainty."""
    history = store.history(user_id, now)
    recent = store.recent(user_id, now)
    events = sorted(
        history["events"] + recent["events"],
        key=lambda row: (row.get("deliver_at", row["received_at"]), row["event_id"]),
    )
    profiles = history["profiles"]
    profile = profiles[-1] if profiles else None
    evidence = []
    assumptions = [
        "Observed absence is not lifetime proof; post-signup coverage is an explicit completeness assumption."
    ]
    conflicts = []
    signup = None
    signup_source = None
    if profile and profile.get("signup_at"):
        candidate = ts(profile["signup_at"])
        if candidate <= now:
            signup = candidate
            signup_source = profile["source_ref"]
    signup_events = [event for event in events if event["event"] == "signup_completed"]
    if signup is None and signup_events:
        valid = [
            (activity_time(event, config["fresh_event_seconds"]), event) for event in signup_events
        ]
        valid = [(at, event) for at, event in valid if at is not None and at <= now]
        if valid:
            signup, event = min(valid, key=lambda item: item[0])
            signup_source = event["source_ref"]
            assumptions.append(
                "Signup comes from a plausible phone clock; warehouse profile is missing."
            )
    corroborated = signup is not None and any(
        abs((ts(event["received_at"]) - signup).total_seconds()) <= config["fresh_event_seconds"]
        for event in signup_events
    )
    observed_plays = [event for event in events if event["event"] == "play_started"]
    completed = [event for event in events if event["event"] == "episode_completed"]
    prior_paid = [
        event
        for event in events
        if event["event"] == "subscription_renewed"
        or (event["event"] == "purchase_completed" and event["properties"].get("is_trial") is False)
    ]
    status = "unknown"
    status_source = None
    status_time = None
    status_expiry = None
    if profile:
        age = (now - ts(profile["updated_at"])).total_seconds() / 3600
        if age <= config["profile_max_age_hours"]:
            status = profile["subscription_status"]
            status_source = profile["source_ref"]
            status_time = ts(profile["snapshot_at"])
            status_expiry = ts(profile.get("plan_ends_at"))
    prices = {plan["plan_id"]: plan for plan in store.rule("pricing")["plans"]}
    for event in events:
        kind = event["event"]
        if kind not in ("purchase_completed", "subscription_renewed", "subscription_lapsed"):
            continue
        observed = ts(event.get("deliver_at", event["received_at"]))
        # Positive paid history always refutes 'never', even when older than the export.
        if observed < (status_time or ts(store.value("history_start"))):
            continue
        if kind == "subscription_lapsed":
            status = "lapsed"
            status_expiry = None
        else:
            at = activity_time(event, config["fresh_event_seconds"])
            plan = prices.get(event["properties"].get("plan_id"))
            if at and plan:
                status_expiry = at + timedelta(days=plan["days"])
                status = "trial_active" if event["properties"].get("is_trial") is True else "active"
            else:
                status = (
                    "paid_expiry_unknown"
                    if kind == "subscription_renewed"
                    or event["properties"].get("is_trial") is False
                    else "trial_expiry_unknown"
                )
                status_expiry = None
        status_source = event["source_ref"]
        status_time = observed
    if prior_paid and status == "never":
        conflicts.append(
            {
                "fact": "subscription",
                "profile_value": "never",
                "event_value": "paid_history",
                "source_refs": [profile["source_ref"], prior_paid[-1]["source_ref"]],
            }
        )
        status = "paid_expiry_unknown"
        status_source = prior_paid[-1]["source_ref"]
        status_time = ts(prior_paid[-1]["received_at"])
    if status_expiry and status_expiry <= now and status in ("active", "trial_active"):
        status = "lapsed"
    optouts = {
        event["properties"].get("channel")
        for event in events
        if event["event"] == "notification_opt_out"
    }
    push_allowed = bool(
        profile and profile["notifications_opt_in"] == "true" and "push" not in optouts
    )
    whatsapp_allowed = bool(
        profile and profile["whatsapp_opt_in"] == "true" and "whatsapp" not in optouts
    )
    last_app = events[-1] if events else None
    if trigger:
        # Use the newest received settings known at this decision, including the trigger.
        observations = events + [trigger]
        last_app = max(
            observations,
            key=lambda event: (
                event.get("deliver_at", event["received_at"]),
                event["event_id"] == trigger["event_id"],
            ),
        )
    dialect = last_app.get("app_dialect") if last_app else None
    language = last_app.get("app_language") if last_app else None
    ambiguities = []
    if dialect not in DIALECTS:
        ambiguities.append("Latest app-setting value is missing or unsupported.")
        dialect = None
    if last_app and activity_time(last_app, config["fresh_event_seconds"]) is None:
        ambiguities.append("Latest received app report may be buffered or clock-skewed.")
    if profile and profile.get("preferred_dialect") and profile["preferred_dialect"] != dialect:
        conflicts.append(
            {
                "fact": "dialect",
                "profile_value": profile["preferred_dialect"],
                "event_value": dialect,
                "source_refs": (
                    [profile["source_ref"], last_app["source_ref"]]
                    if last_app
                    else [profile["source_ref"]]
                ),
            }
        )
    if trigger:
        match = re.search(
            r"ott://c/[^/]+/(d[1-6])/title/", trigger["properties"].get("deeplink", "")
        )
        if match and match.group(1) != dialect:
            conflicts.append(
                {
                    "fact": "campaign_dialect",
                    "campaign_value": match.group(1),
                    "event_value": dialect,
                    "source_refs": [trigger["source_ref"]],
                }
            )
    if profile:
        evidence.append(
            reference(
                profile["source_ref"],
                ts(profile["snapshot_at"]),
                now,
                "Selected warehouse version; updated_at=" + profile["updated_at"],
            )
        )
    if last_app:
        evidence.append(
            reference(
                last_app["source_ref"],
                ts(last_app.get("deliver_at", last_app["received_at"])),
                now,
                "App-setting observation",
            )
        )
    for event in prior_paid[-1:] + signup_events[:1]:
        evidence.append(
            reference(
                event["source_ref"],
                ts(event.get("deliver_at", event["received_at"])),
                now,
                event["event"],
            )
        )
    if status_source and status_time:
        evidence.append(
            reference(status_source, status_time, now, "Subscription evidence, not model assertion")
        )
    return {
        "user_id": user_id,
        "known_as_of": iso(now),
        "profile": profile,
        "events": events,
        "signup_at": iso(signup) if signup else None,
        "signup_source": signup_source,
        "signup_corroborated": corroborated,
        "plays": observed_plays,
        "completions": completed,
        "paid_history": prior_paid,
        "subscription_status": status,
        "subscription_source": status_source,
        "subscription_expiry": iso(status_expiry) if status_expiry else None,
        "dialect": dialect,
        "language": language,
        "dialect_source": last_app["source_ref"] if last_app else None,
        "dialect_ambiguities": ambiguities,
        "conflicts": conflicts,
        "push_allowed": push_allowed,
        "whatsapp_allowed": whatsapp_allowed,
        "evidence": evidence,
        "assumptions": assumptions,
    }


def available(title, now, controls):
    """A title must be inside its availability window and not withdrawn by a visible control."""
    if not ts(title["available_from"]) <= now or (
        title["available_until"] and ts(title["available_until"]) <= now
    ):
        return False
    return not any(
        control["type"] == "title_withdrawn"
        and control["title_id"] == title["title_id"]
        and ts(control["at"]) <= now
        for control in controls
    )


def choose_titles(facts, catalogue, episodes, controls, now, goal):
    """Filter safe content first, then rank by goal fit and observed viewer preferences."""
    candidates = [
        title
        for title in catalogue.values()
        if title["dialect"] == facts["dialect"]
        and available(title, now, controls)
        and (title["is_premium"] == "false" or facts["subscription_status"] == "active")
    ]
    if facts["language"] in ("hin", "en"):
        candidates.sort(
            key=lambda title: (title["language"] != facts["language"], title["title_id"])
        )
    by_slug = {}
    for title in candidates:
        by_slug.setdefault(title["content_slug"], title)
    candidates = list(by_slug.values())
    genres = Counter()
    for event in facts["plays"]:
        title = catalogue.get(event["properties"].get("title_id"))
        if title:
            genres.update(title["genres"].split("|"))

    def rank(title):
        priority = 0
        if goal["goal_id"] == "G07" and title["content_slug"] == goal.get("context", {}).get(
            "content_slug"
        ):
            priority += 100
        goal_start = ts(goal.get("started_at")) or now
        if (
            goal["goal_id"] == "G08"
            and goal_start.date() - timedelta(days=30)
            <= ts(title["release_date"] + "T00:00:00Z").date()
            <= goal_start.date()
        ):
            priority += 50
        priority += sum(genres[g] for g in title["genres"].split("|"))
        minutes = min(
            (int(ep["minutes"]) for ep in episodes.get(title["content_slug"], [])), default=999
        )
        return (-priority, minutes, title["title_id"])

    return sorted(candidates, key=rank)[:31]
