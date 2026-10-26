from datetime import timedelta

import boto3
import mongomock
import pytest
from moto import mock_aws

from app import create_app
from app.alerts import delete_expired_alerts
from app.db import utcnow

DEVICE_TOKEN = "device-secret"
BUCKET = "agrynex-test"


@pytest.fixture
def app():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        app = create_app({
            "SECRET_KEY": "test-secret-that-is-long-enough-for-hs256",
            "MONGODB_URI": "mongodb://unused",
            "DEVICE_TOKEN": DEVICE_TOKEN,
            "S3_BUCKET": BUCKET,
            "ADMIN_EMAIL": "admin@example.com",
            "ADMIN_PASSWORD": "admin-pass-1",
            "RUN_BACKGROUND_JOBS": False,
            "SESSION_COOKIE_SECURE": False,
        }, mongo_client=mongomock.MongoClient(tz_aware=True))
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def socketio(app):
    return app.extensions["socketio"]


def login(client, email="admin@example.com", password="admin-pass-1"):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.json
    return {"Authorization": f"Bearer {res.json['token']}"}


def device_socket(app, socketio):
    return socketio.test_client(app, namespace="/device", auth={"token": DEVICE_TOKEN})


def events(sock, namespace, name):
    return [e["args"][0] for e in sock.get_received(namespace) if e["name"] == name]


def test_login_and_me(client):
    headers = login(client)
    res = client.get("/api/auth/me", headers=headers)
    assert res.json["user"]["role"] == "admin"
    assert client.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_password_change_signs_out_old_tokens(client):
    headers = login(client)
    res = client.post("/api/auth/change-password", headers=headers,
                      json={"current_password": "admin-pass-1", "new_password": "new-pass-22"})
    assert res.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    new_headers = {"Authorization": f"Bearer {res.json['token']}"}
    assert client.get("/api/auth/me", headers=new_headers).status_code == 200


def test_browser_session_needs_header_for_changes(client):
    client.post("/login", data={"email": "admin@example.com", "password": "admin-pass-1"})
    assert client.get("/api/settings").status_code == 200
    assert client.put("/api/settings", json={"retention_days": 10}).status_code == 400
    res = client.put("/api/settings", json={"retention_days": 10}, headers={"X-Requested-With": "fetch"})
    assert res.status_code == 200
    assert res.json["settings"]["retention_days"] == 10


def test_login_page_ignores_offsite_next(client):
    res = client.post("/login?next=//evil.example", data={"email": "admin@example.com", "password": "admin-pass-1"})
    assert res.headers["Location"] == "/"


def test_settings_validation(client):
    headers = login(client)
    assert client.put("/api/settings", headers=headers, json={"auto_duration_s": 0}).status_code == 400
    assert client.put("/api/settings", headers=headers, json={"clips_enabled": "yes"}).status_code == 400
    assert client.put("/api/settings", headers=headers, json={"system_on": False}).status_code == 400
    assert client.put("/api/settings", headers=headers, json={"bogus": 1}).status_code == 400
    res = client.put("/api/settings", headers=headers, json={"auto_duration_s": 20, "clips_enabled": False})
    assert res.json["settings"]["auto_duration_s"] == 20
    assert res.json["settings"]["clips_enabled"] is False


def test_users_admin_only(client):
    admin = login(client)
    res = client.post("/api/users", headers=admin,
                      json={"email": "Farmer@Example.com", "password": "farmer-pass", "name": "Farmer"})
    assert res.status_code == 201
    assert res.json["user"]["email"] == "farmer@example.com"
    assert client.post("/api/users", headers=admin,
                       json={"email": "farmer@example.com", "password": "farmer-pass"}).status_code == 409

    user = login(client, "farmer@example.com", "farmer-pass")
    assert client.get("/api/users", headers=user).status_code == 403
    assert client.put("/api/settings", headers=user, json={"retention_days": 5}).status_code == 403
    # Regular users can still use the controls.
    assert client.post("/api/control", headers=user, json={"target": "system", "on": False}).status_code == 200

    me = client.get("/api/auth/me", headers=admin).json["user"]
    assert client.delete(f"/api/users/{me['id']}", headers=admin).status_code == 400


def test_bad_device_token_is_rejected(app, socketio, client):
    sock = socketio.test_client(app, namespace="/device", auth={"token": "wrong"})
    assert not sock.is_connected("/device")
    assert client.post("/api/device/alerts", json={}).status_code == 401


def test_controls_reach_the_pi(app, socketio, client):
    headers = login(client)
    assert client.post("/api/control", headers=headers, json={"target": "siren", "on": True}).status_code == 503

    pi = device_socket(app, socketio)
    config = events(pi, "/device", "config")
    assert config[-1]["system_on"] is True
    assert config[-1]["auto_duration_s"] == 10

    res = client.post("/api/control", headers=headers, json={"target": "sprinkler", "on": True})
    assert res.status_code == 200
    assert events(pi, "/device", "command") == [{"target": "sprinkler", "on": True}]

    client.post("/api/control", headers=headers, json={"target": "system", "on": False})
    assert events(pi, "/device", "config")[-1]["system_on"] is False

    client.put("/api/settings", headers=headers, json={"manual_max_s": 60})
    assert events(pi, "/device", "config")[-1]["manual_max_s"] == 60


def test_state_reaches_clients(app, socketio, client):
    token = login(client)["Authorization"][7:]
    viewer = socketio.test_client(app, namespace="/client", auth={"token": token})
    assert viewer.is_connected("/client")
    viewer.get_received("/client")

    pi = device_socket(app, socketio)
    pi.emit("state", {"system_on": True, "siren": {"on": True, "manual": True}}, namespace="/device")
    status = events(viewer, "/client", "status")[-1]
    assert status["device"]["online"] is True
    assert status["device"]["state"]["siren"]["on"] is True

    pi.disconnect(namespace="/device")
    assert events(viewer, "/client", "status")[-1]["device"]["online"] is False


def test_client_socket_needs_login(app, socketio):
    sock = socketio.test_client(app, namespace="/client", auth={"token": "bad"})
    assert not sock.is_connected("/client")


def test_live_view_only_streams_while_watched(app, socketio, client):
    token = login(client)["Authorization"][7:]
    pi = device_socket(app, socketio)
    pi.get_received("/device")
    viewer = socketio.test_client(app, namespace="/client", auth={"token": token})

    viewer.emit("live_start", namespace="/client")
    assert events(pi, "/device", "live") == [{"on": True}]

    pi.emit("frame", b"\xff\xd8jpeg", namespace="/device")
    assert events(viewer, "/client", "frame") == [b"\xff\xd8jpeg"]

    viewer.emit("live_stop", namespace="/client")
    assert events(pi, "/device", "live") == [{"on": False}]

    # Turning live view off in settings stops it even while someone watches.
    viewer.emit("live_start", namespace="/client")
    pi.get_received("/device")
    client.put("/api/settings", headers={"Authorization": f"Bearer {token}"}, json={"live_view_enabled": False})
    assert events(pi, "/device", "live") == [{"on": False}]


def create_device_alert(client, **media):
    return client.post("/api/device/alerts", headers={"Authorization": f"Bearer {DEVICE_TOKEN}"}, json={
        "detections": [{"kind": "animal", "label": "dog", "confidence": 0.91}],
        **media,
    })


def test_alert_flow(app, socketio, client):
    headers = login(client)
    token = headers["Authorization"][7:]
    viewer = socketio.test_client(app, namespace="/client", auth={"token": token})
    viewer.get_received("/client")

    res = create_device_alert(client, snapshot=True, clip=True)
    assert res.status_code == 201
    alert_id = res.json["id"]
    assert set(res.json["uploads"]) == {"snapshot", "clip"}
    assert res.json["uploads"]["snapshot"]["content_type"] == "image/jpeg"
    assert events(viewer, "/client", "alert")[0]["snapshot_status"] == "pending"

    dev = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
    # Can't mark ready before the file is in S3.
    assert client.post(f"/api/device/alerts/{alert_id}/media", headers=dev, json={"snapshot": "ready"}).status_code == 400

    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=BUCKET, Key=f"alerts/{alert_id}/snapshot.jpg", Body=b"jpeg")
    assert client.post(f"/api/device/alerts/{alert_id}/media", headers=dev,
                       json={"snapshot": "ready", "clip": "failed"}).status_code == 200
    updated = events(viewer, "/client", "alert_updated")[0]
    assert updated["snapshot_status"] == "ready"
    assert updated["snapshot_url"].startswith("https://")
    assert updated["clip_status"] == "failed"

    alerts = client.get("/api/alerts", headers=headers).json["alerts"]
    assert [a["id"] for a in alerts] == [alert_id]
    assert alerts[0]["kinds"] == ["animal"]

    assert client.delete(f"/api/alerts/{alert_id}", headers=headers).status_code == 200
    assert client.get("/api/alerts", headers=headers).json["alerts"] == []


def test_alert_needs_detections(client):
    res = client.post("/api/device/alerts", headers={"Authorization": f"Bearer {DEVICE_TOKEN}"},
                      json={"detections": [{"kind": "alien"}]})
    assert res.status_code == 400


def test_alert_paging(client):
    headers = login(client)
    for _ in range(5):
        create_device_alert(client)
    first = client.get("/api/alerts?limit=3", headers=headers).json
    assert len(first["alerts"]) == 3
    second = client.get("/api/alerts", headers=headers,
                        query_string={"limit": 3, "before": first["next_before"]}).json
    assert len(second["alerts"]) == 2
    assert second["next_before"] is None


def test_retention_deletes_old_alerts(app, client):
    db = app.extensions["agrynex_db"]
    storage = app.extensions["agrynex_storage"]
    old_id = create_device_alert(client, snapshot=True).json["id"]
    create_device_alert(client)
    from bson import ObjectId
    db.alerts.update_one({"_id": ObjectId(old_id)}, {"$set": {"created_at": utcnow() - timedelta(days=31)}})
    assert delete_expired_alerts(db, storage) == 1
    assert db.alerts.count_documents({}) == 1


def test_detection_switches_reach_the_pi(app, socketio, client):
    headers = login(client)
    pi = device_socket(app, socketio)
    config = events(pi, "/device", "config")[-1]
    assert config["animal_detection"] is True and config["human_detection"] is True

    res = client.post("/api/control", headers=headers, json={"target": "human_detection", "on": False})
    assert res.status_code == 200
    config = events(pi, "/device", "config")[-1]
    assert config["human_detection"] is False and config["animal_detection"] is True
    assert client.get("/api/settings", headers=headers).json["settings"]["human_detection"] is False
    # Like system on/off, it's not part of the settings form.
    assert client.put("/api/settings", headers=headers, json={"animal_detection": False}).status_code == 400


def test_detection_change_on_the_pi_reaches_clients(app, socketio, client):
    headers = login(client)
    viewer = socketio.test_client(app, namespace="/client", auth={"token": headers["Authorization"][7:]})
    pi = device_socket(app, socketio)
    pi.get_received("/device")
    viewer.get_received("/client")

    pi.emit("detection_changed", {"animal_detection": False, "human_detection": True, "bogus": 1},
            namespace="/device")
    settings = client.get("/api/settings", headers=headers).json["settings"]
    assert settings["animal_detection"] is False and settings["human_detection"] is True
    assert "bogus" not in settings
    assert events(viewer, "/client", "status")[-1]["settings"]["animal_detection"] is False
    # The Pi gets the saved config back as confirmation.
    assert events(pi, "/device", "config")[-1]["animal_detection"] is False


def test_new_pi_connection_replaces_the_old_one(app, socketio):
    old = device_socket(app, socketio)
    new = device_socket(app, socketio)
    assert not old.is_connected("/device")
    assert new.is_connected("/device")
