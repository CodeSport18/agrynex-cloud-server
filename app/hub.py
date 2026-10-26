"""Real-time link between the Raspberry Pi and the people watching it.

The Pi connects to the /device namespace and the app and dashboard connect to
/client. Commands and settings go down to the Pi, and its state and live video
frames come back up and are passed on to clients.
"""
import threading

from flask import request
from flask_socketio import disconnect, join_room, leave_room

from .auth import user_from_session, user_from_token, valid_device_token
from .db import utcnow

DEVICE_NS = "/device"
CLIENT_NS = "/client"
VIEWERS_ROOM = "viewers"

# Settings the Pi needs to know about.
DEVICE_CONFIG_KEYS = [
    "system_on", "live_view_enabled", "clips_enabled", "snapshots_enabled",
    "auto_duration_s", "manual_max_s", "clip_pre_s", "clip_post_s", "alert_cooldown_s",
]


class DeviceHub:
    def __init__(self, socketio, db):
        self.socketio = socketio
        self.db = db
        self.lock = threading.Lock()
        self.device_sid = None
        self.device_state = {}
        self.viewers = set()
        self.streaming = False

    # --- State shared with clients ---

    def status(self):
        doc = self.db.device.find_one({"_id": "device"}) or {}
        last_seen = doc.get("last_seen")
        with self.lock:
            return {
                "online": self.device_sid is not None,
                "last_seen": last_seen.isoformat() if last_seen else None,
                "state": dict(self.device_state),
                "viewers": len(self.viewers),
            }

    def snapshot(self):
        """Everything a client needs to draw the dashboard."""
        return {"device": self.status(), "settings": self.db.get_settings()}

    def broadcast_status(self):
        self.socketio.emit("status", self.snapshot(), namespace=CLIENT_NS)

    def broadcast_alert(self, event, alert):
        self.socketio.emit(event, alert, namespace=CLIENT_NS)

    # --- Talking to the Pi ---

    def device_config(self):
        settings = self.db.get_settings()
        return {k: settings[k] for k in DEVICE_CONFIG_KEYS}

    def push_config(self):
        with self.lock:
            sid = self.device_sid
        if sid:
            self.socketio.emit("config", self.device_config(), namespace=DEVICE_NS, to=sid)
        self._update_live()
        self.broadcast_status()

    def send_command(self, target, on):
        """Return True if the Pi was connected to receive the command."""
        with self.lock:
            sid = self.device_sid
        if not sid:
            return False
        self.socketio.emit("command", {"target": target, "on": on}, namespace=DEVICE_NS, to=sid)
        return True

    def _update_live(self):
        """Ask the Pi to stream only while someone is watching."""
        settings = self.db.get_settings()
        with self.lock:
            want = bool(self.device_sid and self.viewers
                        and settings["live_view_enabled"] and settings["system_on"])
            if want == self.streaming:
                return
            self.streaming = want
            sid = self.device_sid
        if sid:
            self.socketio.emit("live", {"on": want}, namespace=DEVICE_NS, to=sid)

    def _touch_device(self, **fields):
        self.db.device.update_one(
            {"_id": "device"}, {"$set": {"last_seen": utcnow(), **fields}}, upsert=True
        )

    # --- Socket.IO handlers ---

    def register(self):
        sio = self.socketio

        @sio.on("connect", namespace=DEVICE_NS)
        def device_connect(auth):
            if not valid_device_token((auth or {}).get("token")):
                return False
            with self.lock:
                old_sid = self.device_sid
                self.device_sid = request.sid
                self.device_state = {}
                self.streaming = False
            if old_sid:
                disconnect(old_sid, namespace=DEVICE_NS)
            self._touch_device(online=True)
            self.db.log_event("device_connected")
            self.push_config()

        @sio.on("disconnect", namespace=DEVICE_NS)
        def device_disconnect(*_):
            with self.lock:
                if request.sid != self.device_sid:
                    return
                self.device_sid = None
                self.device_state = {}
                self.streaming = False
            self._touch_device(online=False)
            self.db.log_event("device_disconnected")
            self.broadcast_status()

        @sio.on("state", namespace=DEVICE_NS)
        def device_state(state):
            with self.lock:
                if request.sid != self.device_sid or not isinstance(state, dict):
                    return
                self.device_state = state
            self._touch_device(online=True)
            self.broadcast_status()

        @sio.on("frame", namespace=DEVICE_NS)
        def device_frame(data):
            with self.lock:
                ok = request.sid == self.device_sid and self.streaming
            if ok and isinstance(data, (bytes, bytearray)):
                sio.emit("frame", data, namespace=CLIENT_NS, to=VIEWERS_ROOM)

        @sio.on("connect", namespace=CLIENT_NS)
        def client_connect(auth):
            token = (auth or {}).get("token")
            user = user_from_token(self.db, token) if token else user_from_session(self.db)
            if not user:
                return False
            sio.emit("status", self.snapshot(), namespace=CLIENT_NS, to=request.sid)

        @sio.on("disconnect", namespace=CLIENT_NS)
        def client_disconnect(*_):
            with self.lock:
                self.viewers.discard(request.sid)
            self._update_live()

        @sio.on("live_start", namespace=CLIENT_NS)
        def live_start(*_):
            join_room(VIEWERS_ROOM)
            with self.lock:
                self.viewers.add(request.sid)
            self._update_live()

        @sio.on("live_stop", namespace=CLIENT_NS)
        def live_stop(*_):
            leave_room(VIEWERS_ROOM)
            with self.lock:
                self.viewers.discard(request.sid)
            self._update_live()
