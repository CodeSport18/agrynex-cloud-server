"""JSON API used by the mobile app (Bearer token) and the dashboard (session cookie)."""
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, g, jsonify, request, session
from pymongo.errors import DuplicateKeyError

from ..alerts import delete_alerts, serialize_alert
from ..auth import (check_login, check_password_hash, hash_password, issue_token, login_required,
                    password_error, public_user, start_session)
from ..db import utcnow, validate_settings

bp = Blueprint("api", __name__, url_prefix="/api")

CONTROL_TARGETS = {"system", "siren", "sprinkler"}
MAX_PAGE = 100


def db():
    return current_app.extensions["agrynex_db"]


def hub():
    return current_app.extensions["agrynex_hub"]


def storage():
    return current_app.extensions["agrynex_storage"]


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def object_id(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


# --- Auth ---

@bp.post("/auth/login")
def login():
    data = body()
    user, error = check_login(db(), data.get("email"), data.get("password"))
    if error:
        return jsonify(error=error), 401
    return jsonify(token=issue_token(user), user=public_user(user))


@bp.get("/auth/me")
@login_required()
def me():
    return jsonify(user=public_user(g.user))


@bp.post("/auth/change-password")
@login_required()
def change_password():
    data = body()
    if not check_password_hash(g.user["password_hash"], data.get("current_password") or ""):
        return jsonify(error="Current password is wrong"), 400
    new = data.get("new_password")
    error = password_error(new)
    if error:
        return jsonify(error=error), 400
    db().users.update_one(
        {"_id": g.user["_id"]},
        {"$set": {"password_hash": hash_password(new)}, "$inc": {"password_version": 1}},
    )
    user = db().users.find_one({"_id": g.user["_id"]})
    if "user_id" in session:
        # Keep the browser signed in with the new password version.
        start_session(user)
    # Old tokens stop working, so hand back a fresh one.
    return jsonify(token=issue_token(user), user=public_user(user))


# --- Status and controls ---

@bp.get("/status")
@login_required()
def status():
    return jsonify(hub().snapshot())


@bp.post("/control")
@login_required()
def control():
    data = body()
    target, on = data.get("target"), data.get("on")
    if target not in CONTROL_TARGETS or not isinstance(on, bool):
        return jsonify(error="Send target (system, siren or sprinkler) and on (true or false)"), 400

    if target == "system":
        # Saved on the server so the Pi picks it up whenever it reconnects.
        db().update_settings({"system_on": on})
        hub().push_config()
        db().log_event("control", g.user, target=target, on=on)
        return jsonify(ok=True, delivered=hub().status()["online"])

    if not hub().send_command(target, on):
        return jsonify(error="The Raspberry Pi is offline"), 503
    db().log_event("control", g.user, target=target, on=on)
    return jsonify(ok=True, delivered=True)


# --- Settings ---

@bp.get("/settings")
@login_required()
def get_settings():
    return jsonify(settings=db().get_settings())


@bp.put("/settings")
@login_required(admin=True)
def put_settings():
    changes, error = validate_settings(body())
    if error:
        return jsonify(error=error), 400
    settings = db().update_settings(changes) if changes else db().get_settings()
    hub().push_config()
    db().log_event("settings", g.user, changes=changes)
    return jsonify(settings=settings)


# --- Alerts ---

@bp.get("/alerts")
@login_required()
def list_alerts():
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), MAX_PAGE)
    except ValueError:
        return jsonify(error="limit must be a number"), 400
    query = {}
    # Page with the id of the last alert seen, which stays exact even when
    # two alerts share a timestamp.
    before = request.args.get("before")
    if before:
        cursor = db().alerts.find_one({"_id": object_id(before)}, {"created_at": 1})
        if not cursor:
            return jsonify(error="before must be an alert id"), 400
        query = {"$or": [
            {"created_at": {"$lt": cursor["created_at"]}},
            {"created_at": cursor["created_at"], "_id": {"$lt": cursor["_id"]}},
        ]}
    docs = list(db().alerts.find(query).sort([("created_at", -1), ("_id", -1)]).limit(limit))
    alerts = [serialize_alert(a, storage()) for a in docs]
    next_before = alerts[-1]["id"] if len(alerts) == limit else None
    return jsonify(alerts=alerts, next_before=next_before)


@bp.get("/alerts/<alert_id>")
@login_required()
def get_alert(alert_id):
    alert = db().alerts.find_one({"_id": object_id(alert_id)})
    if not alert:
        return jsonify(error="Alert not found"), 404
    return jsonify(alert=serialize_alert(alert, storage()))


@bp.delete("/alerts/<alert_id>")
@login_required()
def remove_alert(alert_id):
    if not delete_alerts(db(), storage(), {"_id": object_id(alert_id)}):
        return jsonify(error="Alert not found"), 404
    db().log_event("alert_deleted", g.user, alert_id=alert_id)
    return jsonify(ok=True)


# --- Users (admin only) ---

@bp.get("/users")
@login_required(admin=True)
def list_users():
    users = db().users.find().sort("created_at", 1)
    return jsonify(users=[public_user(u) for u in users])


@bp.post("/users")
@login_required(admin=True)
def create_user():
    data = body()
    email = (data.get("email") or "").strip().lower()
    role = data.get("role", "user")
    if "@" not in email:
        return jsonify(error="Enter a valid email"), 400
    if role not in ("user", "admin"):
        return jsonify(error="role must be user or admin"), 400
    error = password_error(data.get("password"))
    if error:
        return jsonify(error=error), 400
    user = {
        "email": email,
        "name": (data.get("name") or "").strip(),
        "role": role,
        "password_hash": hash_password(data["password"]),
        "password_version": 0,
        "created_at": utcnow(),
    }
    try:
        user["_id"] = db().users.insert_one(user).inserted_id
    except DuplicateKeyError:
        return jsonify(error="A user with that email already exists"), 409
    db().log_event("user_created", g.user, email=email)
    return jsonify(user=public_user(user)), 201


@bp.post("/users/<user_id>/password")
@login_required(admin=True)
def reset_password(user_id):
    password = body().get("password")
    error = password_error(password)
    if error:
        return jsonify(error=error), 400
    result = db().users.update_one(
        {"_id": object_id(user_id)},
        {"$set": {"password_hash": hash_password(password)}, "$inc": {"password_version": 1}},
    )
    if not result.matched_count:
        return jsonify(error="User not found"), 404
    db().log_event("password_reset", g.user, user_id=user_id)
    return jsonify(ok=True)


@bp.delete("/users/<user_id>")
@login_required(admin=True)
def delete_user(user_id):
    oid = object_id(user_id)
    if oid == g.user["_id"]:
        return jsonify(error="You can't delete your own account"), 400
    user = db().users.find_one({"_id": oid})
    if not user:
        return jsonify(error="User not found"), 404
    if user["role"] == "admin" and db().users.count_documents({"role": "admin"}) == 1:
        return jsonify(error="Can't delete the last admin"), 400
    db().users.delete_one({"_id": oid})
    db().log_event("user_deleted", g.user, email=user["email"])
    return jsonify(ok=True)
