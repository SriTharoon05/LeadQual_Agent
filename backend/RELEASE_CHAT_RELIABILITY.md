# Chat reliability release

The chatbot queries all leads in the selected upload directly from SQL. The
table's current page never limits its search. Row numbers are the one-based
stored record numbers after import deduplication: “row 500” targets
`row_index = 499`, rather than a physical Excel row including its header.

Exact company/contact names, email addresses, lead IDs, row numbers, filters,
counts, ordering, and follow-up questions use saved job data. A partial or
misspelled name prompts a choice before using a suggested record. Repeated
contact names include the company and row number in that choice. Saved research
and ICP evidence provide context and source links; unclear information remains
unclear. This release does not refresh research or verify employment again.

Chat retrieval no longer depends on embeddings or Qdrant availability. Common
data questions can be answered without a model. More complex interpretation and
narrative answers use bounded model calls and fall back to saved facts if the
model is unavailable. A database failure returns a retryable error rather than
pretending the upload has no matching records.

Each browser tab stores a conversation UUID per upload in session storage.
Refreshing retains that conversation; opening another tab can create an
independent one. This is still the application's shared authenticated workspace,
not a new authorization boundary. Messages have stable IDs and increasing
sequence numbers. The browser merges saved and provisional messages by request
ID, so refreshes cannot display duplicate user/reply pairs. It only follows new
messages automatically while the user is near the bottom of the chat.

Each question also has a request UUID. Retrying an uncertain network failure
reuses it; the server returns an already committed reply instead of generating
another. Both messages commit atomically before the response is sent. SQL Server
session application locks serialize requests within one conversation across API
workers. Requests competing with an active turn receive HTTP 503 with
`Retry-After`; invalid or expired choices can be edited and asked again.

## Required deployment step

Back up Azure SQL and stop API/worker writers. From `backend`, with the target
environment configured, run:

```powershell
python scripts/migrate_chat_reliability.py
```

For deployments that have not applied the earlier lead reuse migration, apply
`scripts/migrate_lead_reuse.py` first. The chat migration creates conversations,
adds message ordering/request/source metadata, backfills existing messages, and
adds foreign keys, unique indexes, and a job/row lookup index in one transaction.
It is safe to rerun.
Invalid or orphaned history is reported for review rather than deleted.
Message content and new JSON metadata use Unicode storage. The migration widens
existing chat content to `nvarchar(max)` using the
[documented SQL Server conversion](https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-table-transact-sql#alter-column).

Older history remains in a legacy conversation whose UUID equals the job UUID;
the history endpoint accepts that UUID. New browser conversations start fresh.
Older messages with identical timestamps did not record their original order,
so their tie order can only be made deterministic; new messages record the exact
turn order. Old sources were not stored and cannot be restored retroactively.

Deploy the updated API and frontend together after migration. `/health/ready`
rejects missing chat columns, incomplete backfill, or missing unique indexes.
Both existing Docker images include the migration script. For the combined
image, run the script with its entrypoint overridden so writers stay stopped:

```powershell
docker run --rm --env-file backend/.env --entrypoint python qualitylead scripts/migrate_chat_reliability.py
```

A chat uses one dedicated SQL connection until its response is persisted;
read transactions finish before model calls. Allow pool capacity for concurrent
chat users, workers, and regular API traffic. The client allows up to 120 seconds
for a chat response. Proxy/gateway timeouts must allow that request duration.

Rolling back to the previous application is possible with the additive schema
retained, but its chat writer cannot insert the new required message fields.
Disable chat writes during rollback or restore the backed-up database alongside
the previous application. Do not mix old and new API replicas during rollout.

## Verification in your environment

Only static source and diff review was performed for this change, as requested.
No tests, builds, model calls, or Azure SQL connections were run locally.
Validate the migration on a restored database before routing traffic, then check:

- Ask about row 500 while the results table displays rows 1–50.
- Ask for a company with a typo, select the suggested company, and ask a follow-up.
- Ask about a repeated contact name and select the intended row.
- Request counts, filters, highest scores, notes, contact information, and evidence.
- Refresh after a response and confirm the user/reply order and sources remain.
- Interrupt a request and retry it; confirm there is only one saved reply.
- Switch uploads during generation and confirm neither chat displays the other's response.
- Scroll up while a reply arrives and confirm the view stays where you were reading.
- Exercise model/database failures and verify the fallback/retry behavior.
- Delete an upload that has chats and confirm its history is removed with it.
