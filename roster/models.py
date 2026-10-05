"""Domain records for members, duties, and training."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

PARTIES = ("DP1", "DP2", "DP3")
HOUR_TARGET = 60.0
TRAINING_RATIO = 0.30
CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


def hours_between(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 3600.0, 4)


def format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


def parse_dt(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a date and time, for example 2026-10-06T09:00")
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"{label} must be a date and time, for example 2026-10-06T09:00")


def parse_code(value: object, label: str) -> str:
    if not isinstance(value, str) or not CODE_RE.fullmatch(value.strip()):
        raise ValueError(
            f"{label} must be 1–32 letters, numbers, hyphens, or underscores"
        )
    return value.strip()


@dataclass(frozen=True)
class FinancialYear:
    """Financial year running from 1 April to 31 March."""

    year: int

    @classmethod
    def of(cls, moment: datetime) -> FinancialYear:
        start_year = moment.year if moment.month >= 4 else moment.year - 1
        return cls(start_year)

    @classmethod
    def current(cls) -> FinancialYear:
        return cls.of(datetime.now())

    @property
    def label(self) -> str:
        return f"{self.year}/{(self.year + 1) % 100:02d}"

    @property
    def start(self) -> datetime:
        return datetime(self.year, 4, 1)

    @property
    def end_exclusive(self) -> datetime:
        return datetime(self.year + 1, 4, 1)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end_exclusive

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "label": self.label,
            "start": self.start.strftime("%Y-%m-%d"),
            "end": f"{self.year + 1}-03-31",
        }


class Member:
    """A person on the roster.

    ``attend_hours`` is filled for a financial year from training attendance
    plus assigned duty. It is not stored on its own.
    """

    def __init__(self, id: int | None, member_id: str, party: str, queue_order: int):
        self.id = id
        self.member_id = member_id
        self.party = party
        self.queue_order = queue_order
        self.attend_hours: float | None = None

    def validate(self) -> None:
        self.member_id = parse_code(self.member_id, "Member ID")
        if self.party not in PARTIES:
            raise ValueError("Duty party must be DP1, DP2, or DP3")
        if isinstance(self.queue_order, bool) or not isinstance(self.queue_order, int):
            raise ValueError("Queue order must be a whole number")
        if self.queue_order < 1:
            raise ValueError("Queue order must be at least 1")

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "member_id": self.member_id,
            "party": self.party,
            "queue_order": self.queue_order,
            "attend_hours": self.attend_hours,
        }
        return payload


class Event:
    """A duty that needs a set number of members between two times.

    Attributes required by the roster:
    duty code, duty start, duty end, and required number of members.
    """

    def __init__(
        self,
        id: int | None,
        duty_code: str,
        start_datetime: datetime,
        end_datetime: datetime,
        required_members: int,
        assignment_method: str | None = None,
        assignment_log: dict | None = None,
        assigned_at: str | None = None,
    ):
        self.id = id
        self.duty_code = duty_code
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime
        self.required_members = required_members
        self.assignment_method = assignment_method
        self.assignment_log = assignment_log
        self.assigned_at = assigned_at

    @property
    def hours(self) -> float:
        return hours_between(self.start_datetime, self.end_datetime)

    def overlaps(self, other: Event) -> bool:
        return (
            self.start_datetime < other.end_datetime
            and other.start_datetime < self.end_datetime
        )

    def validate(self) -> None:
        self.duty_code = parse_code(self.duty_code, "Duty code")
        if not isinstance(self.start_datetime, datetime) or not isinstance(
            self.end_datetime, datetime
        ):
            raise ValueError("Duty start and duty end must be dates and times")
        if self.end_datetime <= self.start_datetime:
            raise ValueError("Duty end must be after duty start")
        if self.end_datetime - self.start_datetime > timedelta(days=14):
            raise ValueError("A duty cannot be longer than 14 days")
        if isinstance(self.required_members, bool) or not isinstance(
            self.required_members, int
        ):
            raise ValueError("Required number of members must be a whole number")
        if self.required_members < 1:
            raise ValueError("Required number of members must be at least 1")
        if self.required_members > 500:
            raise ValueError("Required number of members cannot exceed 500")
        if self.assignment_method not in (None, "standard", "assign"):
            raise ValueError("Assignment method must be standard or assign")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "duty_code": self.duty_code,
            "start_datetime": format_dt(self.start_datetime),
            "end_datetime": format_dt(self.end_datetime),
            "required_members": self.required_members,
            "hours": self.hours,
            "assignment_method": self.assignment_method,
            "assigned_at": self.assigned_at,
        }


class Training:
    """A training session. Attendance counts toward the annual hours rules."""

    def __init__(
        self,
        id: int | None,
        training_code: str,
        start_datetime: datetime,
        end_datetime: datetime,
    ):
        self.id = id
        self.training_code = training_code
        self.start_datetime = start_datetime
        self.end_datetime = end_datetime

    @property
    def hours(self) -> float:
        return hours_between(self.start_datetime, self.end_datetime)

    def validate(self) -> None:
        self.training_code = parse_code(self.training_code, "Training code")
        if self.end_datetime <= self.start_datetime:
            raise ValueError("Training end must be after training start")
        if self.end_datetime - self.start_datetime > timedelta(days=14):
            raise ValueError("A training session cannot be longer than 14 days")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "training_code": self.training_code,
            "start_datetime": format_dt(self.start_datetime),
            "end_datetime": format_dt(self.end_datetime),
            "hours": self.hours,
        }
