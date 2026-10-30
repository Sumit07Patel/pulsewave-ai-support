"""LangGraph pipeline.
  - Questions about the customer's own records (serial / order / ticket) -> deterministic CRM node
  - Everything else -> retrieve manual passages (filtered to one product when it is known) -> guard -> answer
Free-tier hardening: backup model, and a manual-excerpt answer if every LLM call fails."""
import logging
import os
import re
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_community.embeddings import FastEmbedEmbeddings
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_postgres import PGVector
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app import crm

load_dotenv()
log = logging.getLogger("pulsewave")
# Python drops INFO messages unless a handler and level are set, and uvicorn only configures its own loggers.
# Without this, the "best_distance" lines used to calibrate MAX_DISTANCE would never appear in the terminal.
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
log.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
log.propagate = False

# ---------- Configuration ----------
CONNECTION_STRING = os.environ.get("SUPABASE_DB_URL")
if not CONNECTION_STRING:
    raise RuntimeError("SUPABASE_DB_URL is not set. Fill in .env first.")
if "YOUR_PASSWORD" in CONNECTION_STRING or "REGION" in CONNECTION_STRING:
    raise RuntimeError("SUPABASE_DB_URL in .env still has placeholder values.")
if not os.getenv("GOOGLE_API_KEY") or "PASTE_YOUR" in os.getenv("GOOGLE_API_KEY", ""):
    raise RuntimeError("GOOGLE_API_KEY in .env is missing. Paste your Gemini API key.")

COLLECTION_NAME = "smartrma_manuals"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"                 # must match ingestion
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
TOP_K = int(os.getenv("TOP_K", "4"))
MAX_DISTANCE = float(os.environ["MAX_DISTANCE"]) if os.getenv("MAX_DISTANCE") else None
SEP = "\n\n---\n\n"

# Manual keys = lower-cased file names used at ingestion (AirdopesApex.txt -> "airdopesapex").
# Aliases are matched against the message with spaces and punctuation removed.
PRODUCT_ALIASES = {
    "airdopesapex": ("airdopesapex", "apex"),
    "stonespark": ("stonespark",),
    "stormcallplus": ("stormcallplus", "stormcall"),
}

# Once the manual is chosen by the metadata filter, the product name in the question only dilutes the
# embedding match (eval: hit@4 97% -> 100%, MRR 0.89 -> 0.97 on the 30-question set), so it is dropped from the search text.
_NAME_RE = re.compile(r"\b(airdopes\s*apex|stone\s*spark|stormcall\s*plus|stormcall|apex)\b", re.I)

NO_ANSWER = ("I could not find this in the PulseWave product manuals, so I will not guess. "
             "Please contact PulseWave support for further help.")


class State(TypedDict):
    messages: Annotated[list, add_messages]
    context: str
    sources: list[str]
    grounded: bool
    product: str | None  # manual key used to filter retrieval, if known
    mode: str            # "ai" | "excerpt" | "no_match" | "crm"


# ---------- Models and vector store ----------
primary = ChatGoogleGenerativeAI(model=GEMINI_MODEL, temperature=0, max_retries=1)
backup = ChatGoogleGenerativeAI(model=FALLBACK_MODEL, temperature=0, max_retries=1)

embeddings = FastEmbedEmbeddings(model_name=EMBED_MODEL, cache_dir=os.getenv("FASTEMBED_CACHE_DIR"))
vectorstore = PGVector(embeddings=embeddings, collection_name=COLLECTION_NAME,
                       connection=CONNECTION_STRING, use_jsonb=True)

support_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are the official customer support AI for PulseWave.
You are polite, professional, and concise. Keep answers under 120 words.
Use short numbered steps for instructions.

Answer using ONLY the product manual context below.
If the context does not contain the answer, do not guess. Politely say you do not have that information.

Retrieved Context:
{context}"""),
    ("placeholder", "{messages}"),
])
chain = support_prompt | primary.with_fallbacks([backup])   # backup model runs if the primary errors or is rate limited


# ---------- Helpers ----------
def _source_label(meta: dict) -> str | None:
    name = meta.get("title") or meta.get("source") or meta.get("file_name")
    if not name:
        return None
    label = os.path.basename(str(name))
    if re.match(r"^tmp\w*", label):          # temp-file names from old ingestion runs are not meaningful
        return None
    page = meta.get("page")
    return f"{label}, p.{int(page) + 1}" if isinstance(page, int) else label


def _excerpt(context: str) -> str:
    chunks = [c.strip() for c in context.split(SEP) if c.strip()][:2]
    body = "\n\n".join(c[:500] + ("…" if len(c) > 500 else "") for c in chunks)
    return "The AI writer is busy right now, so here are the most relevant passages straight from the manual:\n\n" + body


def _detect_product(messages) -> str | None:
    """Which manual is the customer asking about? Product name in the recent chat wins,
    otherwise the product behind a serial number they gave earlier."""
    for text in crm.recent_user_texts(messages, 4):
        norm = re.sub(r"[^a-z0-9]", "", text.lower())
        for key, aliases in PRODUCT_ALIASES.items():
            if any(a in norm for a in aliases):
                return key
    serial = crm.latest_serial(messages)
    return crm.manual_for_serial(serial) if serial and crm.enabled() else None


# ---------- Nodes ----------
def retrieve_node(state: State):
    query = state["messages"][-1].content
    product = _detect_product(state["messages"])
    search_kwargs = {"filter": {"product": {"$eq": product}}} if product else {}

    search_text = query
    if product:
        stripped = " ".join(_NAME_RE.sub(" ", query).split())
        search_text = stripped or query          # never search with an empty string
    results = vectorstore.similarity_search_with_score(search_text, k=TOP_K, **search_kwargs)
    if results:
        # Use these logs to pick MAX_DISTANCE: compare in-scope vs out-of-scope questions.
        log.info("retrieve product=%s best_distance=%.3f", product, results[0][1])
    if MAX_DISTANCE is not None:
        results = [(d, s) for d, s in results if s <= MAX_DISTANCE]

    docs = [d for d, _ in results]
    sources: list[str] = []
    for d in docs:
        label = _source_label(d.metadata or {})
        if label and label not in sources:
            sources.append(label)
    if docs and not sources:
        sources = ["PulseWave product manual"]

    return {"context": SEP.join(d.page_content for d in docs), "sources": sources,
            "grounded": bool(docs), "product": product}


def route_after_retrieve(state: State) -> str:
    return "generate" if state["grounded"] else "fallback"


def generate_node(state: State):
    try:
        response = chain.invoke({"messages": state["messages"], "context": state["context"]})
        return {"messages": [response], "mode": "ai"}
    except Exception:
        log.exception("All LLM calls failed, serving manual excerpt")
        return {"messages": [AIMessage(content=_excerpt(state["context"]))], "mode": "excerpt"}


def fallback_node(state: State):
    return {"messages": [AIMessage(content=NO_ANSWER)], "sources": [], "mode": "no_match"}


def route_start(state: State) -> str:
    # Serial / order / ticket IDs, or "my warranty"-style questions, go to the customer's records.
    return "crm" if crm.wants_crm(state["messages"]) else "retrieve"


def crm_node(state: State):
    reply = crm.handle(state["messages"])
    return {"messages": [AIMessage(content=reply)], "sources": ["PulseWave warranty records"], "mode": "crm"}


# ---------- Graph ----------
workflow = StateGraph(State)
workflow.add_node("retrieve", retrieve_node)
workflow.add_node("generate", generate_node)
workflow.add_node("fallback", fallback_node)
workflow.add_node("crm", crm_node)
workflow.add_conditional_edges(START, route_start, {"crm": "crm", "retrieve": "retrieve"})
workflow.add_edge("crm", END)
workflow.add_conditional_edges("retrieve", route_after_retrieve, {"generate": "generate", "fallback": "fallback"})
workflow.add_edge("generate", END)
workflow.add_edge("fallback", END)
app_graph = workflow.compile()