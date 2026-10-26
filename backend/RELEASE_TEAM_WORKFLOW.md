# Team workflow and evidence refresh

> Historical release. Team profiles, assignments, reminders, groups and the
> lead workspace were removed in [admin operations](RELEASE_ADMIN_OPERATIONS.md).
> Retain this migration prerequisite for the evidence-refresh schema.

The dashboard now has a team directory and follow-up reminders. The **Leads**
navigation opens a workspace across all uploads. Open any lead's details to
assign an owner, schedule a follow-up, maintain notes, or refresh its research.

## Team ownership

Create and edit team members in the dashboard. A member can be deactivated and
reactivated; existing assignments remain visible, and new assignments require
an active member. These are assignment profiles within the existing shared
authenticated workspace. They do not create separate login accounts or send
invitations. Team member emails are optional contact details.

Review state, tracking notes, ownership, and follow-up are attached to the shared
lead identity. Importing the same lead in another upload retains these values.
Version checks prevent simultaneous edits from silently overwriting each other;
the interface preserves unsaved drafts when an edit conflicts.

## Follow-ups

A lead can have one current follow-up, with a date/time and note. The dashboard
shows due, upcoming, and completed reminders, with an optional owner filter and
pagination. The navigation bell shows the due count. Lists and counts update
every minute while the app is open; overdue reminders remain available when a
user returns. These reminders appear inside the application.

Users can edit or cancel a follow-up, complete or reopen it, or snooze it for
one day or one week. Completion and snoozing preserve its note; cancellation
clears it. Dates are stored in UTC and displayed/edited in the browser's local
timezone. Changing a scheduled date reopens the follow-up.

## Lead workspace and saved groups

The workspace searches the full database before pagination, across uploads.
Search, priority, owner/unassigned, review state, follow-up status, country,
industry, minimum score, and evidence age can be combined. Repeated uploads
produce one representative per shared lead identity, preferring the latest
researched snapshot. Opening an individual upload still shows that upload's
own results. Reminders use the same deduplication so a repeat upload cannot
duplicate a reminder.

Save these filters as a named group, update its name/rules, or delete the group.
Groups store rules, so newly matching leads automatically appear. Deleting a
group leaves its lead data intact. Saved groups are shared within the workspace.

## Evidence freshness and manual refresh

Fresh means the actual web check occurred within 90 days. Older checks are stale;
missing or future dates are unknown. This indicator describes the date of the
research, not certainty that every fact is known or correct. Unclear facts and
their source proofs remain visible in ICP validation.

**Refresh evidence** starts new web, employment/contact, ICP, and scoring work
for the selected upload's lead. This bypasses saved qualification results and
the evidence cache, and can consume AI credits. The existing result stays visible
until every stage succeeds and the replacement commits atomically. Failed
refreshes leave it intact. A previous snapshot is retained in the database for
audit/recovery; no restore interface is included.

Refresh preserves manual tracking and historical upload counters/status. It
changes the selected result, while other uploads retain their own research
snapshots. Subsequent uploads prefer the freshest eligible research. Refreshing
a historically failed record does not rewrite the original upload's failure
counter. Job deletion is blocked while that upload or a refresh is processing.

Requests use UUIDs for safe network retries and a unique active-request index.
An SQL identity lock serializes research against normal processing of the same
lead. Each request has a persisted four-attempt research budget; an explicit
manual retry starts another bounded budget. Requests survive broker outages and
worker restarts. Worker startup and a periodic background scan recover queued
or abandoned requests in bounded batches; status polling also assists recovery.
The scan runs inside existing Celery workers, so Celery Beat is not required.
At least one worker listening on `lead_processing` must be running.

Current workflow fields are also included in Excel/CSV downloads and per-lead
chat answers. The chatbot does not change assignments or schedule follow-ups;
these actions use the lead details controls.

## Required deployment steps

Back up Azure SQL and let active imports, qualification jobs, and refreshes
finish. Stop the API and worker writers. From `backend`, with the target
environment configured:

```powershell
# If earlier database result reuse has not been migrated:
python scripts/migrate_lead_reuse.py

# If ordered chat history has not been migrated:
python scripts/migrate_chat_reliability.py

python scripts/migrate_team_workflow.py
```

The team workflow migration is additive and safe to rerun. It creates team,
saved-group, and refresh storage, adds tracking/freshness fields and indexes,
and backfills only valid dates from successful saved web research. It never
invents a research date from an upload date or deletes user data. The earlier
reuse migration now applies the workflow schema before its ORM backfill so
the current model relationships also work on that upgrade path.

Deploy the updated frontend, API, and workers together, then verify `/health/ready`
on the deployment. Readiness includes the new schema and active-refresh index.
No new secrets or notification service are required.

Source and diffs were reviewed statically. Per the request, tests, builds,
runtime execution, migration execution, and Azure SQL connections were not run
in this development session. Live behavior must be verified in the deployment.
