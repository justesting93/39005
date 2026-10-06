"""Assignment rules, including the 12-member rolling example."""

from datetime import datetime

from roster.engine import HourStats, plan_assignment
from roster.models import Event, FinancialYear, Member, parse_dt


def people(names, party="DP1"):
    return [Member(index, name, party, index) for index, name in enumerate(names, start=1)]


def duty(event_id, code, start, end, required):
    return Event(
        event_id,
        code,
        parse_dt(start, "Duty start"),
        parse_dt(end, "Duty end"),
        required,
    )


def roster():
    return [
        Member(number, f"P{number}", f"DP{(number - 1) // 4 + 1}", (number - 1) % 4 + 1)
        for number in range(1, 13)
    ]


def test_financial_year_runs_from_april():
    assert FinancialYear.of(datetime(2026, 10, 5)).year == 2026
    assert FinancialYear.of(datetime(2026, 4, 1, 0, 0)).year == 2026
    assert FinancialYear.of(datetime(2026, 3, 31, 23, 59)).year == 2025
    year = FinancialYear(2026)
    assert year.label == "2026/27"
    assert year.contains(datetime(2026, 4, 1, 0, 0))
    assert year.contains(datetime(2027, 3, 31, 23, 59))
    assert not year.contains(datetime(2027, 4, 1, 0, 0))


def test_event_hours_and_fields():
    event = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    assert event.duty_code == "E1"
    assert event.required_members == 2
    assert event.hours == 4


def test_standard_matches_the_rolling_example():
    members = roster()
    events = [
        duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2),
        duty(2, "E2", "2026-10-07T09:00", "2026-10-07T12:00", 1),
        duty(3, "E3", "2026-10-08T09:00", "2026-10-08T17:00", 3),
        duty(4, "E4", "2026-10-09T09:00", "2026-10-09T11:00", 1),
    ]
    expected = [["P1", "P2"], ["P5"], ["P9", "P10", "P11"], ["P3"]]
    applied = {member.member_id for member in members}
    earlier = []
    for event, names in zip(events, expected):
        plan = plan_assignment("standard", event, members, applied, earlier, {}, {})
        assert plan.selected == names
        assert "were not used" in plan.rows[0].reason
        earlier.append((event, plan.selected))
    assert earlier[0][1] == ["P1", "P2"]
    fourth = plan_assignment("standard", events[3], members, applied, earlier[:3], {}, {})
    assert fourth.queues_before["DP1"] == ["P3", "P4", "P1", "P2"]
    assert fourth.party_order_before == ["DP1", "DP2", "DP3"]
    assert fourth.party_order_after[0] == "DP2"


def test_skip_member_who_did_not_apply_and_keep_their_place():
    members = roster()
    event = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    applied = {member.member_id for member in members if member.member_id != "P1"}
    plan = plan_assignment("standard", event, members, applied, [], {}, {})
    assert plan.selected == ["P2", "P3"]
    assert plan.queues_after["DP1"] == ["P1", "P4", "P2", "P3"]
    skipped = next(row for row in plan.rows if row.member_id == "P1")
    assert "not applied" in skipped.reason.lower()


def test_overlap_skips_without_losing_queue_place():
    members = people(["A", "B", "C", "D"])
    event = duty(2, "E2", "2026-10-06T09:00", "2026-10-06T12:00", 1)
    plan = plan_assignment("standard", event, members, {"A", "B", "C", "D"}, [], {"A": ["E0"]}, {})
    assert plan.selected == ["B"]
    assert plan.queues_after["DP1"] == ["A", "C", "D", "B"]
    skipped = next(row for row in plan.rows if row.member_id == "A")
    assert "overlapping" in skipped.reason


def test_standard_fills_from_the_next_party():
    members = roster()
    event = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 5)
    applied = {member.member_id for member in members}
    plan = plan_assignment("standard", event, members, applied, [], {}, {})
    assert plan.selected == ["P1", "P2", "P3", "P4", "P5"]
    assert plan.queues_after["DP2"] == ["P6", "P7", "P8", "P5"]
    assert plan.party_order_after[0] == "DP2"


def test_nobody_applied_does_not_rotate():
    members = roster()
    event = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    plan = plan_assignment("standard", event, members, set(), [], {}, {})
    assert plan.selected == []
    assert plan.shortfall == 2
    assert plan.party_order_after == ["DP2", "DP3", "DP1"]
    assert plan.queues_after["DP1"] == ["P1", "P2", "P3", "P4"]


def test_assign_prefers_open_hours_then_fewer_duties_then_rolling():
    members = roster()
    event = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    offered = 68
    stats = {}
    for member in members:
        if member.member_id == "P1":
            stats[member.member_id] = HourStats(68, 0, offered, 0)
        elif member.member_id == "P3":
            stats[member.member_id] = HourStats(0, 0, offered, 0)
        else:
            stats[member.member_id] = HourStats(8, 0, offered, 0)
    applied = {member.member_id for member in members}
    plan = plan_assignment("assign", event, members, applied, [], {}, stats)
    assert plan.selected == ["P3", "P2"]
    reasons = {row.member_id: row.reason for row in plan.rows}
    assert "fewer hours" in reasons["P3"]
    assert "P1" in reasons["P3"]
    assert "annual requirement open" in reasons["P3"]
    assert "Annual requirement met" in reasons["P1"]
    assert "has met it" in reasons["P1"]
    assert "rank 2" in reasons["P2"]


def test_fewer_hours_outrank_fewer_duties():
    members = people(["A", "B"])
    event = duty(1, "E", "2026-10-06T09:00", "2026-10-06T10:00", 1)
    stats = {
        "A": HourStats(0, 0, 0, 5),
        "B": HourStats(0, 20, 0, 0),
    }
    plan = plan_assignment("assign", event, members, {"A", "B"}, [], {}, stats)
    assert plan.selected == ["A"]
    assert "fewer hours" in next(row.reason for row in plan.rows if row.member_id == "A")


def test_duty_count_breaks_an_hours_tie_ahead_of_rolling_order():
    members = people(["A", "B"])
    event = duty(1, "E", "2026-10-06T09:00", "2026-10-06T10:00", 1)
    stats = {
        "A": HourStats(0, 10, 0, 2),
        "B": HourStats(0, 10, 0, 0),
    }
    plan = plan_assignment("assign", event, members, {"A", "B"}, [], {}, stats)
    assert plan.selected == ["B"]
    assert "duties stay even" in next(row.reason for row in plan.rows if row.member_id == "B")


def test_met_requirement_ranks_after_an_open_requirement():
    members = people(["A", "B"])
    event = duty(1, "E", "2026-10-06T09:00", "2026-10-06T10:00", 1)
    stats = {
        "A": HourStats(0, 60, 0, 0),
        "B": HourStats(0, 10, 0, 3),
    }
    plan = plan_assignment("assign", event, members, {"A", "B"}, [], {}, stats)
    assert plan.selected == ["B"]
    assert "annual requirement open" in next(row.reason for row in plan.rows if row.member_id == "B")


def test_assign_still_requires_an_application():
    members = people(["A", "B"])
    event = duty(1, "E", "2026-10-06T09:00", "2026-10-06T10:00", 1)
    stats = {"A": HourStats(0, 0, 0, 0), "B": HourStats(0, 50, 0, 0)}
    plan = plan_assignment("assign", event, members, {"B"}, [], {}, stats)
    assert plan.selected == ["B"]
    assert "not applied" in next(row.reason for row in plan.rows if row.member_id == "A").lower()


def test_unassigned_earlier_duty_still_moves_the_party_turn():
    members = roster()
    first = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    second = duty(2, "E2", "2026-10-07T09:00", "2026-10-07T12:00", 1)
    applied = {member.member_id for member in members}
    plan = plan_assignment("standard", second, members, applied, [(first, [])], {}, {})
    assert plan.party_order_before[0] == "DP2"
    assert plan.selected == ["P5"]
    assert plan.queues_before["DP1"] == ["P1", "P2", "P3", "P4"]


def test_assign_fills_the_party_on_turn_before_fewer_hours_elsewhere():
    members = roster()
    first = duty(1, "E1", "2026-10-06T09:00", "2026-10-06T13:00", 2)
    second = duty(2, "E2", "2026-10-07T09:00", "2026-10-07T12:00", 1)
    stats = {member.member_id: HourStats(8, 0, 68, 0) for member in members}
    stats["P3"] = HourStats(0, 0, 68, 0)
    applied = {member.member_id for member in members}
    plan = plan_assignment("assign", second, members, applied, [(first, [])], {}, stats)
    assert plan.party_order_before == ["DP2", "DP3", "DP1"]
    assert plan.selected == ["P5"]
    reason = next(row.reason for row in plan.rows if row.member_id == "P5")
    assert "DP2" in reason
    assert "party priority 1" in reason
    skipped = next(row.reason for row in plan.rows if row.member_id == "P3")
    assert "party turn" in skipped
