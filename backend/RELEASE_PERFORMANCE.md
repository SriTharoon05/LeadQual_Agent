# Application reliability, performance and chatbot rollout

## Chatbot and browser changes

Chat now combines scoped SQL records with Qdrant passages and saved SQL website
text. Original row numbers work independently of dashboard pagination. Structured
filters support scores, contacts, reviewed state, ownership, follow-ups, evidence
freshness and indexing status. Counts, grouped summaries and numeric aggregates
come from the full SQL match set. Vector results are ranked, bounded excerpts;
they must never be represented as an exhaustive count or current verified facts.

Company/contact spelling alternatives require confirmation when ambiguous. The
assistant also asks focused questions about unclear currencies, dates and scope.
Current SQL facts take precedence over historical vector snapshots. Missing facts
remain unknown; chat cannot establish live inbox delivery or recover evidence
that was never saved. Chat reads data; workflow changes remain in their existing
dashboard controls. Response size, evidence and history pages are bounded.

The UI renders bold labels, headings, lists and horizontally scrollable tables,
with an animated three-dot thinking indicator, stable server-ordered messages,
earlier-history loading, safe retry recovery and two initially visible source
links. Chat/export browser storage is account-scoped. Stale authentication errors
cannot sign out a newly authenticated session. Login errors stay visible.

Table search is debounced, obsolete requests are cancelled, and detail drawers
discard superseded responses. Submission guards prevent rapid duplicate uploads,
ICP starts and workflow changes. Accepted upload/start requests can be reconciled
after an interrupted response. Export progress survives navigation and restores
without automatically downloading another account's file. Global render failures
offer a reload instead of a blank workspace.

Shared model calls now send policy through the Responses API `instructions`
parameter and keep imported/retrieved content in `input`, following the
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/prompt-engineering?api-mode=responses).
The chat-specific planner/composer already use separate system messages.

## Background processing and data safety

This release removes synchronous website scraping/archiving from qualification,
streams uploaded spooled files to bounded Blob blocks, and replaces HTTP-time
publication of enormous Celery chords with durable SQL intent and 250-lead pages.
The ICP cannot be changed after processing begins, including ambiguous/partial
broker failures. Imports and running qualification jobs recover after lost
publication or worker restart. A crash may replay one page; identity session
locks and persisted terminal-state checks prevent duplicate qualification work.
A Redis broker-generation marker is stored with each running job. If a broker
is emptied/replaced (including bundled localhost Redis after a container restart),
its missing marker causes bounded republishing of nonterminal rows from SQL.
An ordinary worker restart with a retained broker does not replay the whole job.
This does not detect restoration of a stale Redis snapshot retaining the old
marker but missing later messages; that is an operator recovery case. Shared
durable external Redis remains mandatory for multi-instance production operation.

Identity lookup now reads only distinct identifier projections. Reuse examines
bounded groups of prior evidence instead of loading every historical copy and
refresh history. Retry attempts retain their own newer research instead of
overwriting it with an older reusable snapshot. Import redelivery is serialized
before parsing/AI column mapping; malformed overlong input fields fail with a
readable row/column message. Source/storage and publication transients are retried.
Successful saved research, contact checks and ICP comparisons resume across worker
retries. Upstream/parse failures cannot silently become successful weak scores.
Pipeline logs commit in their own short transactions, and qualification commits
before Redis, DNS, research and outreach waits rather than retaining row locks.

Uploads accept an optional multipart UUID `request_id`. The same ID and file
content/name/type return the existing job; different content under that ID is
rejected. The browser can recover an accepted request after a timeout instead
of creating duplicate jobs. A renewable lease protects transfers without holding
a SQL connection. `source_blob_name` stays NULL until Blob confirms the file,
so background recovery cannot import an incomplete transfer. `upload_pending`
distinguishes this state in job responses; inactive interrupted transfers may be
retried with the same file/ID or deleted. Active transfers remain protected.

Table APIs accept `details=false` for compact visible lead/score/tracking data,
without full validation JSON, generated narratives or complete refresh history.
Full detail remains the default for existing callers. Row drawers load details
on demand, and one current refresh status is fetched per displayed lead.
Large job deletion uses set-based SQL operations and commits before remote
artifact cleanup; a failed SQL delete cannot first destroy its Blob/vector data.
Indexing holds a shared SQL job-artifact lock while writing Blob/vector data;
deletion requires its exclusive counterpart and returns a clear conflict while
indexing is active. This prevents a late ingestion task from recreating artifacts
after the upload is deleted. Session-lock cleanup runs even if session close fails.

Website ingestion uses a durable CompanyScrape outbox, with bounded retries,
recovery, explicit pending/failure status and generation ordering through the
same lead-identity lock as qualification/refresh. SQL transactions commit before
embedding/Qdrant network waits. A short lock wait avoids occupying background
workers for thirty seconds on a lead still being qualified. Valid prior prose
and its source date remain available while a refresh queues newer content.

The previous 4,000-character SQL truncation is removed. New saved Unicode text
uses nvarchar(max), within the configured safe scrape limit. Embedding requires
the complete chunk/vector cardinality; partial responses are not silently marked
complete. Full reuse copies available current saved prose into the new upload,
preserving its observation date and independent SQL snapshot, so deleting the
source upload does not remove that saved prose from the reused job. The new job
queues indexing of copied prose; this can use the embedding API, but does not
rerun AI qualification, scoring or web research.

Historical duplicate scrape rows remain preserved. Normal ingestion/retrieval
selects the newest row deterministically. No historical rows are deleted by the
migration. Text already lost to the old 4,000-character truncation cannot be
invented from SQL. Recovering missing historical text requires a separately
requested archive restoration/reindex, or explicit evidence refresh. Reindexing
may incur embedding API usage; research refresh additionally performs research.

## Deployment order

1. Back up Azure SQL and stop new uploads/ICP submissions/refreshes. Drain normal
   active qualification/import/refresh work, then stop every API and worker writer.
2. From the updated backend image/environment on the SQL-authorized network, run
   **this migration before any other pending ORM-based migration**:

   ```powershell
   python scripts/migrate_dispatch_reliability.py
   ```

   It adds Job upload fingerprint/publication cursor/source fields and recovery index, ingestion
   outbox fields/indexes, and Unicode full-text storage. It makes no AI, Blob or
   research calls and does not change terminal lead results. It can be rerun.
3. Apply any pending lead reuse, chat reliability, team workflow and scoring v2
   migrations. The current ORM Job model requires the dispatch columns, which
   is why dispatch migration goes first. Earlier scoring migration still refuses
   active jobs; do not bypass that guard by editing status values blindly.
4. Deploy the matching frontend, API and all worker images. The combined Docker
   Supervisor configuration isolates qualification, imports/dispatch, embeddings
   and exports. Separate Linux workers can use:

   ```sh
   celery -A app.celery_app.celery worker -Q lead_processing --pool=threads --concurrency=2 --hostname=qualification@%h
   celery -A app.celery_app.celery worker -Q lead_imports --pool=threads --concurrency=1 --hostname=imports@%h
   celery -A app.celery_app.celery worker -Q lead_embeddings --pool=threads --concurrency=1 --hostname=embeddings@%h
   celery -A app.celery_app.celery worker -Q lead_exports --pool=prefork --concurrency=1 --hostname=exports@%h
   ```

   Keep queue names synchronized with configuration. Windows development uses
   `--pool=solo`. The extra worker processes require appropriate memory/CPU/disk
   capacity; scale these services independently rather than oversubscribing one
   small App Service instance. API and every worker must share the same durable
   Redis/Blob/SQL configuration. Per-container localhost Redis is insufficient
   when more than one app instance is running.
5. Verify `/health/ready` from the permitted deployment network. Deployment must
   not route traffic before all required additive schema upgrades complete.

Production startup rejects a default/placeholder authentication secret or one
shorter than 32 UTF-8 bytes. Configure a persistent secret shared by API replicas.
The Docker Python base is pinned to Debian Bookworm to match the ODBC package
repository. Outbound website ingestion rejects private/metadata addresses, pins
validated public destinations, checks redirects and bounds time/decoded size.
`SCRAPE_MAX_HTML_BYTES` defaults to 2 MiB and `SCRAPE_MAX_TEXT_CHARS` to 250,000.

## Existing stranded jobs

The dispatch migration supports restoring durable publication intent for legacy
`running` jobs with partially published chords, and imports that were committed
before a broker failure. It recalculates active-job counters from saved lead
statuses and resumes **pending** rows after matching workers start. Already
completed/failed lead rows are skipped; the migration itself never invokes AI.

If such jobs prevent a still-pending scoring migration, use a controlled two-step
rollout: back up and stop writers, apply dispatch migration, then temporarily run
the matching workers to drain the outstanding jobs on an otherwise compatible
schema. Confirm every prior required schema migration is applied before those
workers start. Stop writers again and apply scoring v2. Do not start current
workers against a schema missing fields they use, and do not mark pending leads
completed merely to pass a migration guard. A deployment administrator must
review genuinely corrupt/orphaned/invalid legacy records before restoring work.

Pending website outbox work may resume after deployment. Completed historical
documents are not automatically scraped or reindexed by the migration.

## Verification boundary

Only static source/diff review was performed, following the standing instruction
not to run tests, builds, application imports or live Azure/SQL checks. Deployment
verification must cover broker interruption during upload/dispatch, worker restart
mid-page, duplicate delivery, concurrent identical imports, completion counters,
large Unicode documents, vector-count mismatches, saved-document reuse and
refresh/index ordering. Also verify authentication/account switching, concurrent
edits, structured chat filters and aggregate accuracy, spelling clarification,
history/retry ordering, SQL/vector outages and complete large CSV/XLSX downloads.
No measured speed, zero-defect guarantee or successful production execution is
claimed by this source review. Deploy to a staging slot first and promote only
after these checks pass in the SQL-authorized Azure environment.
