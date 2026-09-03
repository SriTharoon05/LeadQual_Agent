# LeadQual AI — Frontend

A production-grade React frontend for an AI Lead Qualification Platform, wired to a FastAPI backend.

## Features

- **Login** — expiring signed-token authentication
- **Dashboard** — server-aggregated KPI cards and live asynchronous-job lobby
- **Upload Flow** — drag-and-drop import, ICP prompt, live progress, and leave-to-dashboard action
- **Job Detail** — globally searchable/sortable/filterable paginated results, retry/resume ICP, lead radar, and Outreach Intelligence
- **Chatbot** — floating session-scoped assistant with source links and tool tags
- **Real-time polling** — React Query with `placeholderData` for flicker-free refreshes

## Getting Started

```bash
# 1. Configure the backend URL
cp .env.example .env
# edit .env → VITE_API_BASE_URL=http://127.0.0.1:8000

# 2. Install dependencies
npm ci

# 3. Start the dev server
npm run dev
```

The app runs at `http://localhost:5173` and calls the configured FastAPI URL directly.

## Environment Variables

| Variable             | Default                | Description                |
|----------------------|------------------------|----------------------------|
| `VITE_API_BASE_URL`  | `http://127.0.0.1:8000`| FastAPI backend base URL   |

## Tech Stack

- React + Vite + TypeScript
- Tailwind CSS (custom theme)
- React Router (navigation)
- TanStack React Query (data fetching & polling)
- Axios (HTTP client with auth interceptors)
- Recharts (charts)
- Framer Motion (animations)
- lucide-react (icons)

## API Endpoints Used

| Method | Endpoint                        | Purpose                     |
|--------|---------------------------------|-----------------------------|
| POST   | `/auth/login`                   | Authenticate admin          |
| GET    | `/health`                       | Backend health check        |
| POST   | `/upload`                       | Upload lead file            |
| POST   | `/jobs/{id}/icp`                | Start ICP processing        |
| GET    | `/jobs`                         | List all jobs               |
| GET    | `/jobs/{id}/status`             | Job progress status         |
| GET    | `/jobs/{id}/results`            | Job lead results            |
| GET    | `/jobs/{id}/results/{lead_id}`  | Single lead detail          |
| GET    | `/jobs/{id}/export/excel`       | Export to Excel             |
| GET    | `/jobs/{id}/export/csv`         | Export to CSV               |
| DELETE | `/jobs/{id}`                    | Delete a job                |
| POST   | `/jobs/{id}/chat`               | Send chat message           |
| GET    | `/jobs/{id}/chat/history`       | Chat history                |
