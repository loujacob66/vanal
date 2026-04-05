import os

from fastapi import APIRouter, Cookie, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse

from authlib.integrations.starlette_client import OAuth

from vanal import db
from vanal.auth import make_session_token, verify_session_token

router = APIRouter()

REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
FTM_PREFIX = os.getenv("FTM_PREFIX", "If you are stupid...")

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
)


# ── Dependency used by all write endpoints ─────────────────────────
def require_auth(
    session_token: str | None = Cookie(default=None),
    impersonate_id: str | None = Cookie(default=None),
):
    """Verify session and return the current user dict.

    If the caller is an admin and has a valid impersonate_id cookie,
    return the impersonated user's data instead (with is_admin forced to 0
    and a real_admin dict attached so the frontend knows).
    """
    user_id = verify_session_token(session_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, picture_url, is_admin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    real_user = dict(row)

    # Admin impersonation
    if real_user["is_admin"] and impersonate_id:
        try:
            target_id = int(impersonate_id)
        except (ValueError, TypeError):
            return real_user
        if target_id != real_user["id"]:
            with db.get_conn() as conn:
                target = conn.execute(
                    "SELECT id, email, name, picture_url, is_admin FROM users WHERE id = ?",
                    (target_id,),
                ).fetchone()
            if target:
                impersonated = dict(target)
                impersonated["is_admin"] = 0  # see exactly what user sees
                impersonated["impersonating"] = True
                impersonated["real_admin"] = {
                    "id": real_user["id"],
                    "name": real_user["name"],
                    "email": real_user["email"],
                }
                return impersonated

    return real_user


def require_admin(session_token: str | None = Cookie(default=None)):
    """Like require_auth but also enforces is_admin=1.  Ignores impersonation."""
    user_id = verify_session_token(session_token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, name, picture_url, is_admin FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    user = dict(row)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Routes ─────────────────────────────────────────────────────────
@router.get("/auth/status")
def auth_status(
    session_token: str | None = Cookie(default=None),
    impersonate_id: str | None = Cookie(default=None),
):
    user_id = verify_session_token(session_token)
    user = None
    real_admin = None
    if user_id:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, email, name, picture_url, is_admin FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row:
            user = dict(row)
            # Check impersonation
            if user["is_admin"] and impersonate_id:
                try:
                    target_id = int(impersonate_id)
                except (ValueError, TypeError):
                    target_id = None
                if target_id and target_id != user["id"]:
                    with db.get_conn() as conn:
                        target = conn.execute(
                            "SELECT id, email, name, picture_url, is_admin FROM users WHERE id = ?",
                            (target_id,),
                        ).fetchone()
                    if target:
                        real_admin = {"id": user["id"], "name": user["name"], "email": user["email"]}
                        user = dict(target)
                        user["is_admin"] = 0
    return {
        "required": True,
        "authenticated": user is not None,
        "user": user,
        "impersonating": real_admin is not None,
        "real_admin": real_admin,
        "ftm_prefix": FTM_PREFIX,
    }


@router.get("/auth/google")
async def google_login(request: Request):
    return await oauth.google.authorize_redirect(request, REDIRECT_URI)


@router.get("/auth/google/callback")
async def google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo or not userinfo.get("email"):
        raise HTTPException(status_code=400, detail="Google did not return user info")

    email = userinfo["email"]
    name = userinfo.get("name", "")
    picture_url = userinfo.get("picture", "")

    with db.get_conn() as conn:
        # Upsert user
        conn.execute(
            "INSERT OR IGNORE INTO users (email, name, picture_url) VALUES (?, ?, ?)",
            (email, name, picture_url),
        )
        conn.execute(
            "UPDATE users SET name = ?, picture_url = ?, updated_at = datetime('now') WHERE email = ?",
            (name, picture_url, email),
        )

        # Determine admin status
        user = dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())
        user_id = user["id"]

        if not user["is_admin"]:
            should_be_admin = (ADMIN_EMAIL and email.lower() == ADMIN_EMAIL.lower()) or (
                conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0
            )
            if should_be_admin:
                conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
                user["is_admin"] = 1

        # Assign unowned clips to admin on their first login
        if user["is_admin"]:
            conn.execute(
                "UPDATE clips SET owner_id = ? WHERE owner_id IS NULL",
                (user_id,),
            )

    response = RedirectResponse(url="/")
    response.set_cookie(
        "session_token",
        make_session_token(user_id),
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,  # 30 days
    )
    return response


@router.post("/auth/impersonate/{user_id}")
def impersonate(user_id: int, _admin=Depends(require_admin)):
    """Set impersonation cookie.  Admin only."""
    if user_id == _admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot impersonate yourself")
    with db.get_conn() as conn:
        target = conn.execute("SELECT id, name FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    response = JSONResponse({"ok": True, "user": dict(target)})
    response.set_cookie(
        "impersonate_id",
        str(user_id),
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 4,  # 4 hours
    )
    return response


@router.post("/auth/stop-impersonating")
def stop_impersonating():
    """Clear impersonation cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie("impersonate_id")
    return response


@router.get("/auth/users")
def list_users(_admin=Depends(require_admin)):
    """List all users.  Admin only."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, name, picture_url, is_admin, created_at FROM users ORDER BY name",
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    response.delete_cookie("impersonate_id")
    return response
