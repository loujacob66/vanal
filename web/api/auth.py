from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from vanal.auth import auth_required, make_token, verify_password, verify_token

router = APIRouter()


# ── Dependency used by all write endpoints ─────────────────────────
def require_auth(session_token: str | None = Cookie(default=None)):
    if not verify_token(session_token):
        raise HTTPException(status_code=401, detail="Authentication required")


# ── Routes ─────────────────────────────────────────────────────────
@router.get("/auth/status")
def auth_status(session_token: str | None = Cookie(default=None)):
    return {
        "required": auth_required(),
        "authenticated": verify_token(session_token),
    }


class LoginRequest(BaseModel):
    password: str


@router.post("/auth/login")
def login(req: LoginRequest):
    if not verify_password(req.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "session_token",
        make_token(),
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 7,  # 7 days
    )
    return response


@router.post("/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    return response
