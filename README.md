# Quality Lead — end-to-end technical explanation

## Current guides and job controls

- [Costing Guide (Word)](docs/Quality_Lead_Costing_Guide.docx): reference tariffs,
  formulas, batch estimates, reuse, infrastructure and accounting limitations.
- [Scoring Guide (Word)](docs/Quality_Lead_Scoring_Guide.docx): current 60% ICP /
  25% contact / 15% growth policy, proof rules, score caps and worked examples.
- [Pause/resume rollout](backend/RELEASE_JOB_CONTROLS.md): deployment and API details.

Running qualification jobs have **Pause job** and **Resume job** controls on the
dashboard, job detail and upload progress pages. Pause stops new lead starts;
in-flight leads finish safely. Resume preserves results and continues unfinished
leads. Deploy matching API/frontend/workers and restart old workers before use.
The Word scoring guide describes the current policy; the historical five-factor
architecture explanation below predates scoring v2.

**Current release:** follow [the admin operations guide](backend/RELEASE_ADMIN_OPERATIONS.md)
for SQL-backed concurrency, warning/error logs and detailed usage costs. Team
profiles, assignments, follow-up reminders, saved groups and the lead workspace
have been removed. Earlier architecture/scoring descriptions below are historical;
the scoring v2 and subsequent release guides describe the current behavior.

## What the system does

Quality Lead turns CRM-style CSV/Excel batches into researched and ranked B2B leads. A user uploads a file, describes the Ideal Customer Profile (ICP), and receives validated contact data, web-backed company intelligence, a deterministic weighted score, priority, explanations, growth signals, and actionable Outreach Intelligence.

The browser never performs AI work. It submits commands and polls job state while Celery workers continue asynchronously. This is why users can select **Go to Dashboard & Wait** without interrupting processing.

## Runtime architecture

```text
React/Vite browser
   │ HTTPS + signed bearer token
   ▼
FastAPI API ───────────────► Azure SQL (jobs, leads, scores, logs, chat)
   │                         Azure Blob (source files and scrape archives)
   │ enqueue
   ▼
Redis broker/cache ────────► Celery workers
                              ├─ Azure OpenAI Responses + web search
                              ├─ DNS/phone validation
                              ├─ Qdrant embeddings
                              └─ writes progress/results to Azure SQL
```

The API and workers use the same backend image but different start commands. Queue separation prevents large imports or embeddings from starving qualification work:

- `lead_imports`: file parsing, mapping, deduplication, bulk insert
- `lead_processing`: ICP, web validation, contact verification, scoring
- `lead_embeddings`: background Qdrant ingestion

## Request and processing flow

### 1. Authentication

`POST /auth/login` loads an `admin_users` row and verifies its bcrypt hash. In development only, legacy plaintext rows still work so they can be migrated. The API issues an HS256 token with issuer, subject, issued-at, and expiration claims. `AuthenticationMiddleware` validates that token on every route except health, login, and OpenAPI documentation. A browser 401 clears the local session and returns to login.

### 2. Upload and import

`POST /upload`:

1. Allows only `.csv`, `.xlsx`, `.xls`, or `.xlsm`.
2. Reads at most `MAX_UPLOAD_SIZE_MB + 1 byte`; oversized requests receive HTTP 413.
3. Creates a `jobs` row with `status=importing`.
4. Stores the original bytes at `uploads/{job_id}/source.{ext}` in Blob Storage.
5. Commits the job, then dispatches `import_file` to `lead_imports`.
6. Returns HTTP 202 immediately.

The import worker downloads the Blob, detects CSV encoding/delimiter or reads the first Excel sheet, rejects empty/over-limit data, maps arbitrary source columns into the 29 canonical columns, and deduplicates in this order:

1. exact CRM Contact ID;
2. case-insensitive email;
3. exact contact-name/company match after case, whitespace, and punctuation normalization.

The normalized identity pass is linear-time and deliberately conservative: ambiguous fuzzy matching can silently delete distinct people with similar names. Leads are inserted in 2,000-row chunks, counts are saved, and the job becomes `queued` (ready for an ICP). Failures become `import_failed` with a user-visible error.

### 3. ICP submission and dispatch

`POST /jobs/{job_id}/icp` accepts only a `queued` job and a non-empty prompt. It commits `status=running` before publishing tasks, preventing workers from racing an uncommitted transaction. A Celery chord publishes one independently retryable `process_lead` task per lead and schedules `finalise_job` after the header completes.

### 4. Per-lead pipeline

Each `process_lead` performs these guarded stages:

1. **ICP validation** — Azure OpenAI evaluates only industry, employee count, revenue, geography, seniority, and presence of a valid contact method. It returns an ICP score (0–10), authority score (0–10), matches, mismatches, and hard disqualifiers. A hard disqualifier forces `icp_match=false` and caps the ICP score below the threshold.
2. **Web research** — one combined Responses API request uses web search to verify the company/person, industry and headcount; find technologies and recent dated growth events; describe the company; and return source URLs. Results are cached for 24 hours using a hash of company, website, person, and title. Including person/title prevents one employee’s verification from leaking to another.
3. **Enrichment** — verified company description and technology data populate the canonical lead fields.
4. **RAG ingestion** — website/company text is archived in Blob and SQL, chunked, embedded with the configured Azure OpenAI embedding deployment, and upserted into Qdrant. This is best-effort and isolated from core scoring.
5. **Contact verification** — email syntax and MX are checked with hard DNS timeouts; optional SMTP is disabled by default because cloud port 25 is commonly blocked. Phone numbers are parsed/validated and employment evidence comes from web research. When SMTP is disabled, its points are redistributed rather than unfairly lowering every Azure-hosted lead.
6. **Scoring and Outreach Intelligence** — the model proposes component scores and narrative fields. The backend ignores model arithmetic and calculates the final score itself. The same response produces a recommended channel, evidence-grounded angle, opening message, talking points, and objection watchout. If the model call fails, deterministic scoring and a safe generic outreach plan are still saved.
7. **Transactional completion** — score persistence, lead completion state, job progress, and failure count update in one database transaction. Retries cannot double-increment progress.

Validation stages upsert `(lead_id, stage)`, so retries update rather than duplicate rows. The database migration adds unique indexes that enforce this invariant.

### 5. Exact scoring formula

All component values displayed in the UI are 0–100. The final score is:

```text
Final = ICP × 0.30
      + Authority × 0.25
      + Data accuracy × 0.25
      + Growth × 0.15
      + Contact quality × 0.05
```

- ICP comes from the six allowed ICP criteria.
- Authority is the role/seniority decision-making score.
- Data accuracy reflects company/person/industry/headcount web verification.
- Growth reflects credible recent growth evidence.
- Contact quality reflects email, phone, and employment verification.

Priority tiers are High 80–100, Medium 50–79, and Low 0–49. A lead with any ICP hard disqualifier is always Low regardless of its weighted total.

### 6. Finalization, results, and dashboard

The chord callback rolls token usage into the job, records model/deployment names, sets completion time, and marks the job complete (or failed if nothing finished). The UI polls `/jobs/{id}/status` only while active.

`GET /jobs/{id}/results` is server paginated and supports global search, priority filtering, and server-side sorting. It eager-loads scores and validation rows, avoiding N+1 SQL queries. The browser requests 50 rows and prefetches the next two pages.

`GET /dashboard/stats` computes KPIs in SQL. It does not download every historical lead. `GET /jobs` returns only the 100 most recent job summaries for the lobby.

### 7. Lead detail, export, and chat

The lead drawer displays source links, the five score dimensions, AI reasoning, Growth Signals, then Outreach Intelligence. JSON outreach data is parsed into clear actions; malformed legacy text still renders safely.

If a browser closes after import but before the ICP is submitted, opening the queued job from the dashboard presents a resume form. This prevents imported jobs from becoming stranded.

Excel and CSV exports include canonical data, validation, scoring, growth, Outreach Intelligence, and sources. Relationships are eager-loaded to keep export query counts bounded.

The completed-job chat router uses recent job-scoped conversation history and selects SQL aggregation or Qdrant semantic search. Chat records remain isolated by `job_id`.

## Important files

- `quality_lead_agent/app/main.py`: HTTP contract, CORS, health, upload, jobs, results, exports
- `quality_lead_agent/app/auth.py`: token and password security
- `quality_lead_agent/app/tasks/import_task.py`: asynchronous ingest
- `quality_lead_agent/app/tasks/tasks.py`: per-lead pipeline and finalizer
- `quality_lead_agent/app/validators/*`: ICP, web, and contact validation
- `quality_lead_agent/app/scoring/scoring_engine.py`: formula and outreach generation
- `quality_lead_agent/app/vectorstore/*`: embedding and Qdrant storage
- `quality_lead_agent/app/chat/*`: job-scoped analytics/RAG chat
- `Quality_Lead_Frontend/src/api/endpoints.ts`: typed frontend/backend integration
- `Quality_Lead_Frontend/src/pages/UploadPage.tsx`: upload, ICP, progress, dashboard-lobby action
- `Quality_Lead_Frontend/src/pages/JobDetailPage.tsx`: paginated results
- `Quality_Lead_Frontend/src/components/LeadDetailDrawer.tsx`: reasoning, growth, and outreach UI

## Operational invariants

- Run the SQL migration before the new application image.
- Keep at least one worker listening to all three queues; scale `lead_processing` workers first for large batches.
- Keep API, worker, and migration versions synchronized.
- `EMBED_DIM` must exactly match the embedding deployment and Qdrant collection vector size.
- Changing scoring weights changes business ranking and should be versioned and regression-tested.
- Web-derived statements are evidence, not guaranteed truth; source links remain visible for review.
