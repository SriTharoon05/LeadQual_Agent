# Admin operations rollout

This release adds `/admin/settings`, `/admin/cost` and `/admin/logs` to the
authenticated application. All existing application login accounts come from
`admin_users`; these pages use the same verified authentication. Removed team
profiles were assignment records and never granted login access.

## Deploy and migrate

1. Back up Azure SQL. Let active work finish, stop the API and **all** workers,
   and deploy matching frontend, API, workers and Nginx configuration. Do not
   mix old and new qualification workers: old workers do not honor the SQL limit.
2. Use the new backend dependencies and the same configured Azure SQL connection
   from an allowed network. Apply earlier migrations if they are not already
   installed: lead reuse, chat reliability, team workflow/evidence refresh,
   scoring v2 and performance. Their release guides retain the commands/order.
   The historical team workflow migration remains a prerequisite for refresh
   storage; it does not restore the removed UI or endpoints.
3. From `backend`, run:

   ```powershell
   python scripts/migrate_admin_operations.py
   ```

   In the root Docker image the working directory is `/app`, so the same command
   runs there. Execute it once with all writers stopped, before restarting them.
   The command is restartable: it adds settings/events and usage columns/indexes,
   widens aggregate job token counters to bigint, seeds concurrency **5**, and
   copies only historical WARNING/ERROR records. It does not call AI, scrape,
   check DNS, change scores/priorities or rerun lead processing. For a large log
   history, `--skip-existing-warnings` adds schema first; rerun without that flag
   during a later maintenance window to copy older warnings idempotently.
4. Configure the **same durable Redis service** in `REDIS_URL` for every API and
   worker instance. Require Redis Streams support, persistence, adequate memory
   and a **noeviction** policy. Do not clear Redis or trim the telemetry stream:
   accepted usage awaiting SQL persistence must survive worker restarts.
   Shared external Redis is required for multiple replicas. The bundled local
   Redis now enables AOF every second, but `/var/lib/redis` needs a persistent
   volume for container replacement; every-second persistence still has a
   short host-failure loss window. Choose stronger persistence/HA guarantees
   where that window is unacceptable. Do not change broker/database during a job.
5. Start the API and the separate queue workers. The root Supervisor image
   already uses these capacities. For separate processes, using the default
   queue names:

   ```powershell
   celery -A app.celery_app.celery worker --loglevel=info -Q lead_processing --pool=threads --concurrency=30 --hostname=qualification@%h
   celery -A app.celery_app.celery worker --loglevel=info -Q lead_imports --pool=threads --concurrency=1 --hostname=imports@%h
   celery -A app.celery_app.celery worker --loglevel=info -Q lead_embeddings --pool=threads --concurrency=1 --hostname=embeddings@%h
   celery -A app.celery_app.celery worker --loglevel=info -Q lead_exports --pool=threads --concurrency=1 --hostname=exports@%h
   ```

   Substitute configured queue names where applicable. Supervisor uses a
   separate Linux prefork export worker. Keep the physical qualification pool at
   30; the SQL setting limits active work. Never use a solo worker expecting 30
   concurrent leads. Each process allows up to 40 pooled SQL connections,
   including headroom beyond identity-lock connections; size Azure SQL and
   replica counts accordingly. Keep deployment shutdown grace periods long
   enough for requests to finish and journals to drain.
6. From your permitted environment, check `/health/ready`, sign in and open
   `/admin/settings`. Confirm Azure prices for your deployment/contract, set a
   USD/INR reference rate or refresh it, then save the desired concurrency.
   Start conservatively and increase while observing quotas and latency.
   Check a small new job's warnings and its cost drilldown against Azure usage.
   These production checks must be performed by the operator; they were not
   run during this change at the user's request.

## Concurrency and diagnostics

The range is 1–30, enforced by the API, SQL constraint and renewable global
Redis admission leases across qualification and evidence-refresh tasks. The
legacy `MAX_WORKERS` variable is unused. Saves use version checks and a short
versioned cache; another administrator's save cannot be silently overwritten.
Increasing the setting takes effect on new starts. Lowering it lets existing
leads finish, so active work can temporarily exceed the new value. A lost worker
can occupy a lease until its bounded expiry. A lost lease blocks later paid
calls and commits; work retries. Storage/coordination failures pause new starts.

More active leads can exhaust Azure request/token quotas, increase SQL load and
spend budget faster. HTTP 429 honors provider retry hints and applies a shared
cooldown to qualification starts. Throttling does not mark a lead as bad evidence.
The UI shows cooldown and recent warning/error events every three seconds while
visible. `/admin/logs` provides job/severity/rate-limit filters and cursor-paged
history. INFO output stays in server logs and is not written to SQL. Root Python
warnings/errors are captured with available job/lead/stage context, sanitized
and persisted asynchronously. Process-kill, container, Nginx and infrastructure
logs remain the hosting platform's responsibility. During a SQL outage the UI
can show recent Redis-buffered events; the complete SQL history returns after
recovery. Local warning queues are bounded and attempt a bounded clean-exit flush.

## Cost accounting

GPT-4.1 mini and text-embedding-ada-002 remain selected. Responses calls use
Azure's stable `/openai/v1/responses` endpoint with the existing Azure key and
deployment; chat/embeddings retain their existing API clients. Ensure the Azure
resource supports this endpoint and the web-search tool is enabled.

Every provider attempt is journaled before sending and updated with reported
usage afterward. Retries retain separate IDs. Replay is idempotent; interrupted
attempts remain `started`/unreported rather than appearing free. Redis journaling
falls back to SQL before a paid call; if neither accepts it, no call starts.
New records preserve their tariff snapshot, model identity, cached/input/output
tokens, embedding usage, duration, provider ID and reported Bing request count.
Provider billing identity must match a configured model family to price its
tokens; unknown models stay unpriced. Missing usage is visibly incomplete.

The job list, lead pages and individual call pages are bounded. Aggregates read
only the selected jobs/leads, and per-step totals supply the summary without
another full scan. Usage persistence updates job token totals asynchronously.
During processing, independent drilldowns can reflect slightly different instants.
Import/chat or unassigned requests are shown as job-level cost when no lead is
attributable; globally unattributed attempts remain separate. No arbitrary lead
is charged for them. Reused leads do not acquire the original upload's charge.

Default reference rates in USD are GPT-4.1 mini input **0.40/M**, cached input
**0.10/M**, output **1.60/M**; ada-002 **0.10/M**; Bing grounding **14/1,000
requests**. They start **unconfirmed**. Confirm regional/deployment/contract
rates in Azure and edit them in settings. New call snapshots are preserved
after later edits. These are estimates excluding tax, hosting, storage and
other infrastructure, not an Azure invoice or a guarantee of the final charge.

Bing fees use `tool_usage.web_search.num_requests`. Counts of visible
`web_search_call` output items are shown only as diagnostics, never used to
invent a search bill. Cached tokens are a subset of input and not charged twice.
Legacy records lack some cache/Bing/embedding usage. Only identifiable legacy
token usage can be estimated at the current reference tariff; missing historical
charges cannot be reconstructed without Azure's records or new processing.

INR uses the displayed configured reference rate, not an invented fixed rate.
The cost page attempts to obtain a Frankfurter daily reference if no rate is
configured, with bounded timeout/throttling. Refresh manually when needed or
enter your organization's rate. Conversion is a reporting estimate for the
selected reference date, not historical invoice FX or settlement currency.

References: [Azure web search billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing),
[Azure model pricing](https://azure.microsoft.com/en-us/pricing/details/azure-openai/),
[Bing grounding pricing](https://www.microsoft.com/en-us/bing/apis),
[GPT-4.1 mini public reference](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
[ada-002 historical Azure reference](https://download.microsoft.com/download/a/b/5/ab542db1-f1a7-4f92-b615-2e2eaccb64ea/Azure-OpenAI-Legacy-Pricing.pdf),
[Frankfurter reference rates](https://frankfurter.dev/).

## Removed features and verification boundary

Team-directory, owner assignment, follow-up reminder, saved-group and lead
workspace components, routes, client APIs, export columns and chatbot fields
are removed. Reviewed checkboxes, notes and evidence refresh remain. Legacy
assignment/reminder schema/data stays inactive for safe upgrades and recovery;
this migration does not destroy it. Existing authentication stays in place.

Verification during implementation is limited to source review, syntax parsing,
local import-name checks and Git whitespace/diff checks. No tests, builds,
application imports, migrations, Azure/SQL connections or provider calls were run.
