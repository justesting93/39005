"""Duty Party Roster web app."""

from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from roster.db import Database
from roster.errors import NotFound
from roster.models import FinancialYear
from roster.service import (
    clear_assignment,
    commit_assignment,
    create_event,
    create_member,
    create_training,
    delete_event,
    delete_member,
    delete_training,
    export_roster,
    hours_report,
    import_events,
    import_members,
    import_roster,
    import_trainings,
    list_events,
    list_members,
    list_trainings,
    meta,
    move_member,
    rerun_forward,
    restore_demo,
    selection_report,
    set_applications,
    set_attendees,
    set_duty_attend_hours,
    set_training_attend_hours,
    update_event,
    update_member,
    update_training,
    workspace,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data", "roster.db")


def create_app(db_path: str = DEFAULT_DB, seed: bool = False) -> Flask:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fresh = not os.path.exists(db_path)
    database = Database(db_path)
    database.migrate()
    if seed and fresh:
        restore_demo(database)

    app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="/static")
    app.config["DB"] = database
    app.json.sort_keys = False

    @app.after_request
    def no_cache(response):
        if request.path == "/" or request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/api/meta")
    def get_meta():
        return jsonify(meta(database))

    @app.get("/api/members")
    def get_members():
        return jsonify(list_members(database, _fy_arg()))

    @app.post("/api/members")
    def post_member():
        return jsonify(create_member(database, _body(), _fy_arg())), 201

    @app.post("/api/members/import")
    def post_member_import():
        return jsonify(import_members(database, _body().get("rows"), _fy_arg()))

    @app.put("/api/members/<int:member_id>")
    def put_member(member_id: int):
        return jsonify(update_member(database, member_id, _body(), _fy_arg()))

    @app.delete("/api/members/<int:member_id>")
    def remove_member(member_id: int):
        delete_member(database, member_id)
        return jsonify(ok=True)

    @app.post("/api/members/<int:member_id>/move")
    def post_move(member_id: int):
        direction = _body().get("direction")
        return jsonify(move_member(database, member_id, direction, _fy_arg()))

    @app.get("/api/events")
    def get_events():
        return jsonify(list_events(database))

    @app.post("/api/events")
    def post_event():
        return jsonify(create_event(database, _body())), 201

    @app.post("/api/events/import")
    def post_event_import():
        return jsonify(import_events(database, _body().get("rows")))

    @app.put("/api/events/<int:event_id>")
    def put_event(event_id: int):
        return jsonify(update_event(database, event_id, _body()))

    @app.delete("/api/events/<int:event_id>")
    def remove_event(event_id: int):
        delete_event(database, event_id)
        return jsonify(ok=True)

    @app.get("/api/events/<int:event_id>/workspace")
    def get_workspace(event_id: int):
        return jsonify(workspace(database, event_id))

    @app.put("/api/events/<int:event_id>/applications")
    def put_applications(event_id: int):
        return jsonify(set_applications(database, event_id, _body().get("member_ids", [])))

    @app.put("/api/events/<int:event_id>/hours")
    def put_event_hours(event_id: int):
        body = _body()
        return jsonify(set_duty_attend_hours(database, event_id, body.get("member_id", ""), body.get("hours")))

    @app.post("/api/events/<int:event_id>/assignment")
    def post_assignment(event_id: int):
        return jsonify(commit_assignment(database, event_id, _body().get("method")))

    @app.delete("/api/events/<int:event_id>/assignment")
    def remove_assignment(event_id: int):
        return jsonify(clear_assignment(database, event_id))

    @app.post("/api/events/<int:event_id>/rerun")
    def post_rerun(event_id: int):
        return jsonify(rerun_forward(database, event_id))

    @app.get("/api/trainings")
    def get_trainings():
        return jsonify(list_trainings(database))

    @app.post("/api/trainings")
    def post_training():
        return jsonify(create_training(database, _body())), 201

    @app.post("/api/trainings/import")
    def post_training_import():
        return jsonify(import_trainings(database, _body().get("rows")))

    @app.put("/api/trainings/<int:training_id>")
    def put_training(training_id: int):
        return jsonify(update_training(database, training_id, _body()))

    @app.delete("/api/trainings/<int:training_id>")
    def remove_training(training_id: int):
        delete_training(database, training_id)
        return jsonify(ok=True)

    @app.put("/api/trainings/<int:training_id>/attendees")
    def put_attendees(training_id: int):
        return jsonify(set_attendees(database, training_id, _body().get("member_ids", [])))

    @app.put("/api/trainings/<int:training_id>/hours")
    def put_training_hours(training_id: int):
        body = _body()
        return jsonify(
            set_training_attend_hours(database, training_id, body.get("member_id", ""), body.get("hours"))
        )

    @app.get("/api/reports/selection")
    def get_selection_report():
        raw = request.args.get("event_id")
        event_id = int(raw) if raw else None
        return jsonify(selection_report(database, event_id))

    @app.get("/api/reports/hours")
    def get_hours_report():
        return jsonify(hours_report(database, _fy_arg()))

    @app.post("/api/demo/restore")
    def post_restore():
        restore_demo(database)
        return jsonify(ok=True)

    @app.get("/api/roster")
    def get_roster():
        return jsonify(export_roster(database))

    @app.post("/api/roster")
    def post_roster():
        return jsonify(import_roster(database, _body()))

    @app.errorhandler(ValueError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(NotFound)
    def missing(error):
        return jsonify(error=str(error)), 404

    return app


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def _fy_arg() -> int:
    raw = request.args.get("fy")
    if raw is None or raw == "":
        return FinancialYear.current().year
    try:
        year = int(raw)
    except ValueError as exc:
        raise ValueError("Financial year must be a year, for example 2026") from exc
    if year < 2000 or year > 2100:
        raise ValueError("Financial year is out of range")
    return year


def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    application = create_app(os.environ.get("ROSTER_DB", DEFAULT_DB), seed=True)
    print(f"Duty Party Roster at http://127.0.0.1:{port}")
    application.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
