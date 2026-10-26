# Quality Lead API and workers

For durable upload/qualification dispatch, idempotent upload retries, asynchronous
website indexing, compact table responses and the required first migration, see
[the performance rollout guide](RELEASE_PERFORMANCE.md).

FastAPI accepts uploads and serves job/results APIs. Celery performs imports, lead qualification, web research, scoring, outreach generation, and embeddings asynchronously.

For database result reuse, shared tracking, ICP evidence, and the required migration,
see [the release and rollout guide](RELEASE_LEAD_REUSE.md).

For full-job chatbot retrieval, ordered history, safe retries, and its required
database migration, see [the chat rollout guide](RELEASE_CHAT_RELIABILITY.md).

For team assignments, in-app follow-up reminders, saved lead groups, evidence
freshness/manual refresh, and required database upgrades, see
[the team workflow rollout guide](RELEASE_TEAM_WORKFLOW.md).

For deterministic ICP/contact/growth scoring, honest contact evidence, focused
chat answers, and recalculation of existing results, see
[the scoring v2 rollout guide](RELEASE_SCORING_V2.md). Its migration is required
before deploying the updated API/workers.

For background CSV/Excel exports, bounded-memory writers, download progress and
the required dedicated export worker, see [the export rollout guide](RELEASE_EXPORTS.md).

Start locally after configuring `.env`:

```powershell
uvicorn app.main:app --reload --port 8000
celery -A app.celery_app.celery worker --loglevel=info -Q lead_processing,lead_imports,lead_embeddings --pool=solo
```

Include `lead_exports` in the worker queue list when testing background exports.
Install all backend dependencies with `python -m pip install -r requirements.txt`;
export workers require XlsxWriter even when no export has been requested yet.

If Vite starts on port 5174, add its exact origin to `CORS_ORIGINS` in your existing
backend `.env`, then restart Uvicorn. For example:
`CORS_ORIGINS=http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174`.
Existing `.env` values override defaults and are not changed by pulling code.

Azure SQL error 40615 means the server firewall denied the client's public IP.
In Azure Portal, open the SQL server's Networking settings and allow the exact
client IP reported in the error (start and end IP equal), or use your organization's
approved private-network connection. Wait up to five minutes and retry. API database
connection failures return HTTP 503; a code update does not grant firewall access.

See `../dev.md` for local setup, `../read.md` for the end-to-end design, and `../AZURE_DEPLOYMENT.md` for production deployment.

docker local run commands
docker build --build-arg VITE_API_BASE_URL=http://localhost -t qualitylead .
docker run --name qualitylead -p 80:80 --env-file backend/.env qualitylead
