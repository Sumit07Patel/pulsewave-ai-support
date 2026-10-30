# PulseWave AI Support

A customer support and returns assistant for a fictional audio brand. It answers product questions **only from the official manuals**, shows the source of every answer, and handles warranty checks and return (RMA) tickets from a database of customer records.

![screenshot](docs/screenshot.png)

| | |
|---|---|
| **Live demo** | https://pulsewave-ai-support.onrender.com/ |
| **Source** | https://github.com/Sumit07Patel/pulsewave-ai-support |
| **Author** | Sumit Patel: [GitHub](https://github.com/Sumit07Patel) · [LinkedIn](https://www.linkedin.com/in/sumit-patel-87565b1b7/) |
| **Stack** | FastAPI, LangGraph, Supabase pgvector, FastEmbed, Gemini Flash, Docker, Tailwind |

> The demo runs on a free Render instance. After it has been idle, the first request can take up to a minute while the service wakes up.

## Why it exists

Generic support bots give vague answers, and plain LLMs invent features and unsafe troubleshooting steps. This assistant retrieves passages from the real manuals, limits the model to that context, and cites its sources. For account questions it does not use a language model at all: warranty and return handling is plain, auditable code.

## What it can do

| The customer says | What happens |
|---|---|
| "How do I link two Stone Spark speakers?" | Searches only the Stone Spark manual and answers with the source section |
| "How do I reset it?" (after giving a serial number or naming a product) | Uses the conversation to find the right manual |
| "Is my warranty active?" | Asks for a serial number (`PW-APX-10001`) or order ID (`ORD-100234`) |
| `PW-APX-10001` | Shows the product, purchase date and warranty end date |
| "I want to return my earbuds" → serial number → order ID | Collects both across messages, then opens a ticket (`RMA-2610-A1B2C3`) |
| "Status of my return?" or a ticket number | Reports the ticket status |
| A question the manuals do not cover | Declines instead of guessing (see [Limitations](#limitations)) |

## How it works

1. The browser posts `{message, session_id}` to `POST /chat`. The server adds the last few messages of that session.
2. A LangGraph router checks the newest message. Serial numbers, order IDs, ticket IDs and "my warranty" style questions go to the **records node**. Everything else goes to the **manuals path**.
3. **Manuals path:** the product is detected from the chat (a product name, or the product behind a serial number given earlier). FastEmbed (`bge-small-en-v1.5`) embeds the question locally (with the product name removed from the search text once the product is known, because it only dilutes the match), and pgvector returns the closest passages, filtered to that product when it is known.
4. An optional guard (`MAX_DISTANCE`) rejects weak matches and returns a fixed reply without calling the model. The prompt also tells the model to decline when the context lacks the answer.
5. Gemini Flash answers from the retrieved context only. If it fails or is rate limited, a backup model runs, and if that fails too, the top manual excerpts are shown instead.
6. **Records node:** plain Python reads the `warranty_lookup` view and the `rma_tickets` table. No model decides who gets a ticket.
7. The API returns `{reply, sources, mode, cached, session_id}`.

## Engineering notes

- **Product-aware retrieval.** Every chunk is tagged with its product at ingestion, so a question about one device cannot return passages from another device's manual.
- **Deterministic returns.** A ticket needs a matching serial number and order ID, an active warranty, and no other open ticket for that device. Lookups never reveal customer names, emails or phone numbers.
- **Multi-turn sessions.** The server keeps the last 8 messages per session for 30 minutes. This is what lets the return flow span several messages.
- **Idempotent ingestion.** `ingest.py` replaces a product's chunks on every run, so re-running it never creates duplicates or leaves stale chunks behind.
- **Failure handling.** Rate limits surface as HTTP 429 and the UI shows a retry button for 429s, timeouts and offline states. A backup model and a manual-excerpt fallback keep answers flowing on the free tier.
- **Free-tier protection.** First questions of a session are cached for an hour, and each IP is limited to 12 questions a minute. Customer-specific answers are never cached.
- **Lean footprint.** Local embeddings cost nothing per query, and the model is baked into the Docker image to avoid slow cold starts.
- **Safe output.** Model output is rendered as text in the browser, never as HTML. Record tables use row level security, so only the backend key can read them.

## Knowledge base

Three manuals, 88 chunks in total (chunk size 1000, overlap 100):

| Manual | Product key | Chunks |
|---|---|---|
| `AirdopesApex.txt` | `airdopesapex` | 27 |
| `StoneSpark.txt` | `stonespark` | 29 |
| `StormCallPlus.txt` | `stormcallplus` | 32 |

Upload manuals (`.txt`, `.md` or `.pdf`) to a Supabase Storage bucket named `manuals`. The lower-cased file name becomes the product key. Keys must match `PRODUCT_ALIASES` in `app/agent.py` and the `manual_product` column of the `products` table.

## Results

Measured on `eval/eval_set.json`: 30 answerable questions (10 per product) and 8 out-of-scope questions, written by hand against the three manuals. Retrieval is scored the way the app searches: limited to the product's manual, top 4 passages (`python eval/eval_retrieval.py`).

| Measure | Result |
|---|---|
| Right passage in the top 4 (hit@4) | **97%** (29 of 30) |
| Right passage ranked first (hit@1) | 83% |
| Right passage in the top 8 | 100% |
| Mean reciprocal rank | 0.89 |
| hit@4 with the product name dropped from the search text (what the app now does) | **100%**, mean reciprocal rank 0.97 |
| Passages from the wrong manual **without** the product filter | 23% of results |
| Retrieval time, median / 95th percentile | 577 ms / 599 ms |
| Warranty and return replies (no model), median / 95th percentile | 0.18 s / 0.58 s |
| Model-written answers, 95th percentile | about 5.6 s |
| End-to-end checks (`python eval/smoke_test.py`) | 77 of 77 passed (65 of 65 in `--quick` mode) |
| Out-of-scope questions declined in the end-to-end run | 4 of 4 |

How to read these numbers:
- The set is small and hand-written, so treat the percentages as a sanity check, not a benchmark. A retrieval hit shows the passage was found, not that the model's answer was correct.
- A first run scored hit@4 of 87%. Three of its four misses were wrong expected phrases in the test set (fixed), and the fourth was a product name diluting the search (fixed in the app).
- The distance guard is not enabled. The worst answerable question has a best-match distance of 0.376, and the closest unrelated questions (a washing machine, a competitor's headphones) sit at 0.353 and 0.370, so no single threshold separates them cleanly. The prompt handles those. See [Configuration](#configuration).
- The Gemini free tier allows 20 requests a day per model. Beyond that the app falls back to manual excerpts (`mode: excerpt`), which the end-to-end test reports as a warning. Run `smoke_test.py --quick` (about 10 model requests) to stay inside the quota.
- Cost per query is not measured yet.

## Quick start

1. Copy `.env.example` to `.env` and fill in the values from [Configuration](#configuration).
2. In Supabase open **SQL Editor** and run, in this order:
   1. `db/schema.sql` (tables, safe lookup view, row level security)
   2. `db/seed.sql` (sample customers and devices)
   3. `db/migration_002_manual_product.sql` (links each product to its manual and adds sample devices for Stone Spark and StormCall Plus)
3. Ingest the manuals once:
   ```bash
   python ingest.py
   ```
   It prints the manual and chunk counts when it finishes.
4. Start the app. Windows: double-click `run.bat`. Mac/Linux: `./run.sh`. Or manually:
   ```bash
   python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   uvicorn app.main:app --reload
   ```
5. Open http://127.0.0.1:8000

Use the Supabase **Session pooler** connection string, because the direct connection is IPv6 only.

### Sample data to try

| Serial number | Order ID | Product | Warranty |
|---|---|---|---|
| `PW-APX-10001` | `ORD-100234` | Airdopes Apex | Active |
| `PW-APX-10002` | `ORD-100235` | Airdopes Apex | Expired |
| `PW-SPK-40001` | `ORD-100520` | Stone Spark | Active |
| `PW-STM-50001` | `ORD-100521` | StormCall Plus | Expired |

Try these chats:
- `Is my warranty active?` → `PW-APX-10001`
- `I want to return my earbuds` → `PW-APX-10001` → `ORD-100234`, then `Status of my return?`
- `How do I factory reset the Stone Spark?` and check that the source is `StoneSpark.txt`

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | Yes | Gemini API key |
| `SUPABASE_DB_URL` | Yes | Postgres connection string (Session pooler) for pgvector |
| `SUPABASE_URL` | For records | Project URL, also used by ingestion |
| `SUPABASE_KEY` | For records | **service_role** key. Keep it on the backend only |
| `MAX_DISTANCE` | No | Maximum vector distance for a passage to count as relevant. Unset (the default) means the guard never rejects. Around 0.43 refused none of the answerable test questions and caught 6 of 8 unrelated ones, but check it against vague real questions before enabling |
| `TOP_K` | No | Passages retrieved per question (default 4) |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | No | Main and backup model names |
| `RATE_LIMIT_PER_MIN` | No | Questions per IP per minute (default 12) |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default `*`) |
| `FASTEMBED_CACHE_DIR` | No | Where the embedding model is cached |

To pick `MAX_DISTANCE`, run `python eval/eval_retrieval.py`, which compares the best-match distances of answerable and unrelated questions, or watch the `best_distance` values in the server log. Recalibrate after changing the embedding model, chunk size or manuals.

## Deploy

**Render** (what the live demo uses): create a Docker web service from this repo and add the environment variables above. The free tier sleeps after 15 idle minutes and has limited RAM, which is why embeddings are local and small.

**Google Cloud Run** (scales to zero, needs a billing account):
```bash
gcloud run deploy pulsewave-support --source . --region asia-south1 \
  --memory 1Gi --max-instances 1 --allow-unauthenticated \
  --set-env-vars GOOGLE_API_KEY=...,SUPABASE_DB_URL=...,SUPABASE_URL=...,SUPABASE_KEY=...
```
Keep `--max-instances 1`: conversation memory is stored in process memory and is not shared between instances.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/chat` | Request `{message, session_id?}`. Response `{reply, sources[], mode, cached, session_id}` |
| GET | `/health` | Liveness check |
| GET | `/health/db` | Runs a real vector query, useful as a scheduled keep-alive for the free database |

`mode` is one of `ai` (generated from manuals), `excerpt` (model unavailable, raw passages shown), `no_match` (nothing relevant found) or `crm` (answered from warranty records). Send the `session_id` from each response back with the next message, or the server treats it as a new conversation.

## Limitations

- **Sessions live in memory.** A restart clears them, and they are not shared between instances. Moving them to a database table is the fix.
- **Return verification is light.** Serial number plus order ID works as a simple ownership check, but it is not strong identity verification.
- **Off-topic questions rely on the prompt.** `MAX_DISTANCE` is not set, so unrelated questions reach the model, which is instructed to decline when the context lacks the answer. It did so for all four unrelated questions in the end-to-end run.
- **Gemini free-tier quota.** 20 requests a day per model. After that the app shows raw manual excerpts instead of written answers.
- **Products without a manual** (Airdopes Lite and Pulse Sport in the sample data) have no `manual_product`, so questions about them search all manuals.
- **Small evaluation set.** 30 answerable questions, written by hand. Retrieval is measured, but the correctness of the generated answers is not scored yet.

## Roadmap

- Score the correctness of generated answers (retrieval is already measured)
- Response streaming
- Sessions stored in Supabase so the app can run on several instances
- Stronger identity check for returns, using the email registered at purchase
- Tracing for every request

## Repository layout

```
app/main.py        FastAPI routes, sessions, cache, rate limit
app/agent.py       LangGraph router, retrieval, guard, answer
app/crm.py         Warranty, return and ticket-status logic
db/schema.sql      Tables, safe lookup view, row level security
db/seed.sql        Sample customers and devices
db/migration_002_manual_product.sql   Product-to-manual link and extra samples
ingest.py          Loads manuals into pgvector
index.html         Demo store and chat interface
manuals/           The three product manuals
eval/              eval_set.json, validate_eval_set.py, eval_retrieval.py, smoke_test.py
```