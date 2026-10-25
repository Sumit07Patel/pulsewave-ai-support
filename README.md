# PulseWave AI Support

A customer support and RMA assistant that answers **only from product manuals** and shows the source of every answer. Built as a portfolio project for a fictional audio brand.

![screenshot](docs/screenshot.png)

**Live demo:** https://YOUR-APP-URL  
**Stack:** FastAPI, LangGraph, Supabase pgvector, FastEmbed, Gemini Flash, Tailwind

## Why it exists
Generic support bots give vague answers, and plain LLMs invent features and unsafe troubleshooting steps. This assistant retrieves passages from the real manuals, refuses to answer when nothing relevant is found, and cites its sources.

## How it works
1. The browser posts `{message}` to `POST /chat`.
2. FastEmbed (`bge-small-en-v1.5`) embeds the question locally.
3. Supabase pgvector returns the closest manual passages.
4. A LangGraph guard routes to a fixed "I don't know" reply when nothing is retrieved, so the LLM is never called without context.
5. Gemini Flash answers using only that context. The API returns `{reply, sources}`.

## Engineering notes
- Decoupled FastAPI backend with typed request and response models
- Rate limits surface as HTTP 429, and the UI shows a retry button for 429s, timeouts and offline states
- Local embeddings keep memory low, and the model is baked into the Docker image to avoid slow cold starts
- Model output is rendered as text in the browser, never as HTML
- `/health` for uptime checks and `/health/db` for a scheduled keep-alive of the free database

## Quick start
1. Open `.env` and replace the two placeholder values (Gemini key and Supabase Session pooler URL).
2. Windows: double-click `run.bat`. Mac/Linux: run `./run.sh`.
3. Open http://127.0.0.1:8000

## Run locally (manual steps)
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then fill in your keys
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000. Manuals must already be ingested into the `smartrma_manuals` collection with the same embedding model.

## Deploy (free tier)
**Google Cloud Run** (needs a billing account, scales to zero):
```bash
gcloud run deploy pulsewave-support --source . --region asia-south1 \
  --memory 1Gi --max-instances 2 --allow-unauthenticated \
  --set-env-vars GOOGLE_API_KEY=...,SUPABASE_DB_URL=...
```
**Render** (no card, sleeps after 15 idle minutes, 512 MB RAM may be tight): create a Docker web service from this repo and add the same environment variables.

Use the Supabase **Session pooler** connection string, because the direct connection is IPv6 only.

## API
| Method | Path | Description |
|---|---|---|
| POST | `/chat` | `{message, session_id?}` returns `{reply, sources[]}` |
| GET | `/health` | Liveness check |
| GET | `/health/db` | Runs a real vector query |

## Roadmap
Evaluation set for answer accuracy, response streaming, multi-turn memory via `session_id`, warranty lookup and RMA ticket creation as agent tools.

## Warranty and RMA records
1. In Supabase open **SQL Editor**, run `db/schema.sql`, then `db/seed.sql` for sample data.
2. Add `SUPABASE_URL` and the **service_role** `SUPABASE_KEY` to `.env`.
3. Try: `Check warranty for PW-APX-10001`, then `RMA PW-APX-10001 ORD-100234`.

Messages containing a serial number (`PW-XXX-12345`) are routed to the records instead of the manuals. Lookups return product and warranty dates only, never customer details, and a ticket requires a matching order ID. Tables use row level security, so only the backend key can read them.
