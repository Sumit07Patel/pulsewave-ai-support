import logging
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.agent import app_graph, vectorstore

log = logging.getLogger("pulsewave")
INDEX_FILE = Path(__file__).resolve().parent.parent / "index.html"

app = FastAPI(title="PulseWave Support API")
origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    sources: list[str] = []
    mode: str = "ai"          # ai | excerpt | no_match
    cached: bool = False


# ---------- Free-tier protection: answer cache + per-visitor rate limit ----------
CACHE_TTL, CACHE_MAX = 3600, 200
_cache: OrderedDict = OrderedDict()

def _norm(msg: str) -> str:
    return re.sub(r"\W+", " ", msg.lower()).strip()

def cache_get(key: str):
    item = _cache.get(key)
    if item and time.time() - item[0] < CACHE_TTL:
        _cache.move_to_end(key)
        return item[1]
    _cache.pop(key, None)
    return None

def cache_set(key: str, value: ChatResponse):
    _cache[key] = (time.time(), value)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)

RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "12"))
_hits: dict[str, deque] = defaultdict(deque)

def check_rate(ip: str):
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        wait = int(60 - (now - q[0])) + 1
        raise HTTPException(429, "Too many questions. Please wait a moment.", headers={"Retry-After": str(wait)})
    q.append(now)


def _is_rate_limited(exc: Exception) -> bool:
    t = str(exc)
    return "429" in t or "RESOURCE_EXHAUSTED" in t or "quota" in t.lower()


# ---------- Routes ----------
@app.get("/")
def home():
    return FileResponse(INDEX_FILE)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/health/db")
def health_db():
    """Real vector query. Schedule it to keep a free Supabase project awake."""
    try:
        vectorstore.similarity_search("ping", k=1)
        return {"status": "ok", "db": "reachable"}
    except Exception as exc:
        log.exception("DB health check failed")
        raise HTTPException(503, "database unreachable") from exc


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(request: ChatRequest, http: Request):
    key = _norm(request.message)
    hit = cache_get(key)
    if hit:
        return hit.model_copy(update={"cached": True})

    ip = http.headers.get("x-forwarded-for", "").split(",")[0].strip() or (http.client.host if http.client else "?")
    check_rate(ip)

    try:
        result = app_graph.invoke({"messages": [("user", request.message)]})
        raw = result["messages"][-1].content
        if isinstance(raw, list):
            reply = "".join(b["text"] for b in raw if isinstance(b, dict) and b.get("type") == "text")
        else:
            reply = str(raw)

        resp = ChatResponse(reply=reply, sources=result.get("sources", []), mode=result.get("mode", "ai"))
        if resp.mode in ("ai", "no_match"):      # never cache degraded answers
            cache_set(key, resp)
        return resp

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Chat pipeline failed")
        if _is_rate_limited(exc):
            raise HTTPException(429, "Model is rate limited. Try again shortly.", headers={"Retry-After": "8"}) from exc
        raise HTTPException(500, "Something went wrong while answering.") from exc
