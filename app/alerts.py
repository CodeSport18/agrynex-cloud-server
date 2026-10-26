from datetime import timedelta

from .db import utcnow

MEDIA_TYPES = {
    "snapshot": ("jpg", "image/jpeg"),
    "clip": ("mp4", "video/mp4"),
}
MEDIA_STATUSES = {"ready", "failed"}


def media_key(alert_id, kind):
    ext, _ = MEDIA_TYPES[kind]
    return f"alerts/{alert_id}/{kind}.{ext}"


def serialize_alert(alert, storage):
    out = {
        "id": str(alert["_id"]),
        "created_at": alert["created_at"].isoformat(),
        "kinds": alert.get("kinds", []),
        "detections": alert.get("detections", []),
    }
    for kind in MEDIA_TYPES:
        media = alert.get(kind)
        status = media["status"] if media else "none"
        out[f"{kind}_status"] = status
        out[f"{kind}_url"] = storage.view_url(media["key"]) if status == "ready" else None
    return out


def delete_alerts(db, storage, query):
    """Delete matching alerts and their media. Returns how many were removed."""
    alerts = list(db.alerts.find(query, {"snapshot": 1, "clip": 1}))
    if not alerts:
        return 0
    keys = [a[k]["key"] for a in alerts for k in MEDIA_TYPES if a.get(k)]
    storage.delete(keys)
    db.alerts.delete_many({"_id": {"$in": [a["_id"] for a in alerts]}})
    return len(alerts)


def delete_expired_alerts(db, storage):
    days = db.get_settings()["retention_days"]
    return delete_alerts(db, storage, {"created_at": {"$lt": utcnow() - timedelta(days=days)}})
