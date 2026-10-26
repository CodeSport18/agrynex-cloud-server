import hmac
import threading
import time
from datetime import timedelta
from functools import wraps

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from flask import current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import utcnow

MIN_PASSWORD_LENGTH = 8

# Failed logins per email, to slow down password guessing.
MAX_FAILED_LOGINS = 10
FAILED_LOGIN_WINDOW_S = 15 * 60
_failed_logins = {}
_failed_lock = threading.Lock()


def hash_password(password):
    return generate_password_hash(password)


def public_user(user):
    return {
        "id": str(user["_id"]),
        "email": user["email"],
        "name": user.get("name", ""),
        "role": user["role"],
    }


def password_error(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


def _recent_failures(email):
    cutoff = time.time() - FAILED_LOGIN_WINDOW_S
    with _failed_lock:
        attempts = [t for t in _failed_logins.get(email, []) if t > cutoff]
        _failed_logins[email] = attempts
        return len(attempts)


def check_login(db, email, password):
    """Return (user, error)."""
    email = (email or "").strip().lower()
    if _recent_failures(email) >= MAX_FAILED_LOGINS:
        return None, "Too many failed attempts. Try again in 15 minutes."
    user = db.users.find_one({"email": email})
    if not user or not check_password_hash(user["password_hash"], password or ""):
        with _failed_lock:
            _failed_logins.setdefault(email, []).append(time.time())
        return None, "Wrong email or password"
    with _failed_lock:
        _failed_logins.pop(email, None)
    return user, None


def issue_token(user):
    now = utcnow()
    payload = {
        "sub": str(user["_id"]),
        # Bumped when the password changes, which signs out old tokens.
        "pwv": user.get("password_version", 0),
        "iat": now,
        "exp": now + timedelta(days=current_app.config["TOKEN_TTL_DAYS"]),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def _find_user(db, user_id, password_version):
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except (InvalidId, TypeError):
        return None
    if not user or user.get("password_version", 0) != password_version:
        return None
    return user


def user_from_token(db, token):
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return _find_user(db, payload.get("sub"), payload.get("pwv"))


def user_from_session(db):
    if "user_id" not in session:
        return None
    return _find_user(db, session["user_id"], session.get("pwv"))


def start_session(user):
    session.clear()
    session["user_id"] = str(user["_id"])
    session["pwv"] = user.get("password_version", 0)
    session.permanent = True


def current_user(db):
    """User from a Bearer token (mobile app) or the session cookie (browser)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return user_from_token(db, header[7:]), "token"
    return user_from_session(db), "session"


def login_required(admin=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            db = current_app.extensions["agrynex_db"]
            user, method = current_user(db)
            if not user:
                return jsonify(error="Not signed in"), 401
            # Cookie-based requests that change things must come from our own
            # pages, which send this header. Other sites can't add it.
            if (method == "session" and request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.headers.get("X-Requested-With") != "fetch"):
                return jsonify(error="Missing X-Requested-With header"), 400
            if admin and user["role"] != "admin":
                return jsonify(error="Only an admin can do this"), 403
            g.user = user
            return view(*args, **kwargs)
        return wrapper
    return decorator


def valid_device_token(token):
    expected = current_app.config["DEVICE_TOKEN"]
    return bool(token) and hmac.compare_digest(str(token), expected)


def device_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or not valid_device_token(header[7:]):
            return jsonify(error="Bad device token"), 401
        return view(*args, **kwargs)
    return wrapper
