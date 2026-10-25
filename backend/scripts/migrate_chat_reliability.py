"""Add ordered, retry-safe chat history without removing existing messages.

Stop API/worker writers and back up Azure SQL before running from backend:
    python scripts/migrate_chat_reliability.py

All schema changes/backfill run in one transaction. Rerunning is safe.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.database import engine
from app.models import ChatConversation, ChatMessage, Lead


COLUMNS = {
    "conversation_id": "uniqueidentifier NULL",
    "request_id": "uniqueidentifier NULL",
    "sequence": "int NULL",
    "tool_used": "varchar(50) NULL",
    "sources_json": "nvarchar(max) NULL",
    "metadata_json": "nvarchar(max) NULL",
}


def migrate():
    if engine.dialect.name != "mssql":
        raise RuntimeError("This migration requires Azure SQL / SQL Server")
    with engine.begin() as connection:
        if connection.execute(text("""
            SELECT TOP 1 id FROM jobs WHERE status IN ('running', 'importing')
        """)).first():
            raise RuntimeError("Let active jobs finish and stop API/workers before migrating.")

        if inspect(connection).has_table("chat_message"):
            if connection.execute(text("""
                SELECT TOP 1 m.id FROM chat_message m
                LEFT JOIN jobs j ON j.id=m.job_id
                WHERE j.id IS NULL OR m.role NOT IN ('user', 'assistant')
            """)).first():
                raise RuntimeError("Invalid/orphaned chat history exists. Review it before migrating; no data changed.")
        ChatConversation.__table__.create(connection, checkfirst=True)
        ChatMessage.__table__.create(connection, checkfirst=True)

        columns = {column["name"] for column in inspect(connection).get_columns("chat_message")}
        for name, ddl in COLUMNS.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE chat_message ADD [{name}] {ddl}"))
        content_type = connection.execute(text("""
            SELECT TYPE_NAME(user_type_id), max_length FROM sys.columns
            WHERE object_id=OBJECT_ID('chat_message') AND name='content'
        """)).first()
        if not content_type or content_type[0] != "nvarchar" or content_type[1] != -1:
            connection.execute(text("ALTER TABLE chat_message ALTER COLUMN content nvarchar(max) NOT NULL"))

        # Legacy chats belong to one conversation per job (conversation UUID =
        # job UUID). Fresh browser conversations use independent UUIDs instead.
        connection.execute(text("""
            UPDATE chat_message SET conversation_id=job_id WHERE conversation_id IS NULL;
            UPDATE chat_message SET request_id=id WHERE request_id IS NULL;
        """))
        if connection.execute(text("""
            SELECT TOP 1 conversation_id FROM chat_message
            GROUP BY conversation_id HAVING COUNT(DISTINCT CONVERT(varchar(36), job_id)) > 1
        """)).first():
            raise RuntimeError("A conversation spans multiple uploads. Review it before migrating.")
        connection.execute(text("""
            INSERT INTO chat_conversation (id, job_id, next_sequence, created_at)
            SELECT m.conversation_id, m.job_id, 1, MIN(m.created_at)
            FROM chat_message m
            WHERE NOT EXISTS (SELECT 1 FROM chat_conversation c WHERE c.id=m.conversation_id)
            GROUP BY m.conversation_id, m.job_id
        """))
        if connection.execute(text("""
            SELECT TOP 1 m.id FROM chat_message m JOIN chat_conversation c ON c.id=m.conversation_id
            WHERE m.job_id <> c.job_id
        """)).first():
            raise RuntimeError("Chat history does not match its upload. Review it before migrating.")

        # Older timestamps may tie; their exact historical pairing cannot be
        # reconstructed. Apply a stable user-first tie order once, then retain it.
        connection.execute(text("""
            ;WITH pending AS (
                SELECT id, conversation_id,
                    ROW_NUMBER() OVER (PARTITION BY conversation_id
                        ORDER BY created_at, CASE WHEN role='user' THEN 0 ELSE 1 END, id) AS position
                FROM chat_message WHERE [sequence] IS NULL
            ), maxima AS (
                SELECT conversation_id, COALESCE(MAX([sequence]), 0) AS last_sequence
                FROM chat_message GROUP BY conversation_id
            )
            UPDATE m SET [sequence]=p.position+x.last_sequence
            FROM chat_message m JOIN pending p ON p.id=m.id
            JOIN maxima x ON x.conversation_id=p.conversation_id
        """))
        connection.execute(text("""
            UPDATE c SET next_sequence=x.next_sequence
            FROM chat_conversation c
            JOIN (SELECT conversation_id, MAX([sequence])+1 AS next_sequence
                  FROM chat_message GROUP BY conversation_id) x ON x.conversation_id=c.id
            WHERE c.next_sequence < x.next_sequence
        """))

        column_details = {column["name"]: column for column in inspect(connection).get_columns("chat_message")}
        for name, ddl in (("conversation_id", "uniqueidentifier"),
                          ("request_id", "uniqueidentifier"), ("sequence", "int")):
            if column_details[name]["nullable"]:
                connection.execute(text(f"ALTER TABLE chat_message ALTER COLUMN [{name}] {ddl} NOT NULL"))
        fks = inspect(connection).get_foreign_keys("chat_message")
        if not any(fk["constrained_columns"] == ["conversation_id"] for fk in fks):
            connection.execute(text("""
                ALTER TABLE chat_message ADD CONSTRAINT fk_chat_message_conversation
                FOREIGN KEY (conversation_id) REFERENCES chat_conversation(id)
            """))
        for name, columns in (("uq_chat_conversation_sequence", "conversation_id, [sequence]"),
                              ("uq_chat_request_role", "conversation_id, request_id, role")):
            exists = connection.execute(text("""
                SELECT is_unique FROM sys.indexes WHERE object_id=OBJECT_ID('chat_message') AND name=:name
            """), {"name": name}).first()
            if exists and not exists[0]:
                raise RuntimeError(f"Index {name} is not unique. Review it before migrating.")
            if exists is None:
                connection.execute(text(f"CREATE UNIQUE INDEX {name} ON chat_message ({columns})"))
        for table in (ChatConversation.__table__, ChatMessage.__table__):
            for index in table.indexes:
                index.create(connection, checkfirst=True)
        lead_index = next(
            index for index in Lead.__table__.indexes
            if index.name == "ix_leads_job_row"
        )

        existing = connection.execute(text("""
            SELECT 1
            FROM sys.indexes
            WHERE object_id = OBJECT_ID('leads')
            AND LOWER(name) = LOWER(:name)
        """), {"name": lead_index.name}).first()

        if existing is None:
            lead_index.create(connection)
    print("Chat migration complete. Deploy the updated API and frontend together.")


if __name__ == "__main__":
    migrate()
