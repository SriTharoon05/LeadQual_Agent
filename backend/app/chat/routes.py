"""
chat/routes.py
──────────────
Session-scoped chatbot endpoints. Mount in app/main.py:

    from app.chat.routes import router as chat_router
    app.include_router(chat_router)

Endpoints:
    POST /jobs/{job_id}/chat           Ask a question about this session
    GET  /jobs/{job_id}/chat/history   Retrieve conversation history
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Job, ChatMessage          # ← consolidated in models.py
from app.chat.router import answer as run_chat

router = APIRouter(tags=["chat"])

# Keep short-term memory small (last N turns) to control token cost.
HISTORY_TURNS = 8


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    answer:    str
    tool_used: str | None = None
    sources:   list[dict] = Field(default_factory=list)


def _get_job_or_404(job_id: str, db: Session) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


@router.post("/jobs/{job_id}/chat", response_model=ChatResponse)
def chat(job_id: str, body: ChatRequest, db: Session = Depends(get_db)):
    """Ask a question about THIS session. Router picks SQL vs semantic search."""
    _get_job_or_404(job_id, db)
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message cannot be empty.")

    # Load short conversation memory for this session (oldest → newest).
    recent = (
        db.query(ChatMessage)
        .filter(ChatMessage.job_id == job_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(HISTORY_TURNS * 2)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(recent)]

    # Run the two-tool router.
    result = run_chat(db, job_id=job_id, message=body.message, history=history)

    # Persist both turns.
    db.add(ChatMessage(job_id=job_id, role="user", content=body.message))
    db.add(ChatMessage(job_id=job_id, role="assistant", content=result["answer"]))
    db.flush()

    return ChatResponse(**result)


@router.get("/jobs/{job_id}/chat/history")
def chat_history(job_id: str, db: Session = Depends(get_db)):
    """Full conversation history for a session, oldest first."""
    _get_job_or_404(job_id, db)
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.job_id == job_id)
        .order_by(ChatMessage.created_at)
        .all()
    )
    return [
        {"role": m.role, "content": m.content, "created_at": m.created_at}
        for m in msgs
    ]
