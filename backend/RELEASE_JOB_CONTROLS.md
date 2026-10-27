# Pause and resume lead qualification

Deploy the API, frontend and **all qualification workers** from the same revision.
Restart old workers before relying on pause: old versions do not check paused status.
No new columns or migration are required by this feature; existing dispatch,
processing-mode and admin-operations migrations remain prerequisites.

On a running job choose **Pause job** on the dashboard, job detail page or upload
progress screen. Pause is durable in SQL and survives browser/worker restarts.
It stops new lead starts and further page dispatch. Leads that already passed
the running-status check are in flight and may finish, persist results and incur
provider charges. This is a graceful pause, not cancellation of an AI request.
Already-started background embeddings may also finish. Upload/import, completed-job
chat, manual evidence refresh and exports are separate operations.

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
python -m pytest tests/test_job_controls.py tests/test_result_ordering.py tests/test_database_errors.py -q
```

Frontend: `npm run typecheck`, `npm run build`, and
`npx playwright test job-controls.spec.ts`. Browser tests exercise mocked API
responses on desktop and mobile; backend tests exercise actual SQLAlchemy models
on isolated SQLite. These do not establish live Azure/Redis deployment readiness.

Operator smoke check: start a small fresh job in a configured development
deployment, pause while leads run, observe that no new lead begins after the
pause check, allow in-flight work to finish, restart workers, then resume and
verify all remaining leads finish without duplicate scores/counters. Test with
multiple qualification workers before production rollout.
