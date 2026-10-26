# Reuse or fresh AI research

The ICP form now offers two research modes, on both the Upload page and an
imported job waiting to start:

- **Reuse saved research** (default): reuse compatible completed results or
  earlier research when available, and research new leads normally.
- **Fresh AI research**: bypass other jobs' qualifications, saved company/person
  research and the application's shared Redis/in-memory research cache. Every
  unique lead starts new web research, contact checks, ICP comparison, scoring
  and applicable outreach generation. Website indexing remains asynchronous.
  This takes longer and can consume more AI/search credits.

The policy is stored on the job, not only in a browser checkbox or task message.
It is saved atomically with the ICP before dispatch and cannot be changed after
processing begins. Retry/recovery workers read that saved policy. An identical
ICP submission is idempotent only when its mode also matches.

Fresh means a new application research run using the uploaded data. It does not
erase the model's training knowledge or control Azure/Bing's internal caching.
Retries may resume stages already completed **within that same new run**, so
an interrupted final stage does not repeatedly charge for successful research.
No earlier upload's evidence or score is copied into a fresh job. Successful
fresh research can become available to later jobs that explicitly allow reuse.

Reviewed checkboxes and manual notes stay shared for the same contact. Original
uploaded details remain inputs, and duplicate rows inside one uploaded file are
still deduplicated. These are separate from AI research/result reuse.

## Run an already completed file again

1. Open its job and click **New run**.
2. Upload the original Excel/CSV again. This creates a separate job and leaves
   the old results intact. The prior ICP is prefilled and fresh mode is selected.
3. Choose **Fresh AI research** or switch back to **Reuse saved research**, then
   click **Start Processing**.

Upload retry IDs still recover unfinished imports. A stored ID for a running
or finished job cannot accidentally send this new upload back to its old result.
The former Refresh button is now **Reload results**: it only reloads saved
results and does not start paid AI work. **Refresh evidence** in the individual
lead drawer remains the way to refresh only that lead.

The job header displays its selected mode. Reuse counters continue to distinguish
copied results, reused research (including cache hits), and newly researched
leads. Fresh jobs do not count their own stage retries as cross-job reuse.

## Required rollout

Back up SQL, let active jobs finish, stop the API and all workers, and apply
prior migrations if not already installed. With the updated backend and the
existing permitted Azure SQL connection, run from `backend`:

```powershell
python scripts/migrate_processing_modes.py
```

In the root Docker image run the same command from `/app`. It adds a non-null
`jobs.processing_mode` with default `reuse` and a `reuse`/`fresh` check constraint.
Existing jobs keep their old behavior; no AI processing or network research runs
during migration. The script is restartable and must finish before the new API
and workers start. Deploy the matching frontend and restart **all workers**;
older workers do not honor fresh mode. This does not change admin authentication
or add operational log/cost links to user screens.

Verification during implementation was limited to source syntax, import/reference
review and Git diff checks. No tests, builds, app execution, migrations, Azure SQL
or AI requests were run, per the user's standing instruction. In the permitted
environment verify a repeated file in both modes, a worker restart mid-run, and
an identical-versus-conflicting ICP/mode submission before wider rollout.
