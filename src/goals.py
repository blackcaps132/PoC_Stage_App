"""Measurable goal candidates and event-based goal judgments."""

from collections import defaultdict
from datetime import timedelta

from .common import IST, activity_time, iso, ts

UNSUPPORTED = {
    "G06": "recently_lapsed_has_no_recency_bound",
    "G10": "meaningful_engagement_undefined",
    "G11": "tv_signal_unavailable",
}


def eligible_candidates(facts, now, history_start, catalogue, episodes, config, trigger):
    """Build a priority-ordered list of goals supported by observable viewer evidence."""
    candidates = []
    signup = ts(facts["signup_at"])
    coverage = signup is not None and signup >= history_start and facts["signup_corroborated"]

    def add(goal_id, reason, context=None):
        if goal_id in config["supported_goals"]:
            refs = [item["source_ref"] for item in facts["evidence"]] + [
                f"config/goals.json#{goal_id}"
            ]
            candidates.append(
                {
                    "goal_id": goal_id,
                    "reason": reason,
                    "context": context or {},
                    "evidence_refs": refs,
                }
            )

    if coverage and timedelta(0) <= now - signup <= timedelta(days=7) and not facts["plays"]:
        add("G01", "recent_corroborated_signup_no_observed_play")
    if facts["subscription_status"] == "trial_active":
        add("G04", "trial_status_supported_by_visible_evidence")
    if coverage and timedelta(0) <= now - signup <= timedelta(days=2):
        add("G03", "signup_in_last_two_days")
    if coverage and not facts["completions"]:
        add("G02", "observed_post_signup_coverage_no_completion")
    played = defaultdict(list)
    completed = defaultdict(set)
    for event in facts["plays"]:
        title = catalogue.get(event["properties"].get("title_id"))
        if title and title["content_type"] in ("show", "micro_drama"):
            played[title["content_slug"]].append(event)
    for event in facts["completions"]:
        title = catalogue.get(event["properties"].get("title_id"))
        if title:
            completed[title["content_slug"]].add(event["properties"].get("episode_id"))
    for slug, events in sorted(
        played.items(),
        key=lambda item: max(ts(event["received_at"]) for event in item[1]),
        reverse=True,
    ):
        last_receipt = max(ts(event["received_at"]) for event in events)
        remaining = [
            episode
            for episode in episodes.get(slug, [])
            if episode["episode_id"] not in completed[slug]
        ]
        if remaining and now - last_receipt >= timedelta(days=3):
            add(
                "G07",
                "unfinished_series_no_recent_received_play",
                {"content_slug": slug, "next_episode_id": remaining[0]["episode_id"]},
            )
            break
    if facts["subscription_status"] == "active":
        recent = [
            event
            for event in facts["plays"]
            if ts(event["received_at"]) >= now - timedelta(days=14)
        ]
        times = [activity_time(event, config["fresh_event_seconds"]) for event in recent]
        if (
            all(at is not None for at in times)
            and len({at.astimezone(IST).date() for at in times}) <= 1
        ):
            add("G09", "active_and_at_most_one_observed_play_day_in_two_weeks")
        add("G08", "active_subscriber")
    if facts["subscription_status"] == "never" and not facts["paid_history"]:
        add("G05", "warehouse_never_label_with_no_contradicting_paid_event")
    previous_opens = [
        event
        for event in facts["events"]
        if event["event"] == "app_open"
        and event["event_id"] != trigger["event_id"]
        and ts(event.get("deliver_at", event["received_at"])) < ts(trigger["deliver_at"])
    ]
    existed = bool(previous_opens or (signup and signup <= now - timedelta(days=14)))
    if (
        existed
        and history_start <= now - timedelta(days=14)
        and not any(
            ts(event["received_at"]) >= now - timedelta(days=14) for event in previous_opens
        )
    ):
        add("G12", "no_received_app_open_in_prior_fourteen_days")
    return candidates


def matches(goal, event, catalogue):
    """Check whether an event satisfies this goal type, including any bound series target."""
    goal_id = goal["goal_id"]
    rule = goal["rule"]["success"]
    if event["event"] != rule["event"]:
        return False
    props = event.get("properties", {})
    if goal_id in ("G04", "G05"):
        return props.get("is_trial") is False
    if goal_id == "G07":
        title = catalogue.get(props.get("title_id"))
        return bool(title and title["content_slug"] == goal["context"].get("content_slug"))
    if goal_id == "G08":
        title = catalogue.get(props.get("title_id"))
        start = ts(goal["started_at"])
        return bool(
            title
            and start.date() - timedelta(days=30)
            <= ts(title["release_date"] + "T00:00:00Z").date()
            <= start.date()
        )
    return True


def judge_goal(goal, events, now, catalogue, config, coverage_end=None):
    """Judge attainment within the goal window; missing or ambiguous follow-up stays inconclusive."""
    start, end = ts(goal["started_at"]), ts(goal["deadline"])
    qualifying = []
    ambiguous = []
    days = set()
    for event in events:
        if not matches(goal, event, catalogue):
            continue
        received = ts(event.get("deliver_at", event["received_at"]))
        if received > now:
            continue
        at = activity_time(event, config["fresh_event_seconds"])
        if at is None:
            if received >= start:
                ambiguous.append(event["event_id"])
            continue
        if not start < at <= end:
            continue
        if goal["goal_id"] == "G03" and at.astimezone(IST).date() <= start.astimezone(IST).date():
            continue
        qualifying.append(event)
        days.add(at.astimezone(IST).date())
    succeeded = len(days) >= 3 if goal["goal_id"] == "G09" else bool(qualifying)
    if succeeded:
        status = "SUCCEEDED"
        reason = "qualifying_observed_events_inside_goal_window"
    elif coverage_end is not None and end > coverage_end:
        status = "INCONCLUSIVE"
        reason = "followup_does_not_cover_deadline"
    elif now < end:
        status = "OPEN"
        reason = "window_still_open"
    elif ambiguous:
        status = "INCONCLUSIVE"
        reason = "qualifying_reports_have_uncertain_occurrence_time"
    else:
        status = "FAILED"
        reason = "no_observed_success_by_deadline_under_reporting_completeness_assumption"
    return {
        "status": status,
        "reason": reason,
        "judged_at": iso(now),
        "qualifying_event_ids": [event["event_id"] for event in qualifying],
        "ambiguous_event_ids": ambiguous,
        "distinct_ist_days": sorted(day.isoformat() for day in days),
        "causal_attribution": "not_established_by_goal_attainment_alone",
    }
