"""Browser dashboard pages. Each page loads its data from the JSON API."""
from functools import wraps

from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for

from ..auth import check_login, start_session, user_from_session

bp = Blueprint("web", __name__)


def page(admin=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = user_from_session(current_app.extensions["agrynex_db"])
            if not user:
                return redirect(url_for("web.login", next=request.path))
            if admin and user["role"] != "admin":
                return redirect(url_for("web.dashboard"))
            g.user = user
            return view(*args, **kwargs)
        return wrapper
    return decorator


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        user, error = check_login(
            current_app.extensions["agrynex_db"], request.form.get("email"), request.form.get("password")
        )
        if user:
            start_session(user)
            target = request.args.get("next", "")
            # Only follow local paths, never another site.
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("web.dashboard")
            return redirect(target)
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@bp.get("/")
@page()
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@bp.get("/alerts")
@page()
def alerts():
    return render_template("alerts.html", active="alerts")


@bp.get("/settings")
@page()
def settings():
    return render_template("settings.html", active="settings")


@bp.get("/users")
@page(admin=True)
def users():
    return render_template("users.html", active="users")


@bp.get("/account")
@page()
def account():
    return render_template("account.html", active="account")


@bp.get("/healthz")
def healthz():
    return {"ok": True}
