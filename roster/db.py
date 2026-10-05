"""SQLite storage for the roster."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from roster.errors import NotFound
from roster.models import PARTIES, Event, Member, Training, format_dt, parse_dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    member_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    party TEXT NOT NULL CHECK (party IN ('DP1', 'DP2', 'DP3')),
    queue_order INTEGER NOT NULL CHECK (queue_order >= 1)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    duty_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL,
    required_members INTEGER NOT NULL CHECK (required_members >= 1),
    assignment_method TEXT CHECK (
        assignment_method IN ('standard', 'assign') OR assignment_method IS NULL
    ),
    assignment_log TEXT,
    assigned_at TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, member_id)
);

CREATE TABLE IF NOT EXISTS assignments (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    pick_index INTEGER NOT NULL,
    PRIMARY KEY (event_id, member_id)
);

CREATE TABLE IF NOT EXISTS trainings (
    id INTEGER PRIMARY KEY,
    training_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    start_dt TEXT NOT NULL,
    end_dt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_attendance (
    training_id INTEGER NOT NULL REFERENCES trainings(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    PRIMARY KEY (training_id, member_id)
);
"""


@dataclass
class Snapshot:
    members: list[Member] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    applications: dict[int, set[str]] = field(default_factory=dict)
    assignments: dict[int, list[str]] = field(default_factory=dict)
    trainings: list[Training] = field(default_factory=list)
    attendance: dict[int, set[str]] = field(default_factory=dict)

    def event(self, event_id: int) -> Event | None:
        return next((item for item in self.events if item.id == event_id), None)

    def member(self, member_id: int) -> Member | None:
        return next((item for item in self.members if item.id == member_id), None)

    def training(self, training_id: int) -> Training | None:
        return next((item for item in self.trainings if item.id == training_id), None)


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def session(self):
        with self._lock:
            connection = sqlite3.connect(self.path)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def migrate(self) -> None:
        with self.session() as connection:
            connection.executescript(SCHEMA)

    def load(self) -> Snapshot:
        with self.session() as connection:
            members = [
                Member(row["id"], row["member_code"], row["party"], row["queue_order"])
                for row in connection.execute(
                    "SELECT id, member_code, party, queue_order FROM members ORDER BY party, queue_order, member_code"
                )
            ]
            events = []
            for row in connection.execute(
                """
                SELECT id, duty_code, start_dt, end_dt, required_members,
                       assignment_method, assignment_log, assigned_at
                FROM events
                ORDER BY start_dt, id
                """
            ):
                log = json.loads(row["assignment_log"]) if row["assignment_log"] else None
                events.append(
                    Event(
                        row["id"],
                        row["duty_code"],
                        parse_dt(row["start_dt"], "Duty start"),
                        parse_dt(row["end_dt"], "Duty end"),
                        row["required_members"],
                        row["assignment_method"],
                        log,
                        row["assigned_at"],
                    )
                )
            applications: dict[int, set[str]] = {}
            for row in connection.execute(
                """
                SELECT a.event_id, m.member_code
                FROM applications a
                JOIN members m ON m.id = a.member_id
                """
            ):
                applications.setdefault(row["event_id"], set()).add(row["member_code"])
            assignments: dict[int, list[str]] = {}
            for row in connection.execute(
                """
                SELECT s.event_id, m.member_code
                FROM assignments s
                JOIN members m ON m.id = s.member_id
                ORDER BY s.event_id, s.pick_index
                """
            ):
                assignments.setdefault(row["event_id"], []).append(row["member_code"])
            trainings = [
                Training(
                    row["id"],
                    row["training_code"],
                    parse_dt(row["start_dt"], "Training start"),
                    parse_dt(row["end_dt"], "Training end"),
                )
                for row in connection.execute(
                    """
                    SELECT id, training_code, start_dt, end_dt
                    FROM trainings
                    ORDER BY start_dt, id
                    """
                )
            ]
            attendance: dict[int, set[str]] = {}
            for row in connection.execute(
                """
                SELECT t.training_id, m.member_code
                FROM training_attendance t
                JOIN members m ON m.id = t.member_id
                """
            ):
                attendance.setdefault(row["training_id"], set()).add(row["member_code"])
        return Snapshot(members, events, applications, assignments, trainings, attendance)

    def insert_member(self, member_code: str, party: str) -> int:
        if party not in PARTIES:
            raise ValueError("Duty party must be DP1, DP2, or DP3")
        with self.session() as connection:
            order = connection.execute(
                "SELECT COALESCE(MAX(queue_order), 0) + 1 AS next_order FROM members WHERE party = ?",
                (party,),
            ).fetchone()["next_order"]
            try:
                cursor = connection.execute(
                    "INSERT INTO members (member_code, party, queue_order) VALUES (?, ?, ?)",
                    (member_code, party, order),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Member ID already exists") from exc
            return int(cursor.lastrowid)

    def update_member(
        self,
        member_id: int,
        *,
        member_code: str | None = None,
        party: str | None = None,
        queue_order: int | None = None,
    ) -> None:
        with self.session() as connection:
            row = connection.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            if row is None:
                raise NotFound("Member not found")
            new_code = row["member_code"] if member_code is None else member_code
            new_party = row["party"] if party is None else party
            if new_party not in PARTIES:
                raise ValueError("Duty party must be DP1, DP2, or DP3")
            try:
                connection.execute(
                    "UPDATE members SET member_code = ?, party = ? WHERE id = ?",
                    (new_code, new_party, member_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Member ID already exists") from exc
            if new_party != row["party"]:
                _renumber_party(connection, row["party"])
                _place_member(connection, member_id, new_party, queue_order)
            elif queue_order is not None:
                _place_member(connection, member_id, new_party, queue_order)

    def move_member(self, member_id: int, direction: str) -> None:
        if direction not in ("up", "down"):
            raise ValueError("Direction must be up or down")
        with self.session() as connection:
            row = connection.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            if row is None:
                raise NotFound("Member not found")
            delta = -1 if direction == "up" else 1
            _place_member(connection, member_id, row["party"], row["queue_order"] + delta)

    def delete_member(self, member_id: int) -> None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT party FROM members WHERE id = ?", (member_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Member not found")
            cursor = connection.execute("DELETE FROM members WHERE id = ?", (member_id,))
            if cursor.rowcount == 0:
                raise NotFound("Member not found")
            _renumber_party(connection, row["party"])

    def insert_event(
        self,
        duty_code: str,
        start: datetime,
        end: datetime,
        required_members: int,
    ) -> int:
        with self.session() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO events (duty_code, start_dt, end_dt, required_members)
                    VALUES (?, ?, ?, ?)
                    """,
                    (duty_code, format_dt(start), format_dt(end), required_members),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Duty code already exists") from exc
            return int(cursor.lastrowid)

    def update_event(
        self,
        event_id: int,
        *,
        duty_code: str,
        start: datetime,
        end: datetime,
        required_members: int,
    ) -> None:
        with self.session() as connection:
            if connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise NotFound("Duty not found")
            try:
                connection.execute(
                    """
                    UPDATE events
                    SET duty_code = ?, start_dt = ?, end_dt = ?, required_members = ?
                    WHERE id = ?
                    """,
                    (duty_code, format_dt(start), format_dt(end), required_members, event_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Duty code already exists") from exc

    def delete_event(self, event_id: int) -> None:
        with self.session() as connection:
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            if cursor.rowcount == 0:
                raise NotFound("Duty not found")

    def set_applications(self, event_id: int, member_codes: list[str]) -> None:
        with self.session() as connection:
            _require_event(connection, event_id)
            connection.execute("DELETE FROM applications WHERE event_id = ?", (event_id,))
            for code in dict.fromkeys(member_codes):
                member_id = _member_db_id(connection, code)
                connection.execute(
                    "INSERT INTO applications (event_id, member_id) VALUES (?, ?)",
                    (event_id, member_id),
                )

    def set_assignment(
        self,
        event_id: int,
        member_codes: list[str],
        method: str,
        log_json: str,
        assigned_at: str,
    ) -> None:
        with self.session() as connection:
            _require_event(connection, event_id)
            connection.execute("DELETE FROM assignments WHERE event_id = ?", (event_id,))
            for index, code in enumerate(member_codes, start=1):
                connection.execute(
                    """
                    INSERT INTO assignments (event_id, member_id, pick_index)
                    VALUES (?, ?, ?)
                    """,
                    (event_id, _member_db_id(connection, code), index),
                )
            connection.execute(
                """
                UPDATE events
                SET assignment_method = ?, assignment_log = ?, assigned_at = ?
                WHERE id = ?
                """,
                (method, log_json, assigned_at, event_id),
            )

    def clear_assignment(self, event_id: int) -> None:
        with self.session() as connection:
            _require_event(connection, event_id)
            connection.execute("DELETE FROM assignments WHERE event_id = ?", (event_id,))
            connection.execute(
                """
                UPDATE events
                SET assignment_method = NULL, assignment_log = NULL, assigned_at = NULL
                WHERE id = ?
                """,
                (event_id,),
            )

    def insert_training(self, training_code: str, start: datetime, end: datetime) -> int:
        with self.session() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO trainings (training_code, start_dt, end_dt) VALUES (?, ?, ?)",
                    (training_code, format_dt(start), format_dt(end)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Training code already exists") from exc
            return int(cursor.lastrowid)

    def update_training(
        self,
        training_id: int,
        *,
        training_code: str,
        start: datetime,
        end: datetime,
    ) -> None:
        with self.session() as connection:
            if connection.execute(
                "SELECT 1 FROM trainings WHERE id = ?", (training_id,)
            ).fetchone() is None:
                raise NotFound("Training not found")
            try:
                connection.execute(
                    """
                    UPDATE trainings
                    SET training_code = ?, start_dt = ?, end_dt = ?
                    WHERE id = ?
                    """,
                    (training_code, format_dt(start), format_dt(end), training_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Training code already exists") from exc

    def delete_training(self, training_id: int) -> None:
        with self.session() as connection:
            cursor = connection.execute("DELETE FROM trainings WHERE id = ?", (training_id,))
            if cursor.rowcount == 0:
                raise NotFound("Training not found")

    def set_attendees(self, training_id: int, member_codes: list[str]) -> None:
        with self.session() as connection:
            if connection.execute(
                "SELECT 1 FROM trainings WHERE id = ?", (training_id,)
            ).fetchone() is None:
                raise NotFound("Training not found")
            connection.execute(
                "DELETE FROM training_attendance WHERE training_id = ?", (training_id,)
            )
            for code in dict.fromkeys(member_codes):
                connection.execute(
                    "INSERT INTO training_attendance (training_id, member_id) VALUES (?, ?)",
                    (training_id, _member_db_id(connection, code)),
                )

    def clear_all(self) -> None:
        with self.session() as connection:
            for table in (
                "training_attendance",
                "assignments",
                "applications",
                "trainings",
                "events",
                "members",
            ):
                connection.execute(f"DELETE FROM {table}")


def _require_event(connection, event_id: int) -> None:
    if connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
        raise NotFound("Duty not found")


def _member_db_id(connection, member_code: str) -> int:
    row = connection.execute(
        "SELECT id FROM members WHERE member_code = ? COLLATE NOCASE",
        (member_code,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Member {member_code} was not found")
    return int(row["id"])


def _party_ids(connection, party: str) -> list[int]:
    rows = connection.execute(
        """
        SELECT id FROM members
        WHERE party = ?
        ORDER BY queue_order, member_code
        """,
        (party,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _renumber(connection, ids: list[int]) -> None:
    for index, member_id in enumerate(ids, start=1):
        connection.execute(
            "UPDATE members SET queue_order = ? WHERE id = ?",
            (index, member_id),
        )


def _renumber_party(connection, party: str) -> None:
    _renumber(connection, _party_ids(connection, party))


def _place_member(connection, member_id: int, party: str, queue_order: int | None) -> None:
    ids = [item for item in _party_ids(connection, party) if item != member_id]
    if queue_order is None:
        ids.append(member_id)
    else:
        index = max(0, min(int(queue_order) - 1, len(ids)))
        ids.insert(index, member_id)
    _renumber(connection, ids)
