# Intern Agent

AI agent that helps students land internships. Paste a vacancy link from **hh.kz** (or raw text from LinkedIn / anywhere else) — the agent compares it against your resume and returns:

- **Match score (0–100)** with an honest verdict — is it worth applying?
- **Matched vs missing requirements** — what you already cover and what to learn
- **Actionable recommendations** for this specific vacancy
- **Resume tailored to the vacancy** — your facts, reordered and rephrased for the role
- **Cover letters in Russian and English**, ready to send
- **Application tracker** — every analysis is saved with a status pipeline: analyzed → applied → reply → interview → offer / rejected

No invented experience: the agent works strictly with facts from your resume.

## How it works

```
hh.kz link ──► hh API (api.hh.ru) ──► fallback: JSON-LD from the vacancy page
                                │
raw vacancy text ───────────────┤
                                ▼
              your resume + vacancy ──► Gemini (structured JSON output)
                                ▼
              score / gaps / tailored resume / cover letters ──► SQLite tracker
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
| `GET` | `/api/applications` | tracker list + stats |
| `GET` / `PATCH` / `DELETE` | `/api/applications/{id}` | detail / update status / remove |

## Tests

```bash
ruff check src tests
PYTHONPATH=src pytest -q   # 30 tests
```

## Roadmap

- [ ] Auto-monitor new vacancies by keywords → Telegram notifications
- [ ] Response/conversion analytics in the tracker
- [ ] PDF export of the tailored resume
