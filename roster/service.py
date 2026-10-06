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
    credited_hours,
    format_dt,
    parse_attend_hours,
    parse_code,
    parse_dt,
    parse_name,
    parse_remarks,
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
    member = Member(
        None,
        _required_text(payload, "member_id"),
        _party(payload),
        1,
        parse_name(payload.get("name", "")),
    )
    member.validate()
    new_id = db.insert_member(member.member_id, member.party, member.name)
    return _one_member(db, new_id, fy_year)


def update_member(db: Database, member_id: int, payload: dict, fy_year: int) -> dict:
    snap = db.load()
    current = snap.member(member_id)
    if current is None:
        raise NotFound("Member not found")
    code = current.member_id if "member_id" not in payload else parse_code(payload["member_id"], "Member ID")
    name = current.name if "name" not in payload else parse_name(payload.get("name"))
    party = current.party if "party" not in payload else _party(payload)
    order = None
    if "queue_order" in payload:
        order = _whole_number(payload["queue_order"], "Queue order")
        if order < 1:
            raise ValueError("Queue order must be at least 1")
    Member(current.id, code, party, order or current.queue_order, name).validate()
    db.update_member(member_id, member_code=code, name=name, party=party, queue_order=order)
    return _one_member(db, member_id, fy_year)


def import_members(db: Database, rows: object, fy_year: int) -> dict:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Import needs at least one member")
    prepared: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index} is not a member")
        raw_id = row.get("member_id", "")
        code = parse_code(raw_id if isinstance(raw_id, str) else "", f"Member ID on row {index}")
        key = code.casefold()
        if key in seen:
            raise ValueError(f"Member ID {code} is repeated in the file")
        seen.add(key)
        prepared.append((code, parse_name(row.get("name", ""))))
    by_code = {member.member_id.casefold(): member for member in db.load().members}
    created = 0
    updated = 0
    for code, name in prepared:
        existing = by_code.get(code.casefold())
        if existing is None:
            db.insert_member(code, "DP1", name)
            created += 1
        else:
            db.update_member(existing.id, name=name)
            updated += 1
    return {
        "created": created,
        "updated": updated,
        "members": list_members(db, fy_year)["members"],
    }


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
        event.duty_code,
        event.start_datetime,
        event.end_datetime,
        event.required_members,
        event.remarks,
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
        "remarks": payload.get("remarks", current.remarks),
    }
    event = _event_from_payload(merged, current)
    event.validate()
    db.update_event(
        event_id,
        duty_code=event.duty_code,
        start=event.start_datetime,
        end=event.end_datetime,
        required_members=event.required_members,
        remarks=event.remarks,
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
                "assigned": member.member_id in snap.assignments.get(event.id, []),
                "duty_attend_hours": credited_hours(
                    snap.assignment_hours.get(event.id, {}).get(member.member_id),
                    event.hours,
                )
                if member.member_id in snap.assignments.get(event.id, [])
                else None,
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
    new_id = db.insert_training(
        training.training_code, training.start_datetime, training.end_datetime, training.remarks
    )
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
        "remarks": payload.get("remarks", current.remarks),
    }
    training = _training_from_payload(merged)
    training.validate()
    db.update_training(
        training_id,
        training_code=training.training_code,
        start=training.start_datetime,
        end=training.end_datetime,
        remarks=training.remarks,
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


def set_duty_attend_hours(db: Database, event_id: int, member_code: str, raw_hours: object) -> dict:
    snap = db.load()
    event = snap.event(event_id)
    if event is None:
        raise NotFound("Duty not found")
    code = parse_code(member_code, "Member ID")
    db.set_assignment_hours(event_id, code, parse_attend_hours(raw_hours, event.hours))
    return workspace(db, event_id)


def set_training_attend_hours(db: Database, training_id: int, member_code: str, raw_hours: object) -> dict:
    snap = db.load()
    training = snap.training(training_id)
    if training is None:
        raise NotFound("Training not found")
    code = parse_code(member_code, "Member ID")
    db.set_attendance_hours(training_id, code, parse_attend_hours(raw_hours, training.hours))
    snap = db.load()
    return _training_payload(snap.training(training_id), snap)


def import_events(db: Database, rows: object) -> dict:
    prepared = _import_rows(rows, "duty")
    by_code = {event.duty_code.casefold(): event for event in db.load().events}
    created = 0
    updated = 0
    for item in prepared:
        existing = by_code.get(item.duty_code.casefold())
        if existing is None:
            db.insert_event(
                item.duty_code, item.start_datetime, item.end_datetime, item.required_members, item.remarks
            )
            created += 1
        else:
            db.update_event(
                existing.id,
                duty_code=item.duty_code,
                start=item.start_datetime,
                end=item.end_datetime,
                required_members=item.required_members,
                remarks=item.remarks,
            )
            updated += 1
    return {"created": created, "updated": updated, "events": list_events(db)["events"]}


def import_trainings(db: Database, rows: object) -> dict:
    prepared = _import_training_rows(rows)
    by_code = {item.training_code.casefold(): item for item in db.load().trainings}
    created = 0
    updated = 0
    for item in prepared:
        existing = by_code.get(item.training_code.casefold())
        if existing is None:
            db.insert_training(item.training_code, item.start_datetime, item.end_datetime, item.remarks)
            created += 1
        else:
            db.update_training(
                existing.id,
                training_code=item.training_code,
                start=item.start_datetime,
                end=item.end_datetime,
                remarks=item.remarks,
            )
            updated += 1
    return {"created": created, "updated": updated, "trainings": list_trainings(db)["trainings"]}


def export_roster(db: Database) -> dict:
    snap = db.load()
    return {
        "members": [
            {
                "member_id": member.member_id,
                "name": member.name,
                "party": member.party,
                "queue_order": member.queue_order,
            }
            for member in snap.members
        ],
        "duties": [
            {
                "duty_code": event.duty_code,
                "start_datetime": format_dt(event.start_datetime),
                "end_datetime": format_dt(event.end_datetime),
                "required_members": event.required_members,
                "remarks": event.remarks,
                "applied": sorted(snap.applications.get(event.id, ())),
                "assigned": [
                    {
                        "member_id": member_id,
                        "hours": snap.assignment_hours.get(event.id, {}).get(member_id),
                    }
                    for member_id in snap.assignments.get(event.id, [])
                ],
                "assignment_method": event.assignment_method,
                "assignment_log": event.assignment_log,
                "assigned_at": event.assigned_at,
            }
            for event in snap.events
        ],
        "trainings": [
            {
                "training_code": training.training_code,
                "start_datetime": format_dt(training.start_datetime),
                "end_datetime": format_dt(training.end_datetime),
                "remarks": training.remarks,
                "attendees": [
                    {"member_id": member_id, "hours": stored}
                    for member_id, stored in sorted(snap.attendance.get(training.id, {}).items())
                ],
            }
            for training in snap.trainings
        ],
    }


def import_roster(db: Database, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Saved roster must be an object")
    members = _roster_members(payload.get("members", []))
    known = {item["member_code"].casefold() for item in members}
    duties = _roster_duties(payload.get("duties", []), known)
    trainings = _roster_trainings(payload.get("trainings", []), known)
    db.replace_roster(members, duties, trainings)
    return export_roster(db)


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
        attended = snap.attendance.get(training.id, {})
        for member_id, stored in attended.items():
            if member_id not in totals:
                continue
            attended_hours = credited_hours(stored, training.hours)
            totals[member_id].training_hours = round(
                totals[member_id].training_hours + attended_hours, 4
            )
            totals[member_id].trainings.append(
                {
                    "training_code": training.training_code,
                    "hours": training.hours,
                    "attend_hours": attended_hours,
                    "remarks": training.remarks,
                    "start_datetime": training.start_datetime.strftime("%Y-%m-%dT%H:%M"),
                    "end_datetime": training.end_datetime.strftime("%Y-%m-%dT%H:%M"),
                }
            )
    for event in snap.events:
        if event.id == exclude_event_id or not fy.contains(event.start_datetime):
            continue
        stored_hours = snap.assignment_hours.get(event.id, {})
        for member_id in snap.assignments.get(event.id, ()):
            if member_id not in totals:
                continue
            attended_hours = credited_hours(stored_hours.get(member_id), event.hours)
            totals[member_id].duty_hours = round(totals[member_id].duty_hours + attended_hours, 4)
            totals[member_id].duty_count += 1
            totals[member_id].duties.append(
                {
                    "duty_code": event.duty_code,
                    "hours": event.hours,
                    "attend_hours": attended_hours,
                    "remarks": event.remarks,
                    "start_datetime": event.start_datetime.strftime("%Y-%m-%dT%H:%M"),
                    "end_datetime": event.end_datetime.strftime("%Y-%m-%dT%H:%M"),
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
        assigned = list(snap.assignments.get(other.id, []))
        if assigned and event.overlaps(other):
            for member_id in assigned:
                overlapping[member_id].append(other.duty_code)
        item = {
            "duty_code": other.duty_code,
            "start_datetime": other.start_datetime.strftime("%Y-%m-%dT%H:%M"),
            "method": other.assignment_method,
            "member_ids": assigned,
        }
        if (other.start_datetime, other.id) < (event.start_datetime, event.id):
            earlier_assigned.append((other, assigned))
            earlier_meta.append(item)
        elif assigned:
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
        "name": member.name,
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
    attended = snap.attendance.get(training.id, {})
    payload["attendee_ids"] = sorted(attended)
    payload["attendees"] = [
        {"member_id": member_id, "hours": credited_hours(stored, training.hours)}
        for member_id, stored in sorted(attended.items())
    ]
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
        parse_remarks(payload.get("remarks", existing.remarks if existing else "")),
    )


def _training_from_payload(payload: dict) -> Training:
    return Training(
        None,
        parse_code(_required(payload, "training_code"), "Training code"),
        parse_dt(_required(payload, "start_datetime"), "Training start"),
        parse_dt(_required(payload, "end_datetime"), "Training end"),
        parse_remarks(payload.get("remarks", "")),
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


def _import_rows(rows: object, kind: str) -> list[Event]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Import needs at least one duty" if kind == "duty" else "Import needs at least one training")
    prepared: list[Event] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index} is not a duty")
        required = row.get("required_members")
        if isinstance(required, str) and required.strip().isdigit():
            required = int(required.strip())
        event = Event(
            None,
            parse_code(row.get("duty_code") if isinstance(row.get("duty_code"), str) else "", f"Duty code on row {index}"),
            parse_dt(row.get("start_datetime"), f"Duty start on row {index}"),
            parse_dt(row.get("end_datetime"), f"Duty end on row {index}"),
            _whole_number(required, f"Required members on row {index}"),
            remarks=parse_remarks(row.get("remarks", "")),
        )
        event.validate()
        key = event.duty_code.casefold()
        if key in seen:
            raise ValueError(f"Duty code {event.duty_code} is repeated in the file")
        seen.add(key)
        prepared.append(event)
    return prepared


def _import_training_rows(rows: object) -> list[Training]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Import needs at least one training")
    prepared: list[Training] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {index} is not a training")
        training = Training(
            None,
            parse_code(
                row.get("training_code") if isinstance(row.get("training_code"), str) else "",
                f"Training code on row {index}",
            ),
            parse_dt(row.get("start_datetime"), f"Training start on row {index}"),
            parse_dt(row.get("end_datetime"), f"Training end on row {index}"),
            parse_remarks(row.get("remarks", "")),
        )
        training.validate()
        key = training.training_code.casefold()
        if key in seen:
            raise ValueError(f"Training code {training.training_code} is repeated in the file")
        seen.add(key)
        prepared.append(training)
    return prepared


def _roster_members(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("Saved roster members must be a list")
    prepared = []
    seen: set[str] = set()
    next_order = {"DP1": 1, "DP2": 1, "DP3": 1}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Member {index} is not an object")
        code = parse_code(row.get("member_id") if isinstance(row.get("member_id"), str) else "", f"Member ID {index}")
        party = row.get("party")
        if party not in PARTIES:
            raise ValueError("Duty party must be DP1, DP2, or DP3")
        key = code.casefold()
        if key in seen:
            raise ValueError(f"Member ID {code} is repeated in the file")
        seen.add(key)
        order = row.get("queue_order", next_order[party])
        if isinstance(order, bool) or not isinstance(order, int) or order < 1:
            raise ValueError(f"Queue order for {code} must be a whole number of at least 1")
        next_order[party] = max(next_order[party], order + 1)
        member = Member(None, code, party, order, parse_name(row.get("name", "")))
        member.validate()
        prepared.append(
            {"member_code": member.member_id, "name": member.name, "party": member.party, "queue_order": order}
        )
    return prepared


def _roster_people(value: object, known: set[str], label: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    people = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            code = parse_code(item, "Member ID")
            hours = None
        elif isinstance(item, dict):
            raw = item.get("member_id", "")
            code = parse_code(raw if isinstance(raw, str) else "", "Member ID")
            hours = item.get("hours")
        else:
            raise ValueError(f"{label} has a member that is not an object")
        if code.casefold() not in known:
            raise ValueError(f"Member {code} is not in the saved roster")
        if code.casefold() in seen:
            raise ValueError(f"Member {code} is repeated in {label}")
        seen.add(code.casefold())
        people.append({"member_code": code, "hours": hours})
    return people


def _roster_duties(rows: object, known: set[str]) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("Saved duties must be a list")
    prepared = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Duty {index} is not an object")
        event = _import_rows([row], "duty")[0]
        key = event.duty_code.casefold()
        if key in seen:
            raise ValueError(f"Duty code {event.duty_code} is repeated in the file")
        seen.add(key)
        applied = _roster_people(row.get("applied", []), known, f"Applied list for {event.duty_code}")
        assigned = _roster_people(row.get("assigned", []), known, f"Assigned list for {event.duty_code}")
        for person in assigned:
            person["hours"] = parse_attend_hours(person["hours"], event.hours)
        method = row.get("assignment_method")
        if method not in (None, "standard", "assign"):
            raise ValueError("Assignment method must be standard or assign")
        log = row.get("assignment_log")
        if log is not None and not isinstance(log, dict):
            raise ValueError(f"Assignment record for {event.duty_code} must be an object")
        prepared.append(
            {
                "duty_code": event.duty_code,
                "start_dt": format_dt(event.start_datetime),
                "end_dt": format_dt(event.end_datetime),
                "required_members": event.required_members,
                "remarks": event.remarks,
                "applied": [person["member_code"] for person in applied],
                "assigned": assigned,
                "assignment_method": method,
                "assignment_log": None if log is None else json.dumps(log),
                "assigned_at": row.get("assigned_at") if isinstance(row.get("assigned_at"), str) else None,
            }
        )
    return prepared


def _roster_trainings(rows: object, known: set[str]) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("Saved training must be a list")
    prepared = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Training {index} is not an object")
        training = _import_training_rows([row])[0]
        key = training.training_code.casefold()
        if key in seen:
            raise ValueError(f"Training code {training.training_code} is repeated in the file")
        seen.add(key)
        attendees = _roster_people(row.get("attendees", []), known, f"Attendance for {training.training_code}")
        for person in attendees:
            person["hours"] = parse_attend_hours(person["hours"], training.hours)
        prepared.append(
            {
                "training_code": training.training_code,
                "start_dt": format_dt(training.start_datetime),
                "end_dt": format_dt(training.end_datetime),
                "remarks": training.remarks,
                "attendees": attendees,
            }
        )
    return prepared


def _whole_number(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number")
    return value
