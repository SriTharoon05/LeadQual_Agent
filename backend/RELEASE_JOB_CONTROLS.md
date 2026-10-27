# Pause and resume lead qualification

Deploy the API, frontend and **all workers, including embedding workers**, from the same revision.
Restart old workers before relying on pause: old versions do not check paused status.
No new columns or migration are required by this feature; existing dispatch,
processing-mode and admin-operations migrations remain prerequisites.

On a running job choose **Pause job** on the dashboard, job detail page or upload
progress screen. Pause is durable in SQL and survives browser/worker restarts.
It stops new lead starts and further page dispatch. Every paid LLM/search request
and embedding batch also checks uncached SQL status before admission and usage
journaling. A status lookup failure cannot authorize a paid request. In-flight
leads can save the current response, but their next paid stage waits until resume.
Already-admitted provider requests cannot be cancelled by this control and may
finish, persist results and incur charges.

Queued website scraping/indexing and embedding deliveries are acknowledged without
doing work or scheduling retries for paused jobs. Recovery excludes paused jobs;
pending SQL checkpoints remain and are recovered after resume. Pausing between
embedding batches stops the next batch; an interrupted document may need previously
completed batches recomputed on resume because embedding checkpoints are per document,
not per batch. Completed documents are not embedded again.

The original release paused qualification starts but omitted the independent
embedding queue. Deploy the follow-up billing/ingestion gates to stop those tasks.
Upload/import, completed-job chat, manual evidence refresh and exports are separate
operations; the common paid-request gate still rejects paused job contexts.

**A paused dashboard total is not guaranteed to freeze instantly.** Usage for
requests admitted before pause can arrive later, and Redis usage persistence can
catch up after an outage. The accounting pipeline must retain these real charges.
Compare request creation times and stage/module breakdowns when investigating an
increase. Retried Redis/SQL telemetry persistence is not a new paid model request.

Choose **Resume job** to continue. The ICP, research mode, scores and counters
are retained. Resume resets the page cursor and republishes only nonterminal
leads; terminal guards and per-identity locks make duplicate deliveries safe.
Successful stage checkpoints are reused on retry even in fresh mode; completed
leads are never restarted by resume. Broker errors leave committed running intent
for normal reconciliation. A paused job whose last in-flight lead finishes remains
paused until resumed; then normal finalization completes it.

Authenticated endpoints:

| Endpoint | Allowed state | Result |
| --- | --- | --- |
| `POST /jobs/{id}/pause` | running or paused | Current JobStatusResponse with paused state |
| `POST /jobs/{id}/resume` | paused or running | Current JobStatusResponse; durable running intent |
| `GET /jobs/{id}/status` | any existing job | Current saved status and progress |

Repeated pause/resume commands are safe. Invalid transitions return 409; missing
jobs return 404; unauthenticated requests return 401. Resubmitting ICP cannot
change a paused job's policy. Paused jobs cannot be deleted because in-flight
writers may still exist; resume and let processing finish before deleting.
Dashboard Running / Queued totals exclude paused jobs; each job retains its own
Paused badge and progress. This uses the application's existing shared login
authorization model.

Validation (from backend):

```bash
python -m pytest tests/test_paused_billing.py tests/test_job_controls.py tests/test_result_ordering.py tests/test_database_errors.py -q
```

Frontend: `npm run typecheck`, `npm run build`, and
`npx playwright test job-controls.spec.ts`. Browser tests exercise mocked API
responses on desktop and mobile; backend tests exercise actual SQLAlchemy models
on isolated SQLite. These do not establish live Azure/Redis deployment readiness.

Operator smoke check: start a small fresh job in a configured development
deployment, pause while leads run, observe that no new lead begins after the
pause check, verify no new paid stages/embedding batches are admitted, allow
already-admitted requests and buffered accounting to finish, restart workers, then resume and
verify all remaining leads finish without duplicate scores/counters. Test with
multiple qualification workers before production rollout.
