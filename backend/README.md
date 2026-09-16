# Quality Lead API and workers

FastAPI accepts uploads and serves job/results APIs. Celery performs imports, lead qualification, web research, scoring, outreach generation, and embeddings asynchronously.

For database result reuse, shared tracking, ICP evidence, and the required migration,
see [the release and rollout guide](RELEASE_LEAD_REUSE.md).

Start locally after configuring `.env`:

```powershell
uvicorn app.main:app --reload --port 8000
celery -A app.celery_app.celery worker --loglevel=info -Q lead_processing,lead_imports,lead_embeddings --pool=solo
```

See `../dev.md` for local setup, `../read.md` for the end-to-end design, and `../AZURE_DEPLOYMENT.md` for production deployment.

docker local run commands
docker build --build-arg VITE_API_BASE_URL=http://localhost -t qualitylead .
docker run --name qualitylead -p 80:80 --env-file backend/.env qualitylead
