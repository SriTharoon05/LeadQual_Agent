# Consistent qualification and focused chat answers

The scoring policy has three components: **ICP 60%, contact availability 25%,
and sourced growth 15%**. Code computes the score and priority; a model only
compares ICP facts and drafts outreach. Arbitrary model scores and hidden
disqualifiers cannot contradict the displayed comparisons.

## Qualification rules

- Every required ICP comparison must match, including current employment.
  Unrequested criteria are excluded; optional preferences do not disqualify.
- Current role checks include both seniority and function. A VP of Finance does
  not automatically match a prompt limited to IT, Operations, Procurement, etc.
- One available email **or** phone is sufficient. A second method earns no bonus
  and a missing phone cannot reduce an email-contactable lead's score.
- Explicit channel requirements in the ICP can require email, phone, or both.
- All required ICP checks plus an available permitted contact give **85–100,
  High priority**. Supported growth controls the ranking within this group.
- A confirmed mandatory ICP mismatch or no permitted usable contact caps the
  final score at **49, Low**. Component scores still show the reason for the cap.
- Incomplete ICP/contact evidence caps the final score at **79.9** and requires
  review. Unknown does not mean a confirmed failure. Priority follows the final
  score everywhere: High >=80, Medium >=50 and <80, Low <50.

The lead workspace defaults to highest score first. Priority sorting uses
High/Medium/Low ranks, with score as the next sort key, rather than alphabetical
label order. The job table also defaults to descending score.

ICP points are calculated from the displayed required comparisons: match=1,
unclear=0.5, mismatch=0, divided by the number of required comparisons. The
contact component is 100 when a permitted method is available, 50 when its
availability is unresolved, and 0 when no permitted method is usable.

Growth points require an event date, relevance, supporting excerpt, and URL
from the actual research sources. Hiring=100, expansion=95, technology
investment=90, funding=80, acquisition=70, product launch=60 and leadership
change=45. Events up to 180 days old retain full strength; 181–365 days use 80%,
366–730 days use 50%, and older/future/undated events receive no points. The
strongest supported signal is used, plus 5 points per additional event category
(maximum 10 bonus points, total capped at 100). Dates are evaluated as of the
saved research check, which keeps snapshots reproducible. Evidence freshness
still shows the age of that research. Growth is an indicator of possible demand,
not a guarantee of interest or spending.

## Contact evidence without delivery claims

Email syntax, DNS mail routing, and phone numbering-plan format are different
from mailbox existence, ownership and successful delivery/call connection.
Valid syntax with inconclusive DNS is shown as unverified, not rejected.
Syntax/domain-routing failures include the finding and recorded observation;
DNS links open a live lookup and may differ from the saved observation.

Missing MX alone is not a failure: SMTP permits fallback A/AAAA mail routing.
A Null MX record explicitly declares that the domain accepts no mail.
See [SMTP routing](https://www.rfc-editor.org/rfc/rfc5321.html#section-5.1)
and [Null MX](https://www.rfc-editor.org/rfc/rfc7505.html).

SMTP probes remain optional and send no message. A probe rejection/timeout or
disabled probe does not establish an undeliverable mailbox; even RCPT acceptance
does not guarantee inbox delivery. An individual legacy false delivery flag is
treated as unknown, not proof of a dead mailbox. A legacy negative contact check
without recorded evidence requires review/refresh. Real sending-provider
delivery/bounce events would be needed for stronger delivery observations; this
release does not invent those events or integrate an unspecified provider.

Contact availability and growth now appear alongside the six ICP proof cards.
The validation column and separate authority/data scores are removed from the
UI, API response and exports. Nullable historical database columns remain for
safe upgrades; they are not used for scoring.

## Chat fixes

- Only two distinct sources initially appear, with an accessible See more/less
  control. Tracking/hash/host variants are deduplicated and titles/domains replace
  repetitive company-only labels. Answers do not need a long list of URLs.
- Narrow field questions stay narrow, and a plain row question no longer dumps
  every field, workflow value and proof. Text uses readable Markdown bullets.
- Company overviews, contact lookups and qualification explanations use saved
  facts directly, without an extra narrative model dependency.
- Sourced industry, size, revenue and HQ values take precedence over blank upload
  cells. Relevant SQL filters/sorts also use confirmed researched values.
- Follow-ups such as 'company size of that' are not mistaken for company names.
  Company-level follow-ups can resolve from the previously selected contact/row.
- Unique punctuation-only spelling variants such as RCMD/RCM&D resolve together;
  actual misspellings/partial names still require user confirmation.
- Explicit row queries search the entire upload and reset unrelated old scopes.
  If the row is missing, the reply gives the actual saved count/range. A job with
  396 saved rows cannot truthfully return row 500. Stored row numbers are now
  visible in the table/drawer and remain stable when the score sort changes.

## Required deployment and existing-record update

Back up Azure SQL. Let imports, qualification jobs and evidence refreshes finish,
then stop API/workers. Install the updated backend requirements (including
`email-validator`) and apply any outstanding earlier migrations first. From
`backend`, using the target environment:

```powershell
python scripts/migrate_scoring_v2.py
```

The additive schema change and existing-score recalculation run in one
transaction and can safely be rerun. Existing valid comparisons are reused;
unsubstantiated old contact failures become reviewable unknowns. Old undated
growth summaries do not receive made-up growth points; refresh those leads to
obtain new dated/source-attributed research. Existing criteria are not silently
re-researched or reinterpreted by a language model during this backfill.
Original score/ICP/contact snapshots are retained in a deferred audit field,
separate from normal API responses. Manual tracking, ownership, reminders,
research timestamps, upload counts and processing status remain intact.

Deploy the matching frontend, API and Celery workers together and check
`/health/ready`. Readiness rejects missing columns or scores still using the
previous policy. Regenerate exports through the normal download controls;
older downloaded spreadsheets/chat messages remain historical snapshots.

Verification for this change was static source/diff review only, as requested.
No tests, builds, typechecks, application imports, migrations, live DNS/email
checks, or Azure SQL connections were executed in this development session.
Live verification remains required on the deployment network.
