from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, MongoClient

DEFAULT_SETTINGS = {
    # Whether the Pi should run detection and the camera at all.
    "system_on": True,
    # Which kinds of detection run while the system is on. Kept in sync with
    # the switches on the Pi's own local web page.
    "animal_detection": True,
    "human_detection": True,
    # Video features, each of which can be turned off separately.
    "live_view_enabled": True,
    "clips_enabled": True,
    "snapshots_enabled": True,
    # Seconds the siren and sprinkler run after an automatic detection.
    "auto_duration_s": 10,
    # Longest a manually switched-on siren or sprinkler stays on.
    "manual_max_s": 300,
    # Clip length around a detection.
    "clip_pre_s": 5,
    "clip_post_s": 10,
    # Minimum gap between two alerts, so one animal doesn't create dozens.
    "alert_cooldown_s": 30,
    "retention_days": 30,
}

# (min, max) for each numeric setting.
SETTING_LIMITS = {
    "auto_duration_s": (1, 600),
    "manual_max_s": (10, 3600),
    "clip_pre_s": (0, 15),
    "clip_post_s": (1, 30),
    "alert_cooldown_s": (0, 3600),
    "retention_days": (1, 365),
}

BOOL_SETTINGS = {"system_on", "animal_detection", "human_detection",
                 "live_view_enabled", "clips_enabled", "snapshots_enabled"}

# Changed through the control endpoint rather than the settings form.
CONTROL_SETTINGS = {"system_on", "animal_detection", "human_detection"}


def utcnow():
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri, db_name, client=None):
        self.client = client or MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=10000)
        self.db = self.client[db_name]
        self.users = self.db.users
        self.settings = self.db.settings
        self.device = self.db.device
        self.alerts = self.db.alerts
        self.events = self.db.events

    def ensure_indexes(self):
        self.users.create_index([("email", ASCENDING)], unique=True)
        self.alerts.create_index([("created_at", DESCENDING), ("_id", DESCENDING)])
        self.events.create_index([("at", DESCENDING)])

    def get_settings(self):
        doc = self.settings.find_one({"_id": "settings"}) or {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in doc.items() if k in DEFAULT_SETTINGS})
        return merged

    def update_settings(self, changes):
        self.settings.update_one({"_id": "settings"}, {"$set": changes}, upsert=True)
        return self.get_settings()

    def log_event(self, kind, user=None, **details):
        self.events.insert_one({
            "at": utcnow(),
            "kind": kind,
            "user": user["email"] if user else None,
            **details,
        })


def validate_settings(data):
    """Return (changes, error) for a partial settings update."""
    changes = {}
    for key, value in data.items():
        if key not in DEFAULT_SETTINGS or key in CONTROL_SETTINGS:
            return None, f"Unknown setting: {key}"
        if key in BOOL_SETTINGS:
            if not isinstance(value, bool):
                return None, f"{key} must be true or false"
            changes[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"{key} must be a whole number"
            low, high = SETTING_LIMITS[key]
            if not low <= value <= high:
                return None, f"{key} must be between {low} and {high}"
            changes[key] = value
    return changes, None
