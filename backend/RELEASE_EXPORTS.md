# Reliable CSV and Excel exports

Exports now prepare in a background worker instead of holding an HTTP request
open while SQL rows, worksheet cells and complete byte buffers accumulate.
CSV writes directly to disk; Excel uses XlsxWriter's constant-memory mode.
SQL reads use 250-row keyset pages by default and do not load full refresh
histories or score audit snapshots. Only the chosen format is generated.

Every saved row in the completed upload is included, regardless of the current
table page, search, sort or priority filter. Stable source row numbers, lead IDs,
processing status, all existing exported contact/company fields, scores,
proofs, outreach, reviewed checkboxes and notes are included. Rows retain source
order. Failed lead rows in a completed upload retain their saved fields and
blank unavailable scores; they are not silently dropped.

Excel keeps formatted headers, priority colours, filters and frozen headers.
Data beyond a worksheet's row limit goes to another Leads sheet. Text beyond
Excel's cell limit goes to numbered LongText sheets; the main cell identifies
the continuation and its first row. Join the numbered parts to recover the full
text. CSV retains complete long fields, with UTF-8 BOM, quoted multiline values
and formula injection protection. Unsupported XML control characters are removed.
Scores are numeric Excel cells, so spreadsheet sorting and calculations work.
Malformed legacy evidence is preserved as raw saved content and marked in an
Export Warnings column instead of dropping that lead or failing the whole file.

## Downloads and recovery

The dashboard and job page share an export tray with row progress, readable
errors, retry and a Download file link. Preparation continues when users switch
pages. Tab reloads resume pending request IDs from session storage; ready files
do not download again merely because the page reloads. Only three preparation
polls run at a time in one browser tab.

Files live in private Azure Blob storage. The browser uses its native downloader
through the API; it does not allocate a full JavaScript Blob. A separate signed
ticket permits only one job/export path for 15 minutes and cannot authenticate
other API requests. Fresh tickets are obtained through the authenticated status
endpoint. File responses support byte ranges and include the exact byte length.
No Blob CORS/public access changes are necessary, including for private-endpoint
storage reachable only by the API/worker. Transfers still depend on the browser,
network, storage and hosting provider; unlimited transfer speed is not promised.

Requests for the same snapshot/format reuse ready files for ten minutes by
default. Changes to tracked notes/review,
refreshed results and refresh activity change the cache revision. Pending builds
for the same job/format are shared even if someone edits the data meanwhile.
The worker checks row counts and revisions before publishing and retries a
changed dataset, rather than offering an incomplete or mixed export. Time-based
freshness fields are evaluated at the displayed snapshot time.
Download again retrieves that prepared snapshot; clicking the table's export
button requests the latest saved revision.

Workers use renewable leases with fencing, attempt-specific Blob names,
bounded retries and heartbeat-based availability. Polling can republish a request
after a lost publish/crashed worker. Redis request metadata expires after 24 hours;
an expired request can be recreated from SQL without reprocessing leads or AI.

## Required production deployment

1. Rebuild and deploy the matching frontend, API and workers together. Install
   updated backend requirements, which now include XlsxWriter. There is **no
   additional SQL migration for exports**; earlier release migrations, including
   scoring v2, are still prerequisites for the full application.
2. Start a worker consuming `lead_exports`. The root Docker image's updated
   Supervisor configuration starts a separate export worker automatically, with
   one prefork process so bulk workbook work does not occupy qualification slots.
   For separately deployed services, run from `backend` on Linux:

   ```sh
   celery -A app.celery_app.celery worker --loglevel=info -Q lead_exports --pool=prefork --concurrency=1 --hostname=exports@%h
   ```

   Windows local development uses `--pool=solo`. Existing workers for
   `lead_processing,lead_imports,lead_embeddings` remain required. If the export
   queue is renamed, set the same `CELERY_EXPORT_QUEUE` on API and workers and
   update separately managed worker start commands.
3. API and workers must use the **same Redis instance**, Blob account/container
   and `AUTH_SECRET_KEY`. With multiple Azure app instances, configure shared
   Redis through `REDIS_URL`; per-container localhost Redis is insufficient for
   cross-instance polling and queues. Use durable Redis for queued work. SQL,
   Redis and Blob must be reachable from the deployment network.
4. Deploy the updated root `nginx.conf`. Download navigations must bypass SPA
   rewrites. The download location disables buffering/gzip and suppresses access
   logging of signed query strings. Separately managed proxies must implement the
   same behaviour. The API also redacts these tickets in Uvicorn access logs.
   Do not record download query strings in external gateway/access analytics.
5. Provide writable temporary disk for one workbook's XML/output files per
   concurrent export, not just its compressed XLSX size. Start with one export
   worker process and scale separately based on production memory/disk capacity.
6. Configure an Azure Storage lifecycle rule to delete generated blobs under
   `<container>/exports/` after two days (or longer than the 24-hour request
   retention). Source uploads and scraped evidence use different prefixes and
   must not be included in that rule. Removing a job still removes its exports.

Optional settings, with defaults:

```dotenv
CELERY_EXPORT_QUEUE=lead_exports
EXPORT_BATCH_SIZE=250
EXPORT_CACHE_SECONDS=600
EXPORT_TIMEOUT_SECONDS=1800
```

Larger batches trade memory for fewer SQL round trips. Do not increase them
without checking actual evidence sizes and memory headroom. Maximum preparation
time includes queue wait; adjust the timeout for unusually large uploads or
provision additional export worker capacity.

## API contract

- `POST /jobs/{job_id}/exports?format=excel|csv`: authenticated; returns a request
  ID and state immediately (202 preparing, 200 already ready).
- `GET /jobs/{job_id}/exports/{export_id}`: authenticated status/progress; returns
  a fresh `download_path` when ready. Failures are explicit, not HTML/empty files.
- `GET /jobs/{job_id}/exports/{export_id}/file?ticket=...`: file-scoped signed
  download; supports a single byte range for resumable transfers.
- Legacy `/jobs/{job_id}/export/excel|csv` URLs return asynchronous state while
  preparing, then redirect to the ready file. External clients must poll rather
  than save a 202 JSON response as a spreadsheet.

## Verification boundary

This release was reviewed statically only, respecting the request not to run
tests, builds, application imports or live Azure/SQL connections. On the allowed
deployment network, verify both formats on small and large completed uploads,
row counts against SQL, long/Unicode/multiline notes, repeat clicks, tracking
changes, page reload/navigation, worker restarts, expired links, and byte-range
downloads. No measured export speed or successful production execution is claimed.

Implementation references: [XlsxWriter memory mode](https://xlsxwriter.readthedocs.io/working_with_memory.html),
[Excel worksheet/cell limits](https://xlsxwriter.readthedocs.io/worksheet.html#worksheet-write-string),
[SQLAlchemy pool isolation after fork](https://docs.sqlalchemy.org/en/20/core/pooling.html#using-connection-pools-with-multiprocessing-or-os-fork),
and [Azure Blob lifecycle configuration](https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-policy-configure).
