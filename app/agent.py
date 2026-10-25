"""LangGraph pipeline: retrieve manual passages -> guard -> answer from context only.
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

NO_ANSWER = ("I could not find this in the PulseWave product manuals, so I will not guess. "
             "Please contact PulseWave support for further help.")


class State(TypedDict):
    messages: Annotated[list, add_messages]
    context: str
    sources: list[str]
    grounded: bool
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
    if re.match(r"^tmp\w*", label):          # temp-file names from ingestion are not meaningful
        return None
    page = meta.get("page")
    return f"{label}, p.{int(page) + 1}" if isinstance(page, int) else label


def _excerpt(context: str) -> str:
    chunks = [c.strip() for c in context.split(SEP) if c.strip()][:2]
    body = "\n\n".join(c[:500] + ("…" if len(c) > 500 else "") for c in chunks)
    return "The AI writer is busy right now, so here are the most relevant passages straight from the manual:\n\n" + body


# ---------- Nodes ----------
def retrieve_node(state: State):
    query = state["messages"][-1].content
    results = vectorstore.similarity_search_with_score(query, k=TOP_K)
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

    return {"context": SEP.join(d.page_content for d in docs), "sources": sources, "grounded": bool(docs)}


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
    # A serial number in the message goes to the warranty records, everything else to the manuals.
    return "crm" if crm.enabled() and crm.find_serial(state["messages"][-1].content) else "retrieve"


def crm_node(state: State):
    reply = crm.handle(state["messages"][-1].content)
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
