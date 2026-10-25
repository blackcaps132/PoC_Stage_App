"""Independent publication checks; model statements never count as evidence."""

from datetime import timedelta

from .common import DIALECTS, IST, activity_time, ts
from .knowledge import available, facts_for


def verify_goal(
    store, goal_id, facts, now, context, trigger, catalogue, episodes, config, continuing=False
):
    """Separate predicate implementation, not the proposal's candidate-list lookup."""
    signup = ts(facts["signup_at"])
    coverage = bool(
        signup and signup >= ts(store.value("history_start")) and facts["signup_corroborated"]
    )
    if goal_id in ("G01", "G02"):
        absent = not facts["plays"] if goal_id == "G01" else not facts["completions"]
        return (
            coverage
            and absent
            and (goal_id != "G01" or timedelta(0) <= now - signup <= timedelta(days=7))
        )
    if goal_id == "G03":
        return coverage and timedelta(0) <= now - signup <= timedelta(days=2)
    if goal_id == "G04":
        return facts["subscription_status"] == "trial_active"
    if goal_id == "G05":
        return facts["subscription_status"] == "never" and not facts["paid_history"]
    if goal_id == "G08":
        return facts["subscription_status"] == "active"
    if goal_id == "G09":
        if facts["subscription_status"] != "active":
            return False
        times = [
            activity_time(event, config["fresh_event_seconds"])
            for event in facts["plays"]
            if ts(event["received_at"]) >= now - timedelta(days=14)
        ]
        return (
            all(at is not None for at in times)
            and len({at.astimezone(IST).date() for at in times}) <= 1
        )
    if goal_id == "G07":
        slug = context.get("content_slug")
        played = [
            event
            for event in facts["plays"]
            if catalogue.get(event["properties"].get("title_id"), {}).get("content_slug") == slug
        ]
        done = {
            event["properties"].get("episode_id")
            for event in facts["completions"]
            if catalogue.get(event["properties"].get("title_id"), {}).get("content_slug") == slug
        }
        is_series = any(
            title["content_slug"] == slug and title["content_type"] in ("show", "micro_drama")
            for title in catalogue.values()
        )
        return bool(
            is_series
            and played
            and any(ep["episode_id"] not in done for ep in episodes.get(slug, []))
            and now - max(ts(event["received_at"]) for event in played) >= timedelta(days=3)
        )
    if goal_id == "G12":
        prior = [
            event
            for event in facts["events"]
            if event["event"] == "app_open"
            and event["event_id"] != trigger["event_id"]
            and ts(event.get("deliver_at", event["received_at"])) < ts(trigger["deliver_at"])
        ]
        existed = bool(prior or (signup and signup <= now - timedelta(days=14)))
        return (
            existed
            and ts(store.value("history_start")) <= now - timedelta(days=14)
            and not any(ts(event["received_at"]) >= now - timedelta(days=14) for event in prior)
        )
    return False


def verify(store, trigger, goal, actions, publish_at, config, catalogue, episodes):
    """Independently check each proposed action against current evidence and policy."""
    passed = []
    failed = []

    def check(name, result):
        (passed if result else failed).append(name)

    now = publish_at
    viewer = store.db.execute(
        "SELECT arm FROM viewers WHERE user_id=?", (trigger["user_id"],)
    ).fetchone()
    check("agent_arm", viewer is not None and viewer[0] == "agent")
    known = store.db.execute(
        "SELECT payload FROM deliveries WHERE source_ref=? AND stream='trigger'",
        (trigger["source_ref"],),
    ).fetchone()
    import json

    check("trigger_provenance", bool(known and json.loads(known[0]) == trigger))
    facts = facts_for(store, trigger["user_id"], now, config, trigger)
    check("fresh_live_trigger", activity_time(trigger, config["fresh_event_seconds"]) is not None)
    check("event_stream_dialect", facts["dialect"] in DIALECTS and not facts["dialect_ambiguities"])
    current = store.open_goal(trigger["user_id"])
    check("single_goal_owner", current is None or current["instance_id"] == goal["instance_id"])
    check("supported_goal", goal["goal_id"] in config["supported_goals"])
    current_rule = store.goal_rules(now).get(goal["goal_id"])
    check(
        "current_goal_rule",
        current_rule is not None and goal["rule"]["success"] == current_rule["success"],
    )
    # Continuing goals retain initial eligibility, but any newly observed success closes them first.
    if current is None:
        check(
            "eligibility",
            verify_goal(
                store,
                goal["goal_id"],
                facts,
                now,
                goal["context"],
                trigger,
                catalogue,
                episodes,
                config,
            ),
        )
    else:
        check("goal_still_open", current["status"] == "OPEN")
    controls = store.visible_controls(now)
    intervention = store.rule("interventions")
    all_promoted = []
    for action in actions:
        check("known_action_type", action["type"] in ("home_page", "push"))
        check("action_user_matches", action["user_id"] == trigger["user_id"])
        if action["type"] == "home_page":
            check("page_dialect", action.get("dialect") == facts["dialect"])
            rows = action.get("rows", [])
            check("page_row_count", 1 <= len(rows) <= intervention["home_page"]["rows"]["max_rows"])
            seen = set()
            hero = action.get("hero_title_id")
            ids = ([hero] if hero else []) + [
                title_id for row in rows for title_id in row["title_ids"]
            ]
            for row in rows:
                check("row_type", row["row_type"] in intervention["home_page"]["rows"]["row_types"])
                check("row_size", 3 <= len(row["title_ids"]) <= 10)
                if row["row_type"] == "genre":
                    check(
                        "genre_row_supported",
                        all(
                            row.get("genre")
                            in catalogue.get(title_id, {}).get("genres", "").split("|")
                            for title_id in row["title_ids"]
                        ),
                    )
            for title_id in ids:
                title = catalogue.get(title_id)
                check("title_exists", title is not None)
                if not title:
                    continue
                check("unique_content", title["content_slug"] not in seen)
                seen.add(title["content_slug"])
                check("dialect", title["dialect"] == facts["dialect"])
                check("availability", available(title, now, controls))
                check(
                    "entitlement",
                    title["is_premium"] == "false" or facts["subscription_status"] == "active",
                )
            all_promoted.extend(ids)
        elif action["type"] == "push":
            send = ts(action["send_at"])
            check("immediate_send", send == now)
            check("push_consent", facts["push_allowed"])
            hour = send.astimezone(IST).hour
            check("quiet_hours", 8 <= hour < 22)
            day = send.astimezone(IST).date().isoformat()
            prior = store.db.execute(
                "SELECT 1 FROM actions WHERE user_id=? AND type IN ('push','whatsapp') AND send_day=?",
                (trigger["user_id"], day),
            ).fetchone()
            historical_messages = [
                event
                for event in facts["events"]
                if event["event"] == "message_delivered"
                and event["properties"].get("channel") in ("push", "whatsapp")
                and ts(event["received_at"]).astimezone(IST).date().isoformat() == day
            ]
            check("combined_daily_cap", prior is None and not historical_messages)
            title = catalogue.get(action["title_id"])
            check(
                "push_title_available",
                bool(
                    title
                    and available(title, send, controls)
                    and title["dialect"] == facts["dialect"]
                ),
            )
            check(
                "push_entitlement",
                bool(
                    title
                    and (title["is_premium"] == "false" or facts["subscription_status"] == "active")
                ),
            )
            check("push_deeplink", action["deeplink"] == f"ott://title/{action['title_id']}")
            text = action["text"]
            check("copy_lengths", len(text["title"]) <= 40 and len(text["body"]) <= 120)
            combined = (text["title"] + " " + text["body"]).lower()
            check(
                "copy_policy",
                not any(
                    phrase in combined
                    for phrase in intervention["push"]["copy"]["forbidden_phrases"]
                ),
            )
            # Copy is produced by our exact catalogue template, never by a model or description.
            check("honest_copy", bool(title and text == push_text(title)))
            all_promoted.append(action["title_id"])
    if goal["goal_id"] == "G07":
        check(
            "resume_target_promoted",
            any(
                catalogue.get(title_id, {}).get("content_slug")
                == goal["context"].get("content_slug")
                for title_id in all_promoted
            ),
        )
    if goal["goal_id"] == "G08":
        start = ts(goal["started_at"])
        check(
            "recent_release_promoted",
            any(
                title_id in catalogue
                and start.date() - timedelta(days=30)
                <= ts(catalogue[title_id]["release_date"] + "T00:00:00Z").date()
                <= start.date()
                for title_id in all_promoted
            ),
        )
    check(
        "no_duplicate_action_ids",
        all(
            store.db.execute(
                "SELECT 1 FROM actions WHERE action_id=?", (action["action_id"],)
            ).fetchone()
            is None
            for action in actions
        ),
    )
    return {"passed": sorted(set(passed)), "failed": sorted(set(failed))}, facts


def push_text(title):
    # Keep a complete title name or abstain; no truncated/made-up content name.
    return {"title": "A pick for you", "body": f"Watch {title['title']}."}
