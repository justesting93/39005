"""Use cases sitting between the grid API and the assignment engine."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from roster.db import Database, Snapshot
from roster.demo import restore
from roster.engine import (
    HourStats,
    meets_requirement,
    meets_total,
    meets_training,
    plan_assignment,
    requirement_status,
)
from roster.errors import NotFound
from roster.models import (
    HOUR_TARGET,
    PARTIES,
    TRAINING_RATIO,
    Event,
    FinancialYear,
    Member,
    Training,
    parse_code,
    parse_dt,
)


def restore_demo(db: Database) -> None:
    restore(db)


def list_members(db: Database, fy_year: int) -> dict:
    snap = db.load()
    fy = FinancialYear(fy_year)
    totals = compute_totals(snap, fy)
    members = [_member_payload(member, totals[member.member_id]) for member in snap.members]
    offered = next(iter(totals.values())).training_offered if totals else 0.0
    return {"fy": fy.to_dict(), "training_offered": offered, "members": members}


def create_member(db: Database, payload: dict, fy_year: int) -> dict:
    member = Member(None, _required_text(payload, "member_id"), _party(payload), 1)
    member.validate()
    new_id = db.insert_member(member.member_id, member.party)
    return _one_member(db, new_id, fy_year)


def update_member(db: Database, member_id: int, payload: dict, fy_year: int) -> dict:
    snap = db.load()
    current = snap.member(member_id)
    if current is None:
        raise NotFound("Member not found")
    code = current.member_id if "member_id" not in payload else parse_code(payload["member_id"], "Member ID")
    party = current.party if "party" not in payload else _party(payload)
    order = None
    if "queue_order" in payload:
        order = _whole_number(payload["queue_order"], "Queue order")
        if order < 1:
            raise ValueError("Queue order must be at least 1")
    Member(current.id, code, party, order or current.queue_order).validate()
    db.update_member(member_id, member_code=code, party=party, queue_order=order)
    return _one_member(db, member_id, fy_year)


def move_member(db: Database, member_id: int, direction: str, fy_year: int) -> dict:
    db.move_member(member_id, direction)
    return _one_member(db, member_id, fy_year)


def delete_member(db: Database, member_id: int) -> None:
    db.delete_member(member_id)


def list_events(db: Database) -> dict:
    snap = db.load()
    conflicts = _conflict_ids(snap)
    return {"events": [_event_brief(event, snap, conflicts) for event in snap.events]}


def create_event(db: Database, payload: dict) -> dict:
    event = _event_from_payload(payload)
    event.validate()
    new_id = db.insert_event(
        event.duty_code, event.start_datetime, event.end_datetime, event.required_members
    )
    snap = db.load()
    created = snap.event(new_id)
    return _event_brief(created, snap, _conflict_ids(snap))


def update_event(db: Database, event_id: int, payload: dict) -> dict:
    snap = db.load()
    current = snap.event(event_id)
    if current is None:
        raise NotFound("Duty not found")
    merged = {
        "duty_code": payload.get("duty_code", current.duty_code),
        "start_datetime": payload.get("start_datetime", current.start_datetime),
        "end_datetime": payload.get("end_datetime", current.end_datetime),
        "required_members": payload.get("required_members", current.required_members),
    }
    event = _event_from_payload(merged, current)
    event.validate()
    db.update_event(
        event_id,
        duty_code=event.duty_code,
        start=event.start_datetime,
        end=event.end_datetime,
        required_members=event.required_members,
    )
    snap = db.load()
    return _event_brief(snap.event(event_id), snap, _conflict_ids(snap))


def delete_event(db: Database, event_id: int) -> None:
    db.delete_event(event_id)


def set_applications(db: Database, event_id: int, member_codes: list[str]) -> dict:
    if not isinstance(member_codes, list):
        raise ValueError("member_ids must be a list")
    db.set_applications(event_id, [parse_code(code, "Member ID") for code in member_codes])
    return workspace(db, event_id)


def workspace(db: Database, event_id: int) -> dict:
    snap = db.load()
    event = snap.event(event_id)
    if event is None:
        raise NotFound("Duty not found")
    previews = {
        "standard": _compute(snap, event, "standard"),
        "assign": _compute(snap, event, "assign"),
    }
    stale = _staleness(snap, event, previews)
    fy = FinancialYear.of(event.start_datetime)
    totals = compute_totals(snap, fy, exclude_event_id=event.id)
    standard_rows = {
        row["member_id"]: row for row in previews["standard"]["rows"]
    }
    members = []
    for member in sorted(snap.members, key=lambda item: (item.party, item.queue_order, item.member_id)):
        total = totals[member.member_id]
        stats = total.hour_stats()
        member.attend_hours = stats.attend_hours
        rank_row = standard_rows[member.member_id]
        members.append(
            {
                **_member_payload(member, total),
                "applied": member.member_id in snap.applications.get(event.id, set()),
                "clash_with": rank_row["clash_with"],
                "standard_rank": rank_row["standard_rank"],
                "party_priority": rank_row["party_priority"],
                "queue_position": rank_row["queue_position"],
            }
        )
    return {
        "event": _event_brief(event, snap, _conflict_ids(snap)),
        "fy": fy.to_dict(),
        "rolling": {
            "party_order": previews["standard"]["party_order_before"],
            "queues": previews["standard"]["queues_before"],
        },
        "earlier": previews["standard"]["earlier"],
        "later": previews["standard"]["later"],
        "members": members,
        "previews": previews,
        "saved": event.assignment_log,
        "saved_stale": stale["stale"],
        "saved_stale_reasons": stale["reasons"],
    }


def commit_assignment(db: Database, event_id: int, method: str) -> dict:
    if method not in ("standard", "assign"):
        raise ValueError("Method must be standard or assign")
    snap = db.load()
    event = snap.event(event_id)
    if event is None:
        raise NotFound("Duty not found")
    payload = _compute(snap, event, method)
    db.set_assignment(
        event_id,
        payload["selected"],
        method,
        json.dumps(payload),
        datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )
    return {"plan": payload, "workspace": workspace(db, event_id)}


def clear_assignment(db: Database, event_id: int) -> dict:
    db.clear_assignment(event_id)
    return workspace(db, event_id)


def rerun_forward(db: Database, event_id: int) -> dict:
    snap = db.load()
    origin = snap.event(event_id)
    if origin is None:
        raise NotFound("Duty not found")
    if not origin.assignment_method:
        raise ValueError("Assign this duty before re-running later ones")
    targets = [
        (event.id, event.assignment_method, event.duty_code)
        for event in snap.events
        if event.assignment_method
        and (event.start_datetime, event.id) >= (origin.start_datetime, origin.id)
    ]
    results = []
    for target_id, method, duty_code in targets:
        plan = commit_assignment(db, target_id, method)["plan"]
        results.append(
            {"id": target_id, "duty_code": duty_code, "method": method, "selected": plan["selected"]}
        )
    return {"results": results, "workspace": workspace(db, event_id)}


def list_trainings(db: Database) -> dict:
    snap = db.load()
    return {"trainings": [_training_payload(item, snap) for item in snap.trainings]}


def create_training(db: Database, payload: dict) -> dict:
    training = _training_from_payload(payload)
    training.validate()
    new_id = db.insert_training(training.training_code, training.start_datetime, training.end_datetime)
    snap = db.load()
    return _training_payload(snap.training(new_id), snap)


def update_training(db: Database, training_id: int, payload: dict) -> dict:
    snap = db.load()
    current = snap.training(training_id)
    if current is None:
        raise NotFound("Training not found")
    merged = {
        "training_code": payload.get("training_code", current.training_code),
        "start_datetime": payload.get("start_datetime", current.start_datetime),
        "end_datetime": payload.get("end_datetime", current.end_datetime),
    }
    training = _training_from_payload(merged)
    training.validate()
    db.update_training(
        training_id,
        training_code=training.training_code,
        start=training.start_datetime,
        end=training.end_datetime,
    )
    snap = db.load()
    return _training_payload(snap.training(training_id), snap)


def delete_training(db: Database, training_id: int) -> None:
    db.delete_training(training_id)


def set_attendees(db: Database, training_id: int, member_codes: list[str]) -> dict:
    if not isinstance(member_codes, list):
        raise ValueError("member_ids must be a list")
    db.set_attendees(training_id, [parse_code(code, "Member ID") for code in member_codes])
    snap = db.load()
    training = snap.training(training_id)
    if training is None:
        raise NotFound("Training not found")
    return _training_payload(training, snap)


def selection_report(db: Database, event_id: int | None = None) -> dict:
    snap = db.load()
    events = snap.events
    if event_id is not None:
        events = [event for event in events if event.id == event_id]
        if not events:
            raise NotFound("Duty not found")
    items = []
    conflicts = _conflict_ids(snap)
    for event in events:
        previews = None
        if event.assignment_method:
            previews = {event.assignment_method: _compute(snap, event, event.assignment_method)}
        items.append(
            {
                "event": _event_brief(event, snap, conflicts),
                "decided": event.assignment_log is not None,
                "log": event.assignment_log,
                "stale": _staleness(snap, event, previews),
            }
        )
    return {"events": items}


def hours_report(db: Database, fy_year: int) -> dict:
    snap = db.load()
    fy = FinancialYear(fy_year)
    totals = compute_totals(snap, fy)
    offered = next(iter(totals.values())).training_offered if totals else 0.0
    rows = []
    for member in snap.members:
        total = totals[member.member_id]
        stats = total.hour_stats()
        member.attend_hours = stats.attend_hours
        rows.append(
            {
                **_member_payload(member, total),
                "hour_shortfall": round(max(0.0, HOUR_TARGET - stats.attend_hours), 4),
                "training_target": round(TRAINING_RATIO * offered, 4),
                "training_percent": (
                    round(100.0 * stats.training_hours / offered, 1) if offered else None
                ),
                "duties": total.duties,
                "trainings": total.trainings,
            }
        )
    met = sum(1 for row in rows if row["meets_requirement"])
    return {
        "fy": fy.to_dict(),
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "hour_target": HOUR_TARGET,
        "training_ratio": TRAINING_RATIO,
        "training_offered": offered,
        "training_target": round(TRAINING_RATIO * offered, 4),
        "member_count": len(rows),
        "met_count": met,
        "short_count": len(rows) - met,
        "rows": rows,
    }


def meta(db: Database) -> dict:
    snap = db.load()
    years = {FinancialYear.current().year - 1, FinancialYear.current().year, FinancialYear.current().year + 1}
    for event in snap.events:
        years.add(FinancialYear.of(event.start_datetime).year)
    for training in snap.trainings:
        years.add(FinancialYear.of(training.start_datetime).year)
    return {
        "parties": list(PARTIES),
        "hour_target": HOUR_TARGET,
        "training_ratio": TRAINING_RATIO,
        "current_fy": FinancialYear.current().year,
        "fy_years": sorted(years),
    }


class MemberTotals:
    def __init__(self):
        self.training_hours = 0.0
        self.duty_hours = 0.0
        self.training_offered = 0.0
        self.duty_count = 0
        self.duties: list[dict] = []
        self.trainings: list[dict] = []

    def hour_stats(self) -> HourStats:
        return HourStats(
            training_hours=self.training_hours,
            duty_hours=self.duty_hours,
            training_offered=self.training_offered,
            duty_count=self.duty_count,
        )


def compute_totals(
    snap: Snapshot,
    fy: FinancialYear,
    exclude_event_id: int | None = None,
) -> dict[str, MemberTotals]:
    totals = {member.member_id: MemberTotals() for member in snap.members}
    offered = 0.0
    for training in snap.trainings:
        if not fy.contains(training.start_datetime):
            continue
        offered = round(offered + training.hours, 4)
        detail = {
            "training_code": training.training_code,
            "hours": training.hours,
            "start_datetime": training.start_datetime.strftime("%Y-%m-%dT%H:%M"),
        }
        for member_id in snap.attendance.get(training.id, ()):
            if member_id not in totals:
                continue
            totals[member_id].training_hours = round(
                totals[member_id].training_hours + training.hours, 4
            )
            totals[member_id].trainings.append(detail)
    for event in snap.events:
        if event.id == exclude_event_id or not fy.contains(event.start_datetime):
            continue
        for member_id in snap.assignments.get(event.id, ()):
            if member_id not in totals:
                continue
            totals[member_id].duty_hours = round(totals[member_id].duty_hours + event.hours, 4)
            totals[member_id].duty_count += 1
            totals[member_id].duties.append(
                {
                    "duty_code": event.duty_code,
                    "hours": event.hours,
                    "start_datetime": event.start_datetime.strftime("%Y-%m-%dT%H:%M"),
                }
            )
    for item in totals.values():
        item.training_offered = offered
    return totals


def _compute(snap: Snapshot, event: Event, method: str) -> dict:
    fy = FinancialYear.of(event.start_datetime)
    totals = compute_totals(snap, fy, exclude_event_id=event.id)
    stats = {member_id: item.hour_stats() for member_id, item in totals.items()}
    earlier_assigned = []
    earlier_meta = []
    later_meta = []
    overlapping: dict[str, list[str]] = defaultdict(list)
    for other in snap.events:
        if other.id == event.id:
            continue
        assigned = snap.assignments.get(other.id, [])
        if not assigned:
            continue
        if event.overlaps(other):
            for member_id in assigned:
                overlapping[member_id].append(other.duty_code)
        item = {
            "duty_code": other.duty_code,
            "start_datetime": other.start_datetime.strftime("%Y-%m-%dT%H:%M"),
            "method": other.assignment_method,
            "member_ids": list(assigned),
        }
        if (other.start_datetime, other.id) < (event.start_datetime, event.id):
            earlier_assigned.append((other, assigned))
            earlier_meta.append(item)
        else:
            later_meta.append(item)
    plan = plan_assignment(
        method=method,
        event=event,
        members=list(snap.members),
        applied_ids=set(snap.applications.get(event.id, set())),
        earlier_assigned=earlier_assigned,
        overlapping={key: sorted(value) for key, value in overlapping.items()},
        stats=stats,
    )
    payload = plan.to_dict()
    payload["financial_year"] = fy.to_dict()
    payload["applied_member_ids"] = sorted(snap.applications.get(event.id, set()))
    payload["earlier"] = earlier_meta
    payload["later"] = later_meta
    payload["rules"] = {"hour_target": HOUR_TARGET, "training_ratio": TRAINING_RATIO}
    return payload


def _staleness(snap: Snapshot, event: Event, previews: dict | None) -> dict:
    log = event.assignment_log
    if not log:
        return {"stale": False, "reasons": []}
    reasons = []
    current_applied = set(snap.applications.get(event.id, set()))
    if set(log.get("applied_member_ids") or []) != current_applied:
        reasons.append("Applications changed after this decision.")
    if list(log.get("selected") or []) != list(snap.assignments.get(event.id, [])):
        reasons.append("The stored names no longer match the decision record.")
    if previews and event.assignment_method in previews:
        fresh = previews[event.assignment_method]["selected"]
        if fresh != list(log.get("selected") or []):
            reasons.append(
                "Running this method again would select different members, because hours, duties, or earlier assignments changed."
            )
    # Preserve order while dropping duplicate sentences.
    unique = list(dict.fromkeys(reasons))
    return {"stale": bool(unique), "reasons": unique}


def _member_payload(member: Member, total: MemberTotals) -> dict:
    stats = total.hour_stats()
    member.attend_hours = stats.attend_hours
    return {
        "id": member.id,
        "member_id": member.member_id,
        "party": member.party,
        "queue_order": member.queue_order,
        "attend_hours": stats.attend_hours,
        "training_hours": stats.training_hours,
        "duty_hours": stats.duty_hours,
        "training_offered": stats.training_offered,
        "duty_count": stats.duty_count,
        "meets_total": meets_total(stats),
        "meets_training": meets_training(stats),
        "meets_requirement": meets_requirement(stats),
        "status": requirement_status(stats),
    }


def _one_member(db: Database, member_id: int, fy_year: int) -> dict:
    payload = list_members(db, fy_year)
    match = next(item for item in payload["members"] if item["id"] == member_id)
    return match


def _event_brief(event: Event, snap: Snapshot, conflicts: set[int]) -> dict:
    assigned = snap.assignments.get(event.id, [])
    brief = event.to_dict()
    brief["assigned_member_ids"] = assigned
    brief["applicant_count"] = len(snap.applications.get(event.id, ()))
    brief["conflict"] = event.id in conflicts
    return brief


def _training_payload(training: Training, snap: Snapshot) -> dict:
    payload = training.to_dict()
    payload["attendee_ids"] = sorted(snap.attendance.get(training.id, set()))
    return payload


def _conflict_ids(snap: Snapshot) -> set[int]:
    assigned = [event for event in snap.events if snap.assignments.get(event.id)]
    conflicts = set()
    for index, left in enumerate(assigned):
        for right in assigned[index + 1 :]:
            if not left.overlaps(right):
                continue
            shared = set(snap.assignments[left.id]) & set(snap.assignments[right.id])
            if shared:
                conflicts.add(left.id)
                conflicts.add(right.id)
    return conflicts


def _event_from_payload(payload: dict, existing: Event | None = None) -> Event:
    return Event(
        existing.id if existing else None,
        parse_code(_required(payload, "duty_code"), "Duty code")
        if not isinstance(payload.get("duty_code"), datetime)
        else existing.duty_code,
        parse_dt(_required(payload, "start_datetime"), "Duty start"),
        parse_dt(_required(payload, "end_datetime"), "Duty end"),
        _whole_number(_required(payload, "required_members"), "Required number of members"),
        existing.assignment_method if existing else None,
        existing.assignment_log if existing else None,
        existing.assigned_at if existing else None,
    )


def _training_from_payload(payload: dict) -> Training:
    return Training(
        None,
        parse_code(_required(payload, "training_code"), "Training code"),
        parse_dt(_required(payload, "start_datetime"), "Training start"),
        parse_dt(_required(payload, "end_datetime"), "Training end"),
    )


def _party(payload: dict) -> str:
    party = payload.get("party")
    if party not in PARTIES:
        raise ValueError("Duty party must be DP1, DP2, or DP3")
    return party


def _required(payload: dict, key: str):
    if key not in payload:
        raise ValueError(f"{key} is required")
    return payload[key]


def _required_text(payload: dict, key: str) -> str:
    value = _required(payload, key)
    if not isinstance(value, str):
        raise ValueError(f"{key} is required")
    return value


def _whole_number(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number")
    return value
