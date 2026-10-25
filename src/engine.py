"""One logical lifecycle owner per viewer, using one serial SQLite writer."""

import copy
import json
import time
from collections import Counter
from datetime import timedelta

from .common import IST, activity_time, iso, reference, ts
from .goals import UNSUPPORTED, eligible_candidates, judge_goal
from .knowledge import available, choose_titles, facts_for
from .provider import Budget, DependencyFailure, ModelsUnavailable, parse_proposal
from .verification import push_text, verify


class LifecycleAgent:
    def __init__(self, store, provider, config):
        self.store = store
        self.provider = provider
        self.config = config
        self.catalogue = store.content()
        self.episodes = store.episode_map()
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS plan_titles(title_id TEXT,user_id TEXT,PRIMARY KEY(title_id,user_id))"
        )
        self.store.db.commit()

    def incident(self, name, now):
        return next(
            (
                control
                for control in reversed(self.store.visible_controls(now))
                if control.get("dependency") == name
                and ts(control["at"]) <= now < ts(control["until"])
            ),
            None,
        )

    def context(self, user_id, now, trigger):
        incident = self.incident("user_data_store", now)
        if incident:
            # Emulate the 8 s service latency; this client times out before it completes.
            timeout = min(self.config["dependency_timeout_seconds"], 8.0)
            time.sleep(timeout)
            raise DependencyFailure("user_data_store_read_timeout")
        return facts_for(self.store, user_id, now, self.config, trigger)

    def goal_events(self, user_id, now):
        events = {event["event_id"]: event for event in self.store.user_events(user_id, now)}
        for row in self.store.db.execute(
            "SELECT payload FROM observations WHERE user_id=? AND at<=?", (user_id, iso(now))
        ):
            event = json.loads(row[0])
            events.setdefault(event["event_id"], event)
        return list(events.values())

    def refresh_goals(self, user_id, now, coverage_end=None):
        """Re-evaluate owned goals from newly visible events and update viewer memory."""
        events = self.goal_events(user_id, now)
        rows = self.store.db.execute(
            "SELECT payload FROM goals WHERE user_id=? AND status!='SUPERSEDED'", (user_id,)
        ).fetchall()
        state = self.store.state(user_id)
        for row in rows:
            goal = json.loads(row[0])
            old = goal["status"]
            judgement = judge_goal(goal, events, now, self.catalogue, self.config, coverage_end)
            # We know a first play/completion/payment happened, but cannot assign its
            # device time to the goal window. Stop pursuing an impossible "first".
            if (
                judgement["status"] == "OPEN"
                and judgement["ambiguous_event_ids"]
                and goal["goal_id"] in ("G01", "G02", "G04", "G05")
            ):
                judgement["status"] = "INCONCLUSIVE"
                judgement["reason"] = (
                    "first_outcome_observed_but_occurrence_time_cannot_be_attributed"
                )
            if judgement["status"] != old or coverage_end is not None:
                goal["status"] = judgement["status"]
                goal["judgement"] = judgement
                self.store.save_goal(goal)
                if old != goal["status"]:
                    self.store.log(
                        now,
                        "goal_transition",
                        {
                            "instance_id": goal["instance_id"],
                            "from": old,
                            "to": goal["status"],
                            "judgement": judgement,
                        },
                        user_id,
                    )
                    if state:
                        state["lessons"].append(
                            {
                                "goal_id": goal["goal_id"],
                                "status": goal["status"],
                                "event_ids": judgement["qualifying_event_ids"],
                                "lesson": "Goal attainment is observed; individual causation is not established.",
                            }
                        )
        if state:
            current = self.store.open_goal(user_id)
            state["active_goal_id"] = current["instance_id"] if current else None
            if current is None:
                state["active_plan"] = None
                self.store.db.execute("DELETE FROM plan_titles WHERE user_id=?", (user_id,))
            state["knowledge_as_of"] = iso(now)
            self.store.save_state(user_id, state)

    def apply_control(self, control):
        """Apply operational corrections or content withdrawal to affected open plans."""
        now = ts(control["at"])
        affected = []
        if control["type"] == "goal_rule_corrected":
            rows = self.store.db.execute(
                "SELECT payload FROM goals WHERE goal_id=? AND status='OPEN'", (control["goal_id"],)
            ).fetchall()
            for row in rows:
                goal = json.loads(row[0])
                goal["rule"]["success"].update(control["new_success"])
                goal.setdefault("rule_changes", []).append(control)
                goal["deadline"] = iso(
                    ts(goal["started_at"])
                    + timedelta(hours=goal["rule"]["success"]["window_hours"])
                )
                self.store.save_goal(goal)
                affected.append(goal["user_id"])
            for user_id in set(affected):
                self.refresh_goals(user_id, now)
        elif control["type"] == "title_withdrawn":
            affected = [
                row[0]
                for row in self.store.db.execute(
                    "SELECT user_id FROM plan_titles WHERE title_id=?", (control["title_id"],)
                )
            ]
            for user_id in affected:
                state = self.store.state(user_id)
                if state and state.get("active_plan"):
                    state["active_plan"]["valid"] = False
                    state["active_plan"]["invalidation_reason"] = control["source_ref"]
                    self.store.save_state(user_id, state)
        self.store.log(
            now, "control_applied", {"control": control, "affected_users": sorted(set(affected))}
        )

    def make_actions(self, trigger, goal, titles, facts, now):
        """Build valid catalogue-based home pages and policy-checked immediate push requests."""
        if not titles:
            return [], ["no_available_content_in_effective_dialect"]
        chosen = titles
        hero = None
        remaining = chosen
        if len(chosen) >= 4:
            hero = chosen[0]["title_id"]
            remaining = chosen[1:]
        rows = []
        remaining = list(remaining)
        while len(remaining) >= 3 and len(rows) < 3:
            counts = Counter(genre for title in remaining for genre in title["genres"].split("|"))
            genre, count = counts.most_common(1)[0]
            if count < 3:
                break
            row_titles = [title for title in remaining if genre in title["genres"].split("|")][:10]
            rows.append(
                {
                    "row_type": "genre",
                    "genre": genre,
                    "title_ids": [title["title_id"] for title in row_titles],
                }
            )
            used = {title["content_slug"] for title in row_titles}
            remaining = [title for title in remaining if title["content_slug"] not in used]
        if not rows:
            return [], ["insufficient_distinct_titles_for_valid_row"]
        user_id = trigger["user_id"]
        decision_id = "D-" + trigger["event_id"]
        common = {
            "user_id": user_id,
            "decision_id": decision_id,
            "trigger_event_id": trigger["event_id"],
            "published_at": iso(now),
        }
        actions = []
        skipped = []
        previous = self.store.db.execute(
            "SELECT payload FROM actions WHERE user_id=? AND type='home_page' ORDER BY published_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        live_page = False
        if previous:
            page = json.loads(previous[0])
            state = self.store.state(user_id)
            live_page = (
                page["dialect"] == facts["dialect"]
                and now - ts(page["published_at"]) < timedelta(minutes=120)
                and bool(state.get("active_plan", {}))
                and state["active_plan"].get("valid", True)
            )
        if not live_page:
            actions.append(
                {
                    **common,
                    "action_id": "A-" + trigger["event_id"] + "-home",
                    "type": "home_page",
                    "dialect": facts["dialect"],
                    "hero_title_id": hero,
                    "rows": rows,
                }
            )
        else:
            skipped.append("existing_page_still_live")
        if self.config["push_enabled"]:
            day = now.astimezone(IST).date().isoformat()
            sent = self.store.db.execute(
                "SELECT 1 FROM actions WHERE user_id=? AND type IN ('push','whatsapp') AND send_day=?",
                (user_id, day),
            ).fetchone()
            prior_delivery = any(
                event["event"] == "message_delivered"
                and event["properties"].get("channel") in ("push", "whatsapp")
                and ts(event["received_at"]).astimezone(IST).date().isoformat() == day
                for event in facts["events"]
            )
            if not facts["push_allowed"]:
                skipped.append("push_no_consent")
            elif not 8 <= now.astimezone(IST).hour < 22:
                skipped.append("push_quiet_hours")
            elif sent or prior_delivery:
                skipped.append("push_daily_cap")
            elif now - ts(goal["started_at"]) < timedelta(
                hours=self.config["push_min_hours_since_goal"]
            ):
                skipped.append("push_not_yet_proportionate_to_new_goal")
            elif len(push_text(chosen[0])["body"]) > 120:
                skipped.append("push_catalogue_name_too_long")
            else:
                title = chosen[0]
                actions.append(
                    {
                        **common,
                        "action_id": "A-" + trigger["event_id"] + "-push",
                        "type": "push",
                        "send_at": iso(now),
                        "title_id": title["title_id"],
                        "text": push_text(title),
                        "deeplink": "ott://title/" + title["title_id"],
                    }
                )
        return actions, skipped

    def handle_trigger(self, trigger):
        """Own one decision from arrival through evidence, proposal, verification and durable records."""
        previous = self.store.db.execute(
            "SELECT payload FROM decisions WHERE trigger_event_id=?", (trigger["event_id"],)
        ).fetchone()
        if previous:
            self.store.log(
                ts(trigger["deliver_at"]),
                "duplicate_trigger",
                {"trigger_event_id": trigger["event_id"]},
                trigger["user_id"],
            )
            return json.loads(previous[0])
        budget = Budget(self.config)
        now = ts(trigger["deliver_at"])
        user_id = trigger["user_id"]
        base = {
            "decision_id": "D-" + trigger["event_id"],
            "user_id": user_id,
            "trigger_event_id": trigger["event_id"],
            "trigger": {
                "event": trigger["event"],
                "received_at": trigger["received_at"],
                "deliver_at": trigger["deliver_at"],
                "event_age_s": (
                    ts(trigger["received_at"]) - ts(trigger["occurred_at"])
                ).total_seconds(),
            },
            "goal": None,
            "actions": [],
            "status": "NO_ACTION",
            "reason": None,
            "assumptions": [],
            "evidence": [reference(trigger["source_ref"], now, now, "Trigger arrival")],
            "checks": {"passed": [], "failed": []},
            "confidence": {
                "level": "insufficient_evidence",
                "score": None,
                "reason": "No action yet justified.",
            },
            "unsupported_goals": UNSUPPORTED,
        }
        facts = None
        actions = []
        goal = None
        proposal = None
        fixture = None
        state = self.store.state(user_id)
        # This probe is required by the supplied fixture contract, including a holdout timeout.
        # Only the harness/provider sees fixtures. It cannot authorize an action.
        if trigger["event_id"] in self.provider.faults:
            try:
                fixture = self.provider.propose(
                    trigger, [], [], budget, self.incident("model_provider", now)
                )
                self.store.log(
                    now,
                    "fixture_model_output",
                    {"trigger_event_id": trigger["event_id"], "output": fixture},
                    user_id,
                )
            except DependencyFailure as error:
                fixture = error
                self.store.log(
                    now,
                    "fixture_model_failure",
                    {"trigger_event_id": trigger["event_id"], "reason": str(error)},
                    user_id,
                )
        # 1. Refresh outcomes, then retrieve only evidence visible at this arrival.
        try:
            self.refresh_goals(user_id, now)
            if not state or state["arm"] != "agent":
                base["reason"] = "holdout_or_unknown_viewer"
            else:
                facts = budget.tool(
                    "viewer_history_and_recent", self.context, user_id, now, trigger
                )
                base["assumptions"] = facts["assumptions"]
                base["evidence"] += facts["evidence"]
                base["dialect"] = {
                    "used": facts["dialect"],
                    "source": facts["dialect_source"],
                    "profile_value": (
                        facts["profile"].get("preferred_dialect") if facts["profile"] else None
                    ),
                    "conflicts": facts["conflicts"],
                    "ambiguities": facts["dialect_ambiguities"],
                    "confidence": {
                        "level": (
                            "qualified"
                            if facts["conflicts"]
                            else "supported" if facts["dialect"] else "unknown"
                        ),
                        "score": None,
                        "reason": "Event-stream settings take precedence; source times and conflicts remain visible.",
                    },
                }
                if activity_time(trigger, self.config["fresh_event_seconds"]) is None:
                    base["reason"] = "late_or_clock_skewed_trigger_not_a_live_visit"
                elif facts["dialect"] is None or facts["dialect_ambiguities"]:
                    base["reason"] = "effective_dialect_uncertain"
                else:
                    # 2. Continue the owned goal, or propose a new eligible goal.
                    current = self.store.open_goal(user_id)
                    candidates = budget.tool(
                        "eligible_goal_candidates",
                        eligible_candidates,
                        facts,
                        now,
                        ts(self.store.value("history_start")),
                        self.catalogue,
                        self.episodes,
                        self.config,
                        trigger,
                    )
                    started = {
                        row[0]
                        for row in self.store.db.execute(
                            "SELECT goal_id FROM goals WHERE user_id=?", (user_id,)
                        )
                    }
                    if current:
                        candidates = [
                            {
                                "goal_id": current["goal_id"],
                                "context": current["context"],
                                "reason": "continue_single_owned_goal",
                                "evidence_refs": current["evidence_refs"],
                            }
                        ]
                    else:
                        candidates = [
                            candidate
                            for candidate in candidates
                            if candidate["goal_id"] not in started
                        ]
                    preview = candidates[0] if candidates else {"goal_id": None, "context": {}}
                    titles = budget.tool(
                        "available_catalogue",
                        choose_titles,
                        facts,
                        self.catalogue,
                        self.episodes,
                        self.store.visible_controls(now),
                        now,
                        preview,
                    )
                    ids = [title["title_id"] for title in titles]
                    # 3. The provider proposes IDs; it cannot authorize an action.
                    if fixture is not None:
                        if isinstance(fixture, Exception):
                            raise fixture
                        output = fixture
                    else:
                        try:
                            output = self.provider.propose(
                                trigger,
                                candidates,
                                ids,
                                budget,
                                self.incident("model_provider", now),
                            )
                        except DependencyFailure as error:
                            raise ModelsUnavailable(
                                [{"provider": self.provider.name, "error": str(error)}]
                            ) from None
                    for attempt in (
                        output.get("provider_attempts", []) if isinstance(output, dict) else []
                    ):
                        if "error" in attempt:
                            self.store.log(
                                now,
                                "model_fallback",
                                {"reason": attempt["error"], "provider": attempt["provider"]},
                                user_id,
                            )
                    proposal = parse_proposal(output)
                    base["model_proposal"] = proposal
                    selected = next(
                        (
                            candidate
                            for candidate in candidates
                            if candidate["goal_id"] == proposal.get("goal_id")
                        ),
                        None,
                    )
                    if proposal.get("goal_id") is not None and selected is None:
                        base["reason"] = "model_goal_claim_has_no_eligibility_evidence"
                        base["checks"]["failed"].append("model_eligibility_claim")
                    elif selected is None:
                        base["reason"] = "no_supported_eligible_goal"
                    elif not isinstance(proposal.get("title_ids"), list) or any(
                        title_id not in ids for title_id in proposal["title_ids"]
                    ):
                        base["reason"] = "model_title_claim_has_no_catalogue_evidence"
                        base["checks"]["failed"].append("model_content_claim")
                    else:
                        rule = self.store.goal_rules(now)[selected["goal_id"]]
                        goal = current or {
                            "instance_id": "G-" + user_id + "-" + trigger["event_id"],
                            "user_id": user_id,
                            "goal_id": selected["goal_id"],
                            "started_at": iso(now),
                            "deadline": iso(now + timedelta(hours=rule["success"]["window_hours"])),
                            "rule": rule,
                            "context": selected["context"],
                            "status": "OPEN",
                            "evidence_refs": selected["evidence_refs"],
                        }
                        wanted = set(proposal["title_ids"])
                        titles = [title for title in titles if title["title_id"] in wanted]
                        # Re-rank for the selected goal if it differs from the first preview.
                        if selected["goal_id"] != preview["goal_id"]:
                            ranked = choose_titles(
                                facts,
                                self.catalogue,
                                self.episodes,
                                self.store.visible_controls(now),
                                now,
                                goal,
                            )
                            titles = [title for title in ranked if title["title_id"] in wanted]
                        actions, skipped = self.make_actions(trigger, goal, titles, facts, now)
                        base["skipped_interventions"] = skipped
                        base["goal"] = copy.deepcopy(goal)
                        base["reason"] = (
                            "continue_current_plan"
                            if not actions and current
                            else (
                                "insufficient_valid_content"
                                if not actions
                                else "evidence_backed_goal_and_action"
                            )
                        )
                        # 4. Re-check proposals independently before publication.
                        if actions:
                            publication = now + timedelta(seconds=budget.elapsed())
                            for action in actions:
                                action["published_at"] = iso(publication)
                                if action["type"] == "push":
                                    action["send_at"] = iso(publication)
                            checks, checked_facts = budget.tool(
                                "independent_verification",
                                verify,
                                self.store,
                                trigger,
                                goal,
                                actions,
                                publication,
                                self.config,
                                self.catalogue,
                                self.episodes,
                            )
                            base["checks"] = checks
                            if checks["failed"]:
                                actions = []
                                base["reason"] = "independent_verification_rejected"
                            else:
                                base["confidence"] = {
                                    "level": "supported_with_assumptions",
                                    "score": None,
                                    "reason": "Eligibility and action checks passed. Confidence is qualitative; reporting completeness and source disagreements are explicit.",
                                }
                                facts = checked_facts
        except ModelsUnavailable as error:
            error.trigger_event_id = trigger["event_id"]
            error.model_calls = budget.model_calls
            error.elapsed_seconds = budget.elapsed()
            raise
        except (DependencyFailure, ValueError, KeyError, TypeError) as error:
            actions = []
            base["reason"] = "safe_abstention: " + str(error)
            base["checks"]["failed"].append(type(error).__name__)
        # Final publication gate detects arrivals during processing and known time boundaries.
        publication = now + timedelta(seconds=budget.elapsed())
        actions = self.check_publication_boundary(actions, goal, facts, user_id, publication, base)
        if budget.elapsed() >= self.config["max_decision_seconds"]:
            actions = []
            base["reason"] = "decision_deadline_exceeded"
            base["checks"]["failed"].append("latency_budget")
        base["published_at"] = iso(publication)
        # The replay clock advances by actual measured processing time.
        # This is handler latency, not time elapsed since the fixture's calendar date.
        base["decided_in_ms"] = round((publication - now).total_seconds() * 1000, 3)
        base["timing"] = {
            "replay_arrived_at": trigger["deliver_at"],
            "decision_ready_at": iso(publication),
            "arrival_to_decision_ms": base["decided_in_ms"],
        }
        base["model_calls"] = budget.model_calls
        base["tool_calls"] = budget.tool_calls
        base["tool_trace"] = budget.trace
        base["actions"] = [action["action_id"] for action in actions]
        base["status"] = "PUBLISHED" if actions else "NO_ACTION"
        base["evidence"].append(
            reference(
                "replay/arms.csv#" + user_id,
                ts(self.store.value("baseline")),
                publication,
                "Randomized arm: " + (state["arm"] if state else "unknown"),
            )
        )
        if goal:
            correction = goal["rule"].get("correction")
            rule_time = ts(correction["at"]) if correction else ts(self.store.value("baseline"))
            rule_source = (
                correction["source_ref"] if correction else "config/goals.json#" + goal["goal_id"]
            )
            base["evidence"].append(
                reference(
                    rule_source, rule_time, publication, "Goal eligibility/success definition"
                )
            )
        for title_id in sorted(
            {title_id for action in actions for title_id in self.action_titles(action)}
        ):
            title = self.catalogue[title_id]
            base["evidence"].append(
                reference(
                    title["source_ref"],
                    ts(self.store.value("baseline")),
                    publication,
                    {
                        "title_id": title_id,
                        "available_from": title["available_from"],
                        "available_until": title["available_until"],
                        "checked_at": iso(publication),
                    },
                )
            )
        if actions and goal:
            if self.store.open_goal(user_id) is None:
                goal["started_at"] = iso(publication)
                goal["deadline"] = iso(
                    publication + timedelta(hours=goal["rule"]["success"]["window_hours"])
                )
                self.store.save_goal(goal)
            base["goal"] = copy.deepcopy(goal)
        elif goal and self.store.open_goal(user_id) is None:
            base["uncommitted_goal_candidate"] = goal["goal_id"]
            base["goal"] = None
        state = self.store.state(user_id)
        if state:
            if facts:
                state["facts"] = {
                    key: value
                    for key, value in facts.items()
                    if key not in ("events", "plays", "completions", "paid_history")
                }
            current = self.store.open_goal(user_id)
            state["active_goal_id"] = current["instance_id"] if current else None
            state["knowledge_as_of"] = iso(publication)
            state["last_decision_id"] = base["decision_id"]
            for action in actions:
                if action["type"] == "home_page":
                    title_ids = self.action_titles(action)
                    state["active_plan"] = {
                        "goal_instance_id": goal["instance_id"],
                        "page_action_id": action["action_id"],
                        "title_ids": title_ids,
                        "valid": True,
                    }
                    self.store.db.execute("DELETE FROM plan_titles WHERE user_id=?", (user_id,))
                    self.store.db.executemany(
                        "INSERT INTO plan_titles VALUES (?,?)",
                        [(title_id, user_id) for title_id in title_ids],
                    )
            self.store.save_state(user_id, state)
        self.store.db.execute(
            "INSERT INTO decisions VALUES (?,?,?,?)",
            (trigger["event_id"], user_id, iso(publication), json.dumps(base, allow_nan=False)),
        )
        for action in actions:
            day = (
                ts(action["send_at"]).astimezone(IST).date().isoformat()
                if action["type"] == "push"
                else None
            )
            self.store.db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
                (
                    action["action_id"],
                    base["decision_id"],
                    user_id,
                    action["type"],
                    iso(publication),
                    day,
                    json.dumps(action, allow_nan=False),
                ),
            )
        total = budget.elapsed() * 1000
        self.store.log(
            publication,
            "decision_processing",
            {
                "decision_id": base["decision_id"],
                "through_persistence_ms": round(total, 3),
                "model_calls": budget.model_calls,
                "tool_calls": budget.tool_calls,
            },
            user_id,
        )
        return base

    def check_publication_boundary(self, actions, goal, facts, user_id, publication, decision):
        """Reject actions if evidence or policy changed while the model was working.

        The earlier verifier checked a point in time. This last gate covers the
        gap before recording publication and annotates the decision if it fails.
        """
        if actions:
            checked_at = ts(actions[0]["published_at"])
            changed = self.store.db.execute(
                "SELECT 1 FROM events WHERE user_id=? AND available_at>? AND available_at<=? LIMIT 1",
                (user_id, iso(checked_at), iso(publication)),
            ).fetchone()
            new_control = self.store.db.execute(
                "SELECT 1 FROM controls WHERE at>? AND at<=? LIMIT 1",
                (iso(checked_at), iso(publication)),
            ).fetchone()
            expired = any(
                not available(
                    self.catalogue[title_id], publication, self.store.visible_controls(publication)
                )
                for action in actions
                for title_id in self.action_titles(action)
            )
            quiet = any(
                action["type"] == "push" and not 8 <= publication.astimezone(IST).hour < 22
                for action in actions
            )
            expired_goal = ts(goal["deadline"]) <= publication
            changed_profile = self.store.db.execute(
                "SELECT 1 FROM profiles WHERE user_id=? AND available_at>? AND available_at<=? LIMIT 1",
                (user_id, iso(checked_at), iso(publication)),
            ).fetchone()
            signup_boundary = False
            if (
                self.store.open_goal(user_id) is None
                and facts.get("signup_at")
                and goal["goal_id"] in ("G01", "G03")
            ):
                signup_boundary = publication - ts(facts["signup_at"]) > timedelta(
                    days=7 if goal["goal_id"] == "G01" else 2
                )
            release_day_boundary = (
                self.store.open_goal(user_id) is None
                and goal["goal_id"] == "G08"
                and ts(goal["started_at"]).date() != publication.date()
            )
            if (
                changed
                or new_control
                or expired
                or quiet
                or expired_goal
                or changed_profile
                or signup_boundary
                or release_day_boundary
            ):
                actions = []
                decision["reason"] = "publication_boundary_changed_replan_next_trigger"
                decision["checks"]["failed"].append("publication_boundary")
            else:
                for action in actions:
                    action["published_at"] = iso(publication)
                    if action["type"] == "push":
                        action["send_at"] = iso(publication)
        return actions

    @staticmethod
    def action_titles(action):
        if action["type"] == "push":
            return [action["title_id"]]
        return ([action["hero_title_id"]] if action.get("hero_title_id") else []) + [
            title_id for row in action.get("rows", []) for title_id in row["title_ids"]
        ]
