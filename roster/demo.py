"""Sample roster used by the worked example.

DP1 is P1–P4, DP2 is P5–P8, DP3 is P9–P12.
With everyone applied and no duty saved yet:
Standard on E1–E4 selects (P1, P2), (P5), (P9, P10, P11), (P3).
Assign on E1 selects P3, P2 because P3 has no attend hours and P1 has already
met the annual requirement.
"""

from __future__ import annotations

from roster.db import Database
from roster.models import format_dt, parse_dt

STANDARD_SEQUENCE = {
    "E1": ["P1", "P2"],
    "E2": ["P5"],
    "E3": ["P9", "P10", "P11"],
    "E4": ["P3"],
}
ASSIGN_E1 = ["P3", "P2"]


def restore(db: Database) -> None:
    members = []
    for number in range(1, 13):
        members.append(
            {
                "member_id": f"P{number}",
                "party": f"DP{(number - 1) // 4 + 1}",
                "queue_order": (number - 1) % 4 + 1,
            }
        )
    events = [
        ("E1", "2026-10-06T09:00", "2026-10-06T13:00", 2),
        ("E2", "2026-10-07T09:00", "2026-10-07T12:00", 1),
        ("E3", "2026-10-08T09:00", "2026-10-08T17:00", 3),
        ("E4", "2026-10-09T09:00", "2026-10-09T11:00", 1),
    ]
    trainings = [
        ("TR-INDUCT", "2026-04-15T09:00", "2026-04-15T17:00"),
        ("TR-CAMP", "2026-05-01T09:00", "2026-05-03T21:00"),
    ]
    induction = [f"P{number}" for number in range(1, 13) if number != 3]
    camp = ["P1"]

    with db.session() as connection:
        for table in (
            "training_attendance",
            "assignments",
            "applications",
            "trainings",
            "events",
            "members",
        ):
            connection.execute(f"DELETE FROM {table}")
        member_ids = {}
        for member in members:
            cursor = connection.execute(
                "INSERT INTO members (member_code, party, queue_order) VALUES (?, ?, ?)",
                (member["member_id"], member["party"], member["queue_order"]),
            )
            member_ids[member["member_id"]] = int(cursor.lastrowid)
        event_ids = []
        for duty_code, start, end, required in events:
            start_dt = parse_dt(start, "Duty start")
            end_dt = parse_dt(end, "Duty end")
            cursor = connection.execute(
                """
                INSERT INTO events (duty_code, start_dt, end_dt, required_members)
                VALUES (?, ?, ?, ?)
                """,
                (duty_code, format_dt(start_dt), format_dt(end_dt), required),
            )
            event_ids.append(int(cursor.lastrowid))
        for event_id in event_ids:
            for member_id in member_ids.values():
                connection.execute(
                    "INSERT INTO applications (event_id, member_id) VALUES (?, ?)",
                    (event_id, member_id),
                )
        training_ids = {}
        for code, start, end in trainings:
            start_dt = parse_dt(start, "Training start")
            end_dt = parse_dt(end, "Training end")
            cursor = connection.execute(
                "INSERT INTO trainings (training_code, start_dt, end_dt) VALUES (?, ?, ?)",
                (code, format_dt(start_dt), format_dt(end_dt)),
            )
            training_ids[code] = int(cursor.lastrowid)
        for code in induction:
            connection.execute(
                "INSERT INTO training_attendance (training_id, member_id) VALUES (?, ?)",
                (training_ids["TR-INDUCT"], member_ids[code]),
            )
        for code in camp:
            connection.execute(
                "INSERT INTO training_attendance (training_id, member_id) VALUES (?, ?)",
                (training_ids["TR-CAMP"], member_ids[code]),
            )
