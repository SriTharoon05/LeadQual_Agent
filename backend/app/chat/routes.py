"""Durable, independently ordered conversations scoped to an upload."""
import json
import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.chat.router import answer as run_chat
from app.database import get_db, locked_chat_session
from app.utils.db_availability import database_unavailable_reason, warn_database_unavailable
from app.models import ChatConversation, ChatMessage, Job
from app.utils.lead_identity import json_object, transaction_lock
from app.validators.evidence import citation_key, safe_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])
HISTORY_MESSAGES = 16


class ChatSelection(BaseModel):
    kind: Literal["company", "person"]
    value: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    request_id: UUID = Field(default_factory=uuid4)
    conversation_id: UUID | None = None
    selection: ChatSelection | None = None


class PersistedMessage(BaseModel):
    id: str
    request_id: str
    role: Literal["user", "assistant"]
    content: str
    sequence: int
    created_at: datetime
    tool_used: str | None = None
    sources: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    tool_used: str | None = None
    sources: list[dict] = Field(default_factory=list)
    degraded: bool = False
    clarification: dict | None = None
    context: dict = Field(default_factory=dict)
    user_message: PersistedMessage
    assistant_message: PersistedMessage


def _get_job_or_404(job_id: str, db: Session) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This upload no longer exists.")
    return job


def _message(row: ChatMessage) -> PersistedMessage:
    metadata = json_object(row.metadata_json)
    try:
        sources = json.loads(row.sources_json or "[]")
    except (ValueError, TypeError):
        sources = []
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return PersistedMessage(
        id=str(row.id), request_id=str(row.request_id), role=row.role,
        content=row.content, sequence=row.sequence, created_at=created_at,
        tool_used=row.tool_used, sources=_sources(sources), metadata=metadata)


def _sources(values) -> list[dict]:
    if not isinstance(values, list):
        return []
    result, seen = [], set()
    for source in values[:30]:
        if not isinstance(source, dict):
            continue
        url = safe_url(source.get("website_url") or source.get("url"))
        if not url or citation_key(url) in seen:
            continue
        seen.add(citation_key(url))
        result.append({"company_name": str(source.get("company_name") or "Source")[:255],
                       "website_url": url, "title": str(source.get("title") or "")[:160],
                       "excerpt": str(source.get("excerpt") or "")[:500]})
    return result


def _response(user: ChatMessage, assistant: ChatMessage) -> ChatResponse:
    saved = _message(assistant)
    return ChatResponse(
        answer=saved.content, tool_used=saved.tool_used, sources=saved.sources,
        degraded=bool(saved.metadata.get("degraded")),
        clarification=saved.metadata.get("clarification"),
        context=saved.metadata.get("context") or {},
        user_message=_message(user), assistant_message=saved)


def _complete_turn(db: Session, job_id: str, conversation_id: str, body: ChatRequest):
    _get_job_or_404(job_id, db)
    conversation = db.get(ChatConversation, conversation_id)
    if conversation and str(conversation.job_id) != job_id:
        raise HTTPException(status_code=409, detail="This conversation belongs to another upload.")
    if not conversation:
        conversation = ChatConversation(id=conversation_id, job_id=job_id, next_sequence=1)
        db.add(conversation)
        db.commit()

    request_id = str(body.request_id)
    selection = body.selection.model_dump() if body.selection else None
    payload = {"message": body.message.strip(), "selection": selection}
    existing = (db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conversation_id, ChatMessage.request_id == request_id,
    ).order_by(ChatMessage.sequence).all())
    if existing:
        user = next((row for row in existing if row.role == "user"), None)
        assistant = next((row for row in existing if row.role == "assistant"), None)
        if not user or not assistant:
            raise HTTPException(status_code=503, detail="The saved reply is incomplete. Retry shortly.", headers={"Retry-After": "2"})
        if json_object(user.metadata_json).get("request_payload") != payload:
            raise HTTPException(status_code=409, detail="This request ID was already used for a different question.")
        return _response(user, assistant)

    recent = (db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id)
              .order_by(ChatMessage.sequence.desc()).limit(HISTORY_MESSAGES).all())
    history = [{"role": row.role, "content": row.content,
                "metadata": json_object(row.metadata_json)} for row in reversed(recent)]
    if selection:
        last_assistant = next((turn for turn in reversed(history) if turn["role"] == "assistant"), None)
        pending = (last_assistant or {}).get("metadata", {}).get("clarification")
        choices = pending.get("choices", []) if isinstance(pending, dict) else []
        if (not isinstance(pending, dict) or pending.get("original_message") != payload["message"]
                or not any(isinstance(choice, dict) and choice.get("kind") == selection["kind"]
                           and choice.get("value") == selection["value"] for choice in choices)):
            raise HTTPException(status_code=409, detail="That suggestion has expired. Ask the question again.")
    db.commit()
    result = run_chat(db, job_id=job_id, message=payload["message"], history=history, selection=selection)
    if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or not result["answer"].strip():
        raise RuntimeError("Chat returned an invalid response")

    db.commit()
    transaction_lock(db, "ql:chat-save:" + conversation_id)
    _get_job_or_404(job_id, db)
    # A previous connection can commit after a client timeout. Recheck under a
    # short SQL lock before allocating sequence numbers or inserting a retry.
    saved = (db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id,
        ChatMessage.request_id == request_id).order_by(ChatMessage.sequence).limit(3).all())
    if saved:
        saved_user = next((row for row in saved if row.role == "user"), None)
        saved_assistant = next((row for row in saved if row.role == "assistant"), None)
        if not saved_user or not saved_assistant:
            raise HTTPException(503, "The saved reply is incomplete. Retry shortly.")
        if json_object(saved_user.metadata_json).get("request_payload") != payload:
            raise HTTPException(409, "This request ID was already used for a different question.")
        return _response(saved_user, saved_assistant)
    db.refresh(conversation)
    last_sequence = db.query(func.max(ChatMessage.sequence)).filter(
        ChatMessage.conversation_id == conversation_id).scalar() or 0
    conversation.next_sequence = max(conversation.next_sequence, last_sequence + 1)
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    metadata = {"degraded": bool(result.get("degraded")),
                "clarification": result.get("clarification"), "context": result.get("context") or {}}
    user = ChatMessage(
        id=str(uuid4()), job_id=job_id, conversation_id=conversation_id,
        request_id=request_id, sequence=conversation.next_sequence,
        role="user", content=payload["message"], created_at=created_at,
        metadata_json=json.dumps({"request_payload": payload}, ensure_ascii=False))
    assistant = ChatMessage(
        id=str(uuid4()), job_id=job_id, conversation_id=conversation_id,
        request_id=request_id, sequence=conversation.next_sequence + 1,
        role="assistant", content=result["answer"].strip(), created_at=created_at,
        tool_used=result.get("tool_used"), sources_json=json.dumps(_sources(result.get("sources"))),
        metadata_json=json.dumps(metadata, ensure_ascii=False, default=str))
    conversation.next_sequence += 2
    db.add_all([user, assistant])
    db.commit()
    return _response(user, assistant)


@router.post("/jobs/{job_id}/chat", response_model=ChatResponse)
def chat(job_id: UUID, body: ChatRequest):
    """A retry with the same request UUID returns the already committed turn."""
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="Please enter a question.")
    scope = str(job_id)
    conversation_id = str(body.conversation_id or job_id)
    try:
        with locked_chat_session(conversation_id) as db:
            return _complete_turn(db, scope, conversation_id, body)
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=503, detail="A reply is already being prepared. Retry in a moment.", headers={"Retry-After": "2"}) from None
    except SQLAlchemyError as exc:
        if database_unavailable_reason(exc):
            warn_database_unavailable(logger, "chat", exc)
            raise HTTPException(status_code=503, detail="The lead database is unavailable. Check database connectivity and Azure SQL firewall access, then retry this message.", headers={"Retry-After": "5"}) from None
        logger.exception("Chat database operation failed for job=%s request=%s", scope, body.request_id)
        raise HTTPException(status_code=500, detail="The assistant could not read the requested data. Please report this error.") from None
    except Exception:
        logger.exception("Chat failed for job=%s request=%s", scope, body.request_id)
        raise HTTPException(status_code=503, detail="The assistant could not complete this reply. Retry this message shortly.", headers={"Retry-After": "3"}) from None


@router.get("/jobs/{job_id}/chat/history", response_model=list[PersistedMessage])
def chat_history(job_id: UUID, conversation_id: UUID | None = Query(default=None),
                 before_sequence: int | None = Query(default=None, ge=1, le=2_147_483_647),
                 limit: int = Query(default=100, ge=2, le=200), db: Session = Depends(get_db)):
    """Sequence ordering is stable even when timestamps are identical."""
    scope = str(job_id)
    try:
        _get_job_or_404(scope, db)
        conversation = db.get(ChatConversation, str(conversation_id or job_id))
        if not conversation:
            return []
        if str(conversation.job_id) != scope:
            raise HTTPException(status_code=404, detail="Conversation not found in this upload.")
        query = db.query(ChatMessage).filter(ChatMessage.job_id == scope,
                    ChatMessage.conversation_id == conversation.id)
        if before_sequence is not None:
            query = query.filter(ChatMessage.sequence < before_sequence)
        rows = query.order_by(ChatMessage.sequence.desc()).limit(limit).all()
        return [_message(row) for row in reversed(rows)]
    except SQLAlchemyError as exc:
        db.rollback()
        if database_unavailable_reason(exc):
            warn_database_unavailable(logger, "chat history", exc)
            raise HTTPException(status_code=503, detail="Chat history is unavailable. Check database connectivity and Azure SQL firewall access, then retry.", headers={"Retry-After": "5"}) from None
        logger.exception("Chat history unavailable for job=%s", scope)
        raise HTTPException(status_code=500, detail="Chat history could not be read. Please report this error.") from None
