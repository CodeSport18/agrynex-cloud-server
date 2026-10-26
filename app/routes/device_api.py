"""HTTP endpoints the Raspberry Pi calls to report alerts and upload their media."""
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from ..alerts import MEDIA_STATUSES, MEDIA_TYPES, media_key, serialize_alert
from ..auth import device_required
from ..db import utcnow
from ..notifications import notify_new_alert

bp = Blueprint("device_api", __name__, url_prefix="/api/device")

DETECTION_KINDS = {"animal", "human"}
MAX_DETECTIONS = 50


def _ext(name):
    return current_app.extensions[name]


def _detected_at(value):
    """The Pi's detection time, if it's sane, else now."""
    now = utcnow()
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return now
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Allow for alerts queued while offline, but not a broken clock.
    if when > now or (now - when).days > 1:
        return now
    return when


@bp.post("/alerts")
@device_required
def create_alert():
    data = request.get_json(silent=True) or {}
    detections = []
    for d in (data.get("detections") or [])[:MAX_DETECTIONS]:
        if isinstance(d, dict) and d.get("kind") in DETECTION_KINDS:
            detections.append({
                "kind": d["kind"],
                "label": str(d.get("label", d["kind"]))[:40],
                "confidence": round(float(d.get("confidence", 0)), 3),
            })
    if not detections:
        return jsonify(error="An alert needs at least one animal or human detection"), 400

    alert_id = ObjectId()
    alert = {
        "_id": alert_id,
        "created_at": _detected_at(data.get("detected_at")),
        "kinds": sorted({d["kind"] for d in detections}),
        "detections": detections,
    }
    upload_urls = {}
    storage = _ext("agrynex_storage")
    for kind, (_, content_type) in MEDIA_TYPES.items():
        if data.get(kind):
            key = media_key(alert_id, kind)
            alert[kind] = {"key": key, "status": "pending"}
            upload_urls[kind] = {"url": storage.upload_url(key, content_type), "content_type": content_type}

    db = _ext("agrynex_db")
    db.alerts.insert_one(alert)
    serialized = serialize_alert(alert, storage)
    _ext("agrynex_hub").broadcast_alert("alert", serialized)
    notify_new_alert(serialized)
    return jsonify(id=str(alert_id), uploads=upload_urls), 201


@bp.post("/alerts/<alert_id>/media")
@device_required
def media_done(alert_id):
    """The Pi reports each upload as ready or failed."""
    data = request.get_json(silent=True) or {}
    try:
        oid = ObjectId(alert_id)
    except InvalidId:
        return jsonify(error="Alert not found"), 404
    db = _ext("agrynex_db")
    storage = _ext("agrynex_storage")
    alert = db.alerts.find_one({"_id": oid})
    if not alert:
        return jsonify(error="Alert not found"), 404

    changes = {}
    for kind in MEDIA_TYPES:
        status = data.get(kind)
        if status is None or not alert.get(kind):
            continue
        if status not in MEDIA_STATUSES:
            return jsonify(error=f"{kind} must be ready or failed"), 400
        if status == "ready" and not storage.exists(alert[kind]["key"]):
            return jsonify(error=f"{kind} hasn't been uploaded"), 400
        changes[f"{kind}.status"] = status
    if changes:
        db.alerts.update_one({"_id": oid}, {"$set": changes})
        alert = db.alerts.find_one({"_id": oid})
        _ext("agrynex_hub").broadcast_alert("alert_updated", serialize_alert(alert, storage))
    return jsonify(ok=True)
