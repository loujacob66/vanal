import json

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from vanal import db
from vanal.vision import suggest_ordering
from web.api.auth import require_auth
from web.api.clips import _owner_where

router = APIRouter()


class SuggestRequest(BaseModel):
    clip_ids: list[int] = []  # empty = use all done clips


@router.post("/order/ai-suggest")
def ai_suggest_order(req: SuggestRequest = Body(default_factory=SuggestRequest), _auth=Depends(require_auth)):
    """Ask the LLM to suggest a narrative ordering.

    If clip_ids is provided, only those clips are ordered (partial ordering).
    Otherwise all done clips owned by the current user are used.
    """
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        if req.clip_ids:
            placeholders = ",".join("?" * len(req.clip_ids))
            rows = conn.execute(
                f"SELECT id, filename, synopsis, owner_id FROM clips "
                f"WHERE id IN ({placeholders}) AND status = 'done' {owner_frag} ORDER BY filename",
                req.clip_ids + owner_params,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT id, filename, synopsis FROM clips WHERE status = 'done' {owner_frag} ORDER BY filename",
                owner_params,
            ).fetchall()

    if not rows:
        return {"error": "No processed clips found"}

    clips = [{"id": r["id"], "filename": r["filename"], "synopsis": r["synopsis"]} for r in rows]
    suggestion = suggest_ordering(clips)

    # Store the suggestion with owner_id
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_order_suggestions (suggestion_json, owner_id) VALUES (?, ?)",
            (json.dumps(suggestion), _auth["id"]),
        )

    return {"suggestion": suggestion, "clip_count": len(clips)}


@router.post("/order/apply")
def apply_order(_auth=Depends(require_auth)):
    """Apply the most recent AI suggestion to clip positions."""
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT id, suggestion_json FROM ai_order_suggestions WHERE applied = 0 {owner_frag} ORDER BY id DESC LIMIT 1",
            owner_params,
        ).fetchone()

        if not row:
            return {"error": "No unapplied suggestion found"}

        suggestion = json.loads(row["suggestion_json"])

        for position, item in enumerate(suggestion, 1):
            clip_id = item.get("id")
            rationale = item.get("rationale", "")
            conn.execute(
                "UPDATE clips SET position = ?, ai_rationale = ?, updated_at = datetime('now') WHERE id = ?",
                (position, rationale, clip_id),
            )

        conn.execute(
            "UPDATE ai_order_suggestions SET applied = 1 WHERE id = ?",
            (row["id"],),
        )

    return {"ok": True, "applied": len(suggestion)}


@router.get("/order/history")
def order_history(_auth=Depends(require_auth)):
    """Get past AI ordering suggestions for the current user."""
    owner_frag, owner_params = _owner_where(_auth)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT id, created_at, applied FROM ai_order_suggestions WHERE 1=1 {owner_frag} ORDER BY id DESC LIMIT 10",
            owner_params,
        ).fetchall()
    return [dict(r) for r in rows]
