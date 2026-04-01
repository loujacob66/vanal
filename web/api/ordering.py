import json

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from vanal import db
from vanal.vision import suggest_ordering
from web.api.auth import require_auth

router = APIRouter()


class SuggestRequest(BaseModel):
    clip_ids: list[int] = []  # empty = use all done clips


@router.post("/order/ai-suggest")
def ai_suggest_order(req: SuggestRequest = Body(default_factory=SuggestRequest), _auth=Depends(require_auth)):
    """Ask the LLM to suggest a narrative ordering.

    If clip_ids is provided, only those clips are ordered (partial ordering).
    Otherwise all done clips are used.
    """
    with db.get_conn() as conn:
        if req.clip_ids:
            placeholders = ",".join("?" * len(req.clip_ids))
            rows = conn.execute(
                f"SELECT id, filename, synopsis FROM clips "
                f"WHERE id IN ({placeholders}) AND status = 'done' ORDER BY filename",
                req.clip_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, filename, synopsis FROM clips WHERE status = 'done' ORDER BY filename"
            ).fetchall()

    if not rows:
        return {"error": "No processed clips found"}

    clips = [{"id": r["id"], "filename": r["filename"], "synopsis": r["synopsis"]} for r in rows]
    suggestion = suggest_ordering(clips)

    # Store the suggestion
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO ai_order_suggestions (suggestion_json) VALUES (?)",
            (json.dumps(suggestion),),
        )

    return {"suggestion": suggestion, "clip_count": len(clips)}


@router.post("/order/apply")
def apply_order(_auth=Depends(require_auth)):
    """Apply the most recent AI suggestion to clip positions."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, suggestion_json FROM ai_order_suggestions WHERE applied = 0 ORDER BY id DESC LIMIT 1"
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
def order_history():
    """Get past AI ordering suggestions."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, applied FROM ai_order_suggestions ORDER BY id DESC LIMIT 10"
        ).fetchall()
    return [dict(r) for r in rows]
