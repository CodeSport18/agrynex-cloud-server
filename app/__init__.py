import logging
from datetime import timedelta

from flask import Flask
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from .alerts import delete_expired_alerts
from .auth import hash_password
from .config import Config
from .db import Database, utcnow
from .hub import DeviceHub
from .storage import MediaStorage

log = logging.getLogger("agrynex")

CLEANUP_INTERVAL_S = 60 * 60


def create_app(overrides=None, mongo_client=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config.update(overrides or {})
    missing = [k for k in Config.REQUIRED if not app.config.get(k)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    app.permanent_session_lifetime = timedelta(days=app.config["TOKEN_TTL_DAYS"])

    # Render sits behind a proxy that terminates HTTPS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db = Database(app.config["MONGODB_URI"], app.config["MONGODB_DB"], client=mongo_client)
    db.ensure_indexes()
    storage = MediaStorage(
        app.config["S3_BUCKET"], app.config["AWS_REGION"],
        endpoint_url=app.config["S3_ENDPOINT_URL"], url_ttl_s=app.config["MEDIA_URL_TTL_S"],
    )
    # Live frames are JPEGs of up to a few hundred KB.
    socketio = SocketIO(app, async_mode="threading", max_http_buffer_size=2_000_000)
    hub = DeviceHub(socketio, db)
    hub.register()

    app.extensions["agrynex_db"] = db
    app.extensions["agrynex_storage"] = storage
    app.extensions["agrynex_hub"] = hub

    from .routes import api, device_api, web
    app.register_blueprint(api.bp)
    app.register_blueprint(device_api.bp)
    app.register_blueprint(web.bp)

    _create_first_admin(app, db)
    # The Pi isn't connected to a freshly started server yet.
    db.device.update_one({"_id": "device"}, {"$set": {"online": False}}, upsert=True)
    if app.config["RUN_BACKGROUND_JOBS"]:
        socketio.start_background_task(_cleanup_loop, socketio, db, storage)
    return app


def _create_first_admin(app, db):
    if db.users.count_documents({}) > 0:
        return
    email, password = app.config["ADMIN_EMAIL"], app.config["ADMIN_PASSWORD"]
    if not email or not password:
        log.warning("No users yet. Set ADMIN_EMAIL and ADMIN_PASSWORD to create the first admin.")
        return
    db.users.insert_one({
        "email": email.strip().lower(),
        "name": "Admin",
        "role": "admin",
        "password_hash": hash_password(password),
        "password_version": 0,
        "created_at": utcnow(),
    })
    log.warning("Created admin account %s", email)


def _cleanup_loop(socketio, db, storage):
    while True:
        try:
            removed = delete_expired_alerts(db, storage)
            if removed:
                log.info("Deleted %d expired alerts", removed)
        except Exception:
            log.exception("Alert cleanup failed")
        socketio.sleep(CLEANUP_INTERVAL_S)
