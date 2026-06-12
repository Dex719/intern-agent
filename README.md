# Intern Agent

**Live: [intern-agent-production.up.railway.app](https://intern-agent-production.up.railway.app/)**

AI agent that helps students land internships. It scans fresh vacancies on **hh.kz** by your search queries, scores each one against your resume and shows only the ones worth applying to. One click on **Apply** — and the AI writes a cover letter and tailors your resume for that exact vacancy.

- **Vacancy feed** — the agent searches hh.kz, screens every new vacancy against your resume in a single LLM pass and ranks them by score; ignore or apply in one click
- **Scheduled auto-scan** — background scanning every N hours with Telegram notifications for high-score vacancies
- **Password login** — single-user auth with PBKDF2 hashing and httpOnly session cookies (set on first launch)
- **Pluggable LLM providers** — Gemini, OpenAI or OpenRouter with your own API key, configurable in the UI
- **Built-in logs viewer** — recent scan/LLM/auth events right in the UI for quick debugging
- **Match score (0–100)** with an honest verdict — is it worth applying?
- **Matched vs missing requirements** — what you already cover and what to learn
- **Actionable recommendations** for this specific vacancy
- **Resume tailored to the vacancy** — your facts, reordered and rephrased for the role
- **Cover letters in Russian and English**, ready to send
- **Application tracker** — every analysis is saved with a status pipeline: analyzed → applied → reply → interview → offer / rejected

No invented experience: the agent works strictly with facts from your resume.

## How it works

```
search queries ──► hh search (api.hh.ru ──► fallback: hh.kz HTML)
                        │ new vacancy ids
hh.kz link ─────────────┤ details: hh API ──► fallback: JSON-LD from the page
raw vacancy text ───────┤
                        ▼
   your resume + vacancies ──► Gemini (structured JSON output)
                        ▼
   feed scores ──► Apply: tailored resume + cover letters ──► SQLite tracker
```

- **Vacancy fetching** — official open hh API first; if it's unavailable for the server IP, the agent falls back to parsing schema.org JobPosting JSON-LD straight from the vacancy page.
- **Analysis** — Google Gemini with a strict JSON response schema (no free-form parsing).
- **Storage** — single SQLite file, no external services.

## Stack

FastAPI · SQLite · Google Gemini · vanilla JS + Material 3 Expressive UI (light/dark) · pytest + ruff

## Run locally

```bash
pip install -e ".[dev]"
export GEMINI_API_KEY=your_key        # https://aistudio.google.com/apikey
PYTHONPATH=src python -m uvicorn intern_agent.api.app:app --reload
# open http://localhost:8000
```

## Deploy (Railway)

1. Create a project from this repo — `railway.json` handles build & start.
2. Set the `GEMINI_API_KEY` variable.
3. Add a Volume mounted at `/data` and set `DB_PATH=/data/intern.db` so the tracker survives redeploys.

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | health check |
| `GET` / `PUT` | `/api/resume` | get / save resume |
| `POST` | `/api/analyze` | `{url}` or `{text}` → full analysis, saved to tracker |
| `GET` / `PUT` | `/api/settings` | search queries for the feed |
| `POST` | `/api/scan` | scan hh by saved queries, score new vacancies into the feed |
| `GET` / `PATCH` | `/api/feed` | feed items / ignore item |
| `POST` | `/api/auth/setup` / `login` / `logout` | first-run password setup, sessions |
| `GET` | `/api/logs` | recent app events (scan, LLM, auth) |
| `POST` | `/api/feed/{id}/apply` | generate application materials, move to tracker |
| `GET` | `/api/applications` | tracker list + stats |
| `GET` / `PATCH` / `DELETE` | `/api/applications/{id}` | detail / update status / remove |

## Tests

```bash
ruff check src tests
PYTHONPATH=src pytest -q   # 45 tests
```

## Roadmap

- [x] Scheduled auto-scan → Telegram notifications
- [ ] Response/conversion analytics in the tracker
- [ ] PDF export of the tailored resume
