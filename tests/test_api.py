"""Grid CRUD and the saved assignment flow."""

from app import create_app
from roster.demo import ASSIGN_E1, STANDARD_SEQUENCE


def client_for(tmp_path, seed=False):
    application = create_app(str(tmp_path / "roster.db"), seed=seed)
    application.testing = True
    return application.test_client()


def test_member_and_event_crud(tmp_path):
    client = client_for(tmp_path)
    created = client.post("/api/members", json={"member_id": "P1", "party": "DP1"})
    assert created.status_code == 201
    member = created.get_json()
    assert member["member_id"] == "P1"
    assert member["party"] == "DP1"
    assert member["attend_hours"] == 0

    second = client.post("/api/members", json={"member_id": "P2", "party": "DP1"})
    assert second.status_code == 201
    moved = client.post(f"/api/members/{second.get_json()['id']}/move", json={"direction": "up"})
    listing = client.get("/api/members").get_json()["members"]
    assert [item["member_id"] for item in listing] == ["P2", "P1"]
    assert moved.get_json()["queue_order"] == 1

    party = client.put(f"/api/members/{member['id']}", json={"party": "DP2"})
    assert party.get_json()["party"] == "DP2"

    duplicate = client.post("/api/members", json={"member_id": "P2", "party": "DP3"})
    assert duplicate.status_code == 400

    event = client.post(
        "/api/events",
        json={
            "duty_code": "E1",
            "start_datetime": "2026-10-06T09:00",
            "end_datetime": "2026-10-06T13:00",
            "required_members": 2,
        },
    )
    assert event.status_code == 201
    body = event.get_json()
    assert body["hours"] == 4
    assert body["required_members"] == 2
    updated = client.put(f"/api/events/{body['id']}", json={"required_members": 3})
    assert updated.get_json()["required_members"] == 3
    invalid = client.put(
        f"/api/events/{body['id']}",
        json={"end_datetime": "2026-10-06T08:00"},
    )
    assert invalid.status_code == 400
    assert client.delete(f"/api/events/{body['id']}").status_code == 200
    assert client.get("/api/events").get_json()["events"] == []


def test_standard_sequence_and_assign_priority(tmp_path):
    client = client_for(tmp_path)
    assert client.post("/api/demo/restore").status_code == 200
    hours = client.get("/api/reports/hours?fy=2026").get_json()
    by_member = {row["member_id"]: row for row in hours["rows"]}
    assert by_member["P3"]["training_hours"] == 0
    assert by_member["P3"]["meets_requirement"] is False
    assert by_member["P1"]["meets_requirement"] is True
    assert by_member["P1"]["training_hours"] == hours["training_offered"]
    assert by_member["P2"]["training_hours"] > 0

    events = {event["duty_code"]: event for event in client.get("/api/events").get_json()["events"]}
    first = client.post(f"/api/events/{events['E1']['id']}/assignment", json={"method": "assign"})
    assert first.status_code == 200
    assert first.get_json()["plan"]["selected"] == ASSIGN_E1
    client.delete(f"/api/events/{events['E1']['id']}/assignment")

    for code, names in STANDARD_SEQUENCE.items():
        result = client.post(
            f"/api/events/{events[code]['id']}/assignment",
            json={"method": "standard"},
        )
        assert result.status_code == 200
        assert result.get_json()["plan"]["selected"] == names

    report = client.get("/api/reports/hours?fy=2026").get_json()
    after = {row["member_id"]: row for row in report["rows"]}
    assert after["P1"]["duty_hours"] == 4
    assert after["P5"]["duty_hours"] == 3
    assert after["P9"]["duty_hours"] == 8
    assert after["P3"]["duty_hours"] == 2
    assert after["P4"]["duty_hours"] == 0

    saved = client.get(f"/api/events/{events['E4']['id']}/workspace").get_json()["saved"]
    assert saved["party_order_before"] == ["DP1", "DP2", "DP3"]
    assert saved["queues_before"]["DP1"] == ["P3", "P4", "P1", "P2"]

    selection = client.get("/api/reports/selection").get_json()["events"]
    decided = [item for item in selection if item["event"]["duty_code"] == "E1"][0]
    assert decided["decided"] is True
    assert "Standard assignment" in decided["log"]["summary"]
    assert decided["stale"]["stale"] is False


def test_application_checkbox_blocks_assignment(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/demo/restore")
    events = {event["duty_code"]: event for event in client.get("/api/events").get_json()["events"]}
    members = [row["member_id"] for row in client.get("/api/members").get_json()["members"]]
    applied = [member_id for member_id in members if member_id != "P1"]
    saved = client.put(
        f"/api/events/{events['E1']['id']}/applications",
        json={"member_ids": applied},
    )
    assert saved.status_code == 200
    result = client.post(
        f"/api/events/{events['E1']['id']}/assignment",
        json={"method": "standard"},
    )
    assert result.get_json()["plan"]["selected"] == ["P2", "P3"]


def test_rerun_later_duties_in_date_order(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/demo/restore")
    events = {event["duty_code"]: event for event in client.get("/api/events").get_json()["events"]}
    early = client.post(f"/api/events/{events['E4']['id']}/assignment", json={"method": "standard"})
    assert early.get_json()["plan"]["selected"] == ["P1"]
    client.post(f"/api/events/{events['E1']['id']}/assignment", json={"method": "standard"})
    rerun = client.post(f"/api/events/{events['E1']['id']}/rerun")
    assert rerun.status_code == 200
    results = {item["duty_code"]: item["selected"] for item in rerun.get_json()["results"]}
    assert results["E1"] == ["P1", "P2"]
    assert results["E4"] == ["P5"]


def test_hours_follow_the_financial_year_boundary(tmp_path):
    client = client_for(tmp_path)
    member = client.post("/api/members", json={"member_id": "P1", "party": "DP1"}).get_json()
    march = client.post(
        "/api/events",
        json={
            "duty_code": "MAR",
            "start_datetime": "2026-03-31T10:00",
            "end_datetime": "2026-03-31T12:00",
            "required_members": 1,
        },
    ).get_json()
    april = client.post(
        "/api/events",
        json={
            "duty_code": "APR",
            "start_datetime": "2026-04-01T10:00",
            "end_datetime": "2026-04-01T14:00",
            "required_members": 1,
        },
    ).get_json()
    for event in (march, april):
        client.put(f"/api/events/{event['id']}/applications", json={"member_ids": ["P1"]})
        assigned = client.post(f"/api/events/{event['id']}/assignment", json={"method": "standard"})
        assert assigned.get_json()["plan"]["selected"] == ["P1"]
    march_training = client.post(
        "/api/trainings",
        json={
            "training_code": "T-MAR",
            "start_datetime": "2026-03-31T09:00",
            "end_datetime": "2026-03-31T12:00",
        },
    ).get_json()
    april_training = client.post(
        "/api/trainings",
        json={
            "training_code": "T-APR",
            "start_datetime": "2026-04-01T09:00",
            "end_datetime": "2026-04-01T11:00",
        },
    ).get_json()
    client.put(f"/api/trainings/{march_training['id']}/attendees", json={"member_ids": ["P1"]})
    client.put(f"/api/trainings/{april_training['id']}/attendees", json={"member_ids": [member["member_id"]]})
    fy2025 = {row["member_id"]: row for row in client.get("/api/reports/hours?fy=2025").get_json()["rows"]}
    fy2026 = {row["member_id"]: row for row in client.get("/api/reports/hours?fy=2026").get_json()["rows"]}
    assert fy2025["P1"]["duty_hours"] == 2
    assert fy2025["P1"]["training_hours"] == 3
    assert fy2025["P1"]["attend_hours"] == 5
    assert fy2026["P1"]["duty_hours"] == 4
    assert fy2026["P1"]["training_hours"] == 2
    assert fy2026["P1"]["attend_hours"] == 6


def test_edited_times_flag_an_overlap(tmp_path):
    client = client_for(tmp_path)
    client.post("/api/members", json={"member_id": "P1", "party": "DP1"})
    first = client.post(
        "/api/events",
        json={
            "duty_code": "A",
            "start_datetime": "2026-10-06T09:00",
            "end_datetime": "2026-10-06T12:00",
            "required_members": 1,
        },
    ).get_json()
    second = client.post(
        "/api/events",
        json={
            "duty_code": "B",
            "start_datetime": "2026-10-07T09:00",
            "end_datetime": "2026-10-07T12:00",
            "required_members": 1,
        },
    ).get_json()
    for event in (first, second):
        client.put(f"/api/events/{event['id']}/applications", json={"member_ids": ["P1"]})
        client.post(f"/api/events/{event['id']}/assignment", json={"method": "standard"})
    client.put(f"/api/events/{second['id']}", json={"start_datetime": "2026-10-06T10:00", "end_datetime": "2026-10-06T13:00"})
    flags = {event["duty_code"]: event["conflict"] for event in client.get("/api/events").get_json()["events"]}
    assert flags["A"] is True
    assert flags["B"] is True
