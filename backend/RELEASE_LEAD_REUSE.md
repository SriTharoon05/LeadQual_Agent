# Lead reuse, tracking, and ICP evidence release

## Behavior

Uploads are parsed and deduplicated, then checked against indexed database
identities before qualification starts. Every upload retains its own lead rows
and result snapshots; repeated leads share a manual tracking record.

| Situation | Behavior |
| --- | --- |
| Same lead inputs and same ICP | Copy the completed score and validation snapshots; no qualification, search, contact checks, scraping, or embedding calls |
| Same person/company/role but changed ICP or qualification inputs | Reuse available structured research; rerun contact checks, ICP comparison, and scoring |
| Changed company, role, website, or personal profile | Research again to avoid reusing employment evidence from the wrong context |
| Failed/incomplete previous processing | Process normally; do not reuse failed stage output |
| Every lead already processed | Finish the new job normally, including progress, results, and exports |
| Older result has no fact-level evidence | Preserve its summary and show “Evidence unavailable for this earlier result” |

For example, 300 uploaded people overlapping 200 of 500 previously processed
people produce 200 reused results and 100 new results, provided qualification
inputs and ICP match. File parsing/column mapping can still incur its existing
cost; full reuse skips the **lead qualification** calls.

Identity matching uses normalized CRM contact IDs, email addresses, personal
LinkedIn URLs, and a complete normalized name/company pair. Empty names do not
merge everyone at a company. Conflicting strong identifiers and ambiguous
matches stay separate. Matching is conservative: changed identifiers may
require new processing. This remains the existing shared-admin workspace; this
release does not add tenant isolation.

Reviewed state and tracking notes are shared across uploads. Imported Notes
remain separate. Tracking edits require a version and return HTTP 409 for stale
edits; the browser retains the user's draft. Deleting one job preserves tracking
used by another job. Tracking with no remaining lead references is removed.

## Evidence and qualification

Research runs before ICP validation. Company size, company location, annual
revenue, industry, current role/seniority, and employment have separate findings,
statuses, explanations, and source links. The ICP validator compares those facts
with the submitted requirements. Unknown/conflicting required facts cannot become
verified matches. Employment distinguishes current, former, and unclear;
absence of evidence does not establish departure.

Fact-level citations require a supporting excerpt and an HTTP(S) URL returned
by the actual web-search call. A URL supplied only by the model or upload is not
accepted as proof. This validates attribution; it does not independently verify
every quotation or guarantee that a public source is correct/current. Inspect
the linked evidence when deciding whether to act on a lead. Search access can
be limited, including private LinkedIn profiles and private-company revenue.

Reused evidence retains its original UTC check date; there is no automatic
expiration or silent paid refresh of completed results. This fulfills reuse while
data remains in the database. Older employment/company evidence may be stale.
There is no manual refresh control in this release.

The lead detail panel displays ICP input, proof cards, globe links, and the
qualification summary. Excel/CSV exports include tracking and evidence. Formula
prefixes in imported values/notes are escaped in exports. The chatbot receives
saved descriptions and proof data through job-scoped SQL retrieval, so it can
answer about reused leads without creating new website embeddings. Existing
semantic website retrieval still supplements newly processed leads.

## Database migration and rollout

The migration is additive and restartable. **Back up Azure SQL, let active jobs
finish, and stop API/worker writers before applying it.** Do not mix old and new
workers during the rollout: old workers do not honor the new reuse locks.

From the `backend` directory, with the intended environment configured:

```powershell
python scripts/migrate_lead_reuse.py
```

The script:

1. Rejects active jobs and duplicate existing score/stage rows before schema changes.
2. Creates `lead_tracking`, identity/provenance fields, and reuse counters.
3. Adds lookup indexes and unique score/stage indexes.
4. Backfills existing lead identities and input fingerprints in committed batches.

If duplicate score/stage rows are reported, review those records before retrying.
The script never chooses or deletes historical duplicate results automatically.
If backfill is interrupted, rerun the script while writers remain stopped.
`/health/ready` rejects a missing migration or unfinished identity backfill.

Both Docker images include `scripts/`. For a migration-only run of the combined
image, override its entrypoint so the API and workers do not start first:

```powershell
docker run --rm --env-file backend/.env --entrypoint python qualitylead scripts/migrate_lead_reuse.py
```

After migration, deploy the updated API, all Celery workers, and frontend together.
Use a strong `AUTH_SECRET_KEY`, `APP_ENV=production`, correct trusted hosts/CORS,
and durable Redis for queued production jobs. `LLM_TIMEOUT_SECONDS` defaults to
120 seconds per request; the SDK makes at most one internal retry, and failed
qualification tasks have a separate bounded error retry budget.

Workers use SQL Server `sp_getapplock`: a short transaction lock for import
identity assignment and a session lock per shared lead identity during processing.
The session lock survives stage commits and releases on worker disconnect.
Lock contention retries without consuming the qualification-error retry budget.
Configure enough SQL connections for workers plus token logging/embedding work.

For rollback, stop the updated writers and restore the previous application
image; additive database fields can remain. Do not drop shared tracking or
provenance fields containing new user data. Returning to old workers also
returns to the old duplicate-processing behavior.

## Verification

From the repository root:

```powershell
python -m pip install -r backend/requirements-dev.txt
python -m pytest backend/tests -q
```

From `frontend`:

```powershell
npm ci
npm run typecheck
npm run lint
npx playwright install chromium
npm run test:e2e
npm run build
```

Browser tests run the built frontend against an isolated mock API, with desktop
and mobile viewports. They do not require production credentials or make paid
AI calls. The final normal build removes the browser-test API URL from the local
build output. Database tests use SQLite and exercise real ORM persistence,
identity batches, snapshots, tracking APIs, exports, retries, and completion.
They do **not** establish SQL Server lock behavior, migration execution, actual
Azure model quality, or live Redis/Celery delivery in the deployment environment.

Before routing production traffic, validate in an Azure SQL staging environment:

1. Run migration on a restored copy and verify `/health/ready` succeeds.
2. Re-upload a completed file; verify results and counters, with no new
   qualification-stage LLM calls for fully reused rows.
3. Upload an overlapping file and confirm only new rows incur qualification calls.
4. Submit overlapping jobs concurrently; verify one worker performs research per
   identity/context and both jobs finish correctly.
5. Change ICP, verify research reuse and a new qualification verdict.
6. Save tracking, refresh, open the same person from another job, and export.
7. Inspect real source pages for company size/location/revenue and current
   employment. Confirm unknowns stay unclear and reused check dates stay original.
8. Exercise broker/model failures and worker restart; verify terminal counters,
   retry behavior, and preserved result snapshots.

No production migration or deployment is performed by the local test commands.
