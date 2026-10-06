"""Standard rolling assignment and the three-priority Assign method.

Standard walks a rolling list. Each duty, in start order, gives the turn to
the next duty party: the first duty is DP1, the next is DP2, then DP3, then
DP1 again. A duty uses its turn even when nobody is assigned. Inside the party
on turn, members form a queue. A member is taken only if they applied and they
are not already on an overlapping duty. Taken members move to the back of their
own party queue. If that party cannot fill the duty, the next party in the turn
is used.

Assign uses the same party turn, then the same eligible members inside a party,
ordered by:
1. Annual hours still open, fewest attend hours first (training plus duty).
2. Fewer duties already taken in that financial year.
3. The member queue inside the party.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from roster.models import HOUR_TARGET, PARTIES, TRAINING_RATIO, Event, Member

PARTIES_LIST = list(PARTIES)


@dataclass(frozen=True)
class HourStats:
    training_hours: float = 0.0
    duty_hours: float = 0.0
    training_offered: float = 0.0
    duty_count: int = 0

    @property
    def attend_hours(self) -> float:
        return round(self.training_hours + self.duty_hours, 4)


def meets_total(stats: HourStats) -> bool:
    return stats.attend_hours + 1e-6 >= HOUR_TARGET


def meets_training(stats: HourStats) -> bool:
    if stats.training_offered <= 0:
        return True
    return stats.training_hours / stats.training_offered + 1e-9 >= TRAINING_RATIO


def meets_requirement(stats: HourStats) -> bool:
    return meets_total(stats) and meets_training(stats)


def requirement_status(stats: HourStats) -> str:
    total_ok = meets_total(stats)
    training_ok = meets_training(stats)
    if total_ok and training_ok:
        return "Met"
    if not total_ok and not training_ok:
        return "Short of 60h and training"
    if not total_ok:
        return "Short of 60h"
    return "Short of training"


def fmt_hours(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text if text else "0"


def initial_queues(members: list[Member]) -> dict[str, list[str]]:
    queues = {party: [] for party in PARTIES}
    ordered = sorted(
        members,
        key=lambda member: (PARTIES.index(member.party), member.queue_order, member.member_id),
    )
    for member in ordered:
        queues[member.party].append(member.member_id)
    return queues


def apply_rotation(
    queues: dict[str, list[str]],
    party_order: list[str],
    selected: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Move assigned members to the back of their party and advance the party turn.

    Every duty advances the party turn, including a duty with nobody assigned.
    Member queues move only for the people who were actually taken.
    """
    selected_set = set(selected)
    rotated = {}
    for party in PARTIES:
        current = list(queues.get(party, []))
        rest = [member_id for member_id in current if member_id not in selected_set]
        taken = [member_id for member_id in current if member_id in selected_set]
        rotated[party] = rest + taken
    next_order = list(party_order[1:]) + list(party_order[:1])
    return rotated, next_order


def rolling_before(
    members: list[Member],
    earlier_assigned: list[tuple[Event, list[str]]],
) -> tuple[dict[str, list[str]], list[str]]:
    queues = initial_queues(members)
    party_order = list(PARTIES_LIST)
    ordered = sorted(
        earlier_assigned,
        key=lambda item: (item[0].start_datetime, item[0].id or 0),
    )
    for _event, selected in ordered:
        queues, party_order = apply_rotation(queues, party_order, selected)
    return queues, party_order


def standard_sequence(queues: dict[str, list[str]], party_order: list[str]) -> list[str]:
    sequence = []
    for party in party_order:
        sequence.extend(queues.get(party, []))
    return sequence


def priority_key(
    requirement_met: bool,
    attend_hours: float,
    duty_count: int,
    standard_rank: int,
) -> tuple:
    """Lower sorts first. Open hours outrank duty count, which outranks the rolling list."""
    return (
        0 if not requirement_met else 1,
        attend_hours if not requirement_met else 0.0,
        duty_count,
        standard_rank,
    )


@dataclass
class DecisionRow:
    member_id: str
    party: str
    applied: bool
    eligible: bool
    selected: bool
    pick_index: int | None
    assign_rank: int | None
    outcome: str
    clash_with: list[str]
    attend_hours: float
    training_hours: float
    duty_hours: float
    training_offered: float
    meets_total: bool
    meets_training: bool
    meets_requirement: bool
    duty_count: int
    standard_rank: int
    party_priority: int
    queue_position: int
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AssignmentPlan:
    method: str
    duty_code: str
    required_members: int
    selected: list[str]
    shortfall: int
    party_order_before: list[str]
    queues_before: dict[str, list[str]]
    party_order_after: list[str]
    queues_after: dict[str, list[str]]
    summary: str
    rolling_note: str
    generated_at: str
    rows: list[DecisionRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def plan_assignment(
    method: str,
    event: Event,
    members: list[Member],
    applied_ids: set[str],
    earlier_assigned: list[tuple[Event, list[str]]],
    overlapping: dict[str, list[str]],
    stats: dict[str, HourStats],
) -> AssignmentPlan:
    if method not in ("standard", "assign"):
        raise ValueError("Method must be standard or assign")

    earlier = [
        (prior, selected)
        for prior, selected in earlier_assigned
        if prior.id != event.id
    ]
    queues, party_order = rolling_before(members, earlier)
    sequence = standard_sequence(queues, party_order)
    rank_of = {member_id: index + 1 for index, member_id in enumerate(sequence)}
    party_priority = {party: index + 1 for index, party in enumerate(party_order)}
    queue_position = {
        member_id: index + 1
        for party in PARTIES
        for index, member_id in enumerate(queues.get(party, []))
    }

    eligible: list[Member] = []
    classified: list[tuple[Member, bool, list[str]]] = []
    for member in members:
        if member.member_id not in rank_of:
            raise ValueError(f"{member.member_id} is missing from the rolling list")
        clash = sorted(overlapping.get(member.member_id, []))
        applied = member.member_id in applied_ids
        classified.append((member, applied, clash))
        if applied and not clash:
            eligible.append(member)

    def sort_key(member: Member) -> tuple:
        member_stats = stats.get(member.member_id, HourStats())
        if method == "standard":
            return (rank_of[member.member_id],)
        return (
            party_priority[member.party],
            *priority_key(
                meets_requirement(member_stats),
                member_stats.attend_hours,
                member_stats.duty_count,
                rank_of[member.member_id],
            ),
        )

    eligible_sorted = sorted(eligible, key=sort_key)
    chosen = eligible_sorted[: event.required_members]
    chosen_ids = [member.member_id for member in chosen]
    chosen_index = {member_id: index + 1 for index, member_id in enumerate(chosen_ids)}
    eligible_index = {
        member.member_id: index + 1 for index, member in enumerate(eligible_sorted)
    }

    rows: list[DecisionRow] = []
    for member, applied, clash in classified:
        member_stats = stats.get(member.member_id, HourStats())
        is_selected = member.member_id in chosen_index
        is_eligible = applied and not clash
        rows.append(
            DecisionRow(
                member_id=member.member_id,
                party=member.party,
                applied=applied,
                eligible=is_eligible,
                selected=is_selected,
                pick_index=chosen_index.get(member.member_id),
                assign_rank=eligible_index.get(member.member_id),
                outcome=_outcome(
                    method,
                    is_selected,
                    chosen_index.get(member.member_id),
                    applied,
                    clash,
                    eligible_index.get(member.member_id),
                ),
                clash_with=clash,
                attend_hours=member_stats.attend_hours,
                training_hours=member_stats.training_hours,
                duty_hours=member_stats.duty_hours,
                training_offered=member_stats.training_offered,
                meets_total=meets_total(member_stats),
                meets_training=meets_training(member_stats),
                meets_requirement=meets_requirement(member_stats),
                duty_count=member_stats.duty_count,
                standard_rank=rank_of[member.member_id],
                party_priority=party_priority[member.party],
                queue_position=queue_position[member.member_id],
            )
        )

    by_id = {row.member_id: row for row in rows}
    eligible_rows = [by_id[member.member_id] for member in eligible_sorted]
    _write_reasons(method, rows, eligible_rows, event.required_members, chosen_ids)

    if method == "standard":
        rows.sort(key=lambda row: row.standard_rank)
    else:
        rows.sort(key=_assign_display_key)

    queues_after, party_after = apply_rotation(queues, party_order, chosen_ids)
    if chosen_ids:
        rolling_note = (
            f"Assigned members ({', '.join(chosen_ids)}) move to the back of their own "
            f"duty party queue, keeping the order they already had in that queue. "
            f"The next duty's first priority party is {party_after[0]}."
        )
    else:
        rolling_note = (
            "Nobody was assigned, so the member queues stay where they are. "
            f"This duty still uses a party turn. The next duty's first priority party is {party_after[0]}."
        )

    shortfall = max(0, event.required_members - len(chosen_ids))
    summary = _summary(method, event, chosen_ids, party_order, shortfall)
    return AssignmentPlan(
        method=method,
        duty_code=event.duty_code,
        required_members=event.required_members,
        selected=chosen_ids,
        shortfall=shortfall,
        party_order_before=list(party_order),
        queues_before={party: list(queue) for party, queue in queues.items()},
        party_order_after=party_after,
        queues_after=queues_after,
        summary=summary,
        rolling_note=rolling_note,
        generated_at=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        rows=rows,
    )


def _outcome(method, selected, pick_index, applied, clash, assign_rank) -> str:
    if selected:
        return f"Selected #{pick_index}"
    if not applied:
        return "Did not apply"
    if clash:
        return "Overlaps " + ", ".join(clash)
    if method == "assign" and assign_rank:
        return f"Not selected (priority {assign_rank})"
    return "Not selected"


def _assign_display_key(row: DecisionRow) -> tuple:
    if row.selected:
        return (0, row.pick_index or 0, row.standard_rank)
    if row.eligible:
        return (1, row.assign_rank or 0, row.standard_rank)
    if row.clash_with:
        return (2, row.standard_rank, 0)
    return (3, row.standard_rank, 0)


def _summary(method, event: Event, selected: list[str], party_order: list[str], shortfall: int) -> str:
    names = ", ".join(selected) if selected else "nobody"
    gap = ""
    if shortfall:
        gap = f" Short by {shortfall}: not enough members who had applied and were free."
    turn = " then ".join(party_order)
    filled = f"Filled {len(selected)} of {event.required_members}."
    if method == "standard":
        return (
            f"Standard assignment for {event.duty_code} selected {names}. "
            f"Duty party priority was {turn}. {filled}{gap}"
        )
    return (
        f"Assign for {event.duty_code} selected {names}. "
        f"This duty's party turn starts at {party_order[0]} ({turn}). "
        f"Inside a party: open annual hours (fewest hours first), then fewer duties, "
        f"then the member queue. {filled}{gap}"
    )


def _write_reasons(method, rows, eligible_rows, required, selected_ids) -> None:
    for row in rows:
        if method == "standard":
            row.reason = _standard_reason(row, required, selected_ids)
        else:
            row.reason = _assign_reason(row, eligible_rows, required)


def _points(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def _standard_reason(row: DecisionRow, required: int, selected_ids: list[str]) -> str:
    if not row.applied:
        return "Has not applied"
    if row.clash_with:
        return f"Has an overlapping duty: {', '.join(row.clash_with)}"
    if row.selected:
        return _points(
            f"Selected, pick {row.pick_index} of {required}",
            f"{row.party} priority {row.party_priority}, queue {row.queue_position}",
            "Hours were not used",
        )
    filled = ", ".join(selected_ids) if selected_ids else "others"
    return _points(
        "Applied and free",
        f"Rank {row.standard_rank}, places filled by {filled}",
    )


def _assign_reason(row: DecisionRow, eligible_rows: list[DecisionRow], required: int) -> str:
    if not row.applied:
        return "Has not applied"
    if row.clash_with:
        return f"Has an overlapping duty: {', '.join(row.clash_with)}"
    points = [
        f"Selected, pick {row.pick_index} of {required}" if row.selected else f"Not selected, only {required} places",
        f"{row.party} party priority {row.party_priority}",
        _requirement_point(row),
    ]
    if not row.eligible:
        return _points(*points)
    index = next(i for i, item in enumerate(eligible_rows) if item.member_id == row.member_id)
    if row.selected and index + 1 < len(eligible_rows):
        points.append(_compare(row, eligible_rows[index + 1], row))
    if not row.selected and index > 0:
        points.append(_compare(eligible_rows[index - 1], row, row))
    if row.selected:
        overtaken = [
            other
            for other in eligible_rows
            if other.standard_rank < row.standard_rank and not other.selected
        ]
        if overtaken:
            earliest = min(overtaken, key=lambda other: other.standard_rank)
            next_id = eligible_rows[index + 1].member_id if index + 1 < len(eligible_rows) else None
            if earliest.member_id != next_id:
                points.append(_compare(row, earliest, row))
    return _points(*points)


def _requirement_point(row: DecisionRow) -> str:
    hours = f"{fmt_hours(row.attend_hours)}h of {fmt_hours(HOUR_TARGET)}h"
    if row.meets_requirement:
        return f"Annual requirement met ({hours})"
    return f"Annual requirement open ({hours})"


def _compare(better: DecisionRow, worse: DecisionRow, viewpoint: DecisionRow) -> str:
    place = (
        f"Ahead of {worse.member_id}"
        if viewpoint.member_id == better.member_id
        else f"Behind {better.member_id}"
    )
    if better.party_priority != worse.party_priority:
        return f"{place}: party turn reaches {better.party} before {worse.party}"
    better_key = priority_key(
        better.meets_requirement, better.attend_hours, better.duty_count, better.standard_rank
    )
    worse_key = priority_key(
        worse.meets_requirement, worse.attend_hours, worse.duty_count, worse.standard_rank
    )
    if better_key[0] != worse_key[0]:
        return (
            f"{place}: annual requirement open, "
            f"{worse.member_id} has met it ({fmt_hours(worse.attend_hours)}h)"
        )
    if better_key[1] != worse_key[1]:
        return (
            f"{place}: fewer hours "
            f"({fmt_hours(better.attend_hours)}h vs {fmt_hours(worse.attend_hours)}h)"
        )
    if better_key[2] != worse_key[2]:
        return (
            f"{place}: duties stay even "
            f"({better.duty_count} vs {worse.duty_count})"
        )
    if better_key[3] != worse_key[3]:
        return (
            f"{place}: rolling rank {better.standard_rank} "
            f"before rank {worse.standard_rank}"
        )
    return f"{place} on the same rules"
