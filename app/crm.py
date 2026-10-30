"""Warranty lookup, RMA tickets and ticket status backed by Supabase tables (see db/schema.sql).
Deterministic on purpose: no LLM decides who gets a ticket.

Handles, across several chat turns:
  - warranty check by serial number or order ID
  - RMA ticket creation (needs BOTH serial and order ID, which acts as a simple ownership check)
  - ticket status by ticket ID
"""
import logging
import os
import re
import secrets
from datetime import date

log = logging.getLogger("pulsewave")

SERIAL_RE = re.compile(r"\bPW-[A-Z]{3}-\d{5}\b", re.I)
ORDER_RE = re.compile(r"\bORD-\d{6}\b", re.I)
TICKET_RE = re.compile(r"\bRMA-\d{4}-[0-9A-F]{6}\b", re.I)
STATUS_RE = re.compile(r"\b(status|track|tracking|progress|update on)\b", re.I)
RMA_INTENT = re.compile(r"\b(rma|return|replace|replacement|claim|refund|defect\w*|faulty|broken|repair)\b", re.I)

# "my warranty", "I want to return ..." = a question about the customer's own records.
# "What is the warranty period?" has no "my", so it still goes to the manuals.
ACCOUNT_RE = re.compile(
    r"\bmy\b.{0,40}\b(warranty|order|purchase|ticket|rma|return|refund|replacement)\b"
    r"|\b(i want|i need|i'd like|i would like)\b.{0,30}\b(return|refund|replace\w*|rma|claim)\b",
    re.I,
)

ASK_ID = ("To check your warranty, please send your **serial number** (like `PW-APX-10001`, printed on the "
          "earbud case) or your **order ID** (like `ORD-100234`).")
RMA_MARKER = "To open a return"      # also used to detect that the previous bot reply asked for return details
ASK_RMA = (f"{RMA_MARKER}, I need both your **serial number** and your **order ID**, for example: "
           "`RMA PW-APX-10001 ORD-100234`. If you already sent one of them, just send the other.")
DB_DOWN = "The warranty system is not reachable right now. Please try again in a minute."

_client = None


def enabled() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY")
                and "YOUR" not in os.getenv("SUPABASE_URL", "") and "PASTE" not in os.getenv("SUPABASE_KEY", ""))


def _db():
    global _client
    if _client is None:
        from supabase import create_client      # imported lazily so the app starts without it
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _client


# ---------- Reading the conversation ----------
def _text(m) -> str:
    return m.content if isinstance(m.content, str) else str(m.content)


def recent_user_texts(messages, n: int = 4) -> list[str]:
    """Text of the last n user messages, newest first."""
    texts = []
    for m in reversed(messages):
        if m.type == "human":
            texts.append(_text(m))
            if len(texts) == n:
                break
    return texts


def _find(rx: re.Pattern, texts: list[str]) -> str | None:
    """First match scanning newest to oldest, so the latest value wins."""
    for t in texts:
        m = rx.search(t)
        if m:
            return m.group(0).upper()
    return None


def latest_serial(messages) -> str | None:
    return _find(SERIAL_RE, recent_user_texts(messages))


def _pending_rma(messages) -> bool:
    """True if the bot's previous reply was asking for return details."""
    for m in reversed(messages[:-1]):
        if m.type == "ai":
            return RMA_MARKER in _text(m)
    return False


def RMA_MARKER_IN(messages) -> bool:
    return _pending_rma(messages)


def wants_crm(messages) -> bool:
    """Router check: is the newest message about the customer's own records?"""
    if not enabled():
        return False
    latest = (recent_user_texts(messages, 1) or [""])[0]
    return any(rx.search(latest) for rx in (SERIAL_RE, ORDER_RE, TICKET_RE, ACCOUNT_RE))


# ---------- Database helpers ----------
def _fmt(d: str) -> str:
    return date.fromisoformat(d[:10]).strftime("%d %b %Y")


def _lookup(serial: str | None, order: str | None) -> list[dict]:
    q = _db().table("warranty_lookup").select("*")
    q = q.eq("serial_number", serial) if serial else q.eq("order_id", order)
    return q.limit(5).execute().data or []


def manual_for_serial(serial: str) -> str | None:
    """Which manual (products.manual_product) belongs to this serial number."""
    try:
        rows = (_db().table("purchases").select("products(manual_product)")
                .eq("serial_number", serial).limit(1).execute().data)
        return ((rows[0].get("products") or {}).get("manual_product")) if rows else None
    except Exception:
        log.exception("manual_for_serial failed")
        return None


def _ticket_status(ticket_id: str) -> str:
    rows = (_db().table("rma_tickets").select("ticket_id,status,created_at")
            .eq("ticket_id", ticket_id).limit(1).execute().data)
    if not rows:
        return f"I could not find return ticket **{ticket_id}**. Please check the number and try again."
    r = rows[0]
    return f"Return ticket **{r['ticket_id']}** is **{r['status'].upper()}** (opened {_fmt(r['created_at'])})."


# ---------- Main entry ----------
def handle(messages) -> str:
    texts = recent_user_texts(messages)
    latest = texts[0] if texts else ""
    serial, order = _find(SERIAL_RE, texts), _find(ORDER_RE, texts)
    ticket = TICKET_RE.search(latest)
    wants_rma = bool(RMA_INTENT.search(latest)) or _pending_rma(messages)

    # A plain warranty question about the ID in the NEWEST message must not be answered for an older ID:
    # "PW-APX-10001" followed by "what about ORD-100235?" is a new lookup. A return needs both IDs, so it
    # still combines them across messages.
    if not wants_rma:
        in_serial, in_order = SERIAL_RE.search(latest), ORDER_RE.search(latest)
        if in_order and not in_serial:
            serial = None
        elif in_serial and not in_order:
            order = None

    try:
        if ticket:
            return _ticket_status(ticket.group(0).upper())
        if not (serial or order):
            return ASK_RMA if wants_rma else ASK_ID

        if serial and STATUS_RE.search(latest) and not RMA_MARKER_IN(messages):
            rows = (_db().table("rma_tickets").select("ticket_id")
                    .eq("serial_number", serial).eq("status", "open").limit(1).execute().data)
            if rows:
                return _ticket_status(rows[0]["ticket_id"])

        rows = _lookup(serial, order)
        if not rows:
            return (f"I could not find **{serial or order}**. Please check the number printed on the "
                    "earbud case or your invoice and try again.")
        if len(rows) > 1:
            names = ", ".join(sorted({r["product_name"] for r in rows}))
            return (f"Order **{order}** includes several devices ({names}). "
                    "Please send the serial number printed on the device you mean.")

        row = rows[0]
        name, end, active = row["product_name"], _fmt(row["warranty_end"]), row["warranty_active"]
        label = f"{name} ({serial})" if serial else name     # never reveal a serial the user did not provide

        if not wants_rma:
            head = f"**{label}**, bought on {_fmt(row['purchase_date'])}.\n\n"
            if active:
                return head + (f"Warranty is **ACTIVE** until **{end}**.\n\n"
                               "To start a return, send: `RMA <serial number> <order ID>`.")
            return head + f"The warranty **expired on {end}**. Please contact PulseWave support about paid repair options."

        if not active:
            return f"The warranty for {label} expired on {end}, so I cannot open a free return ticket."
        if not (serial and order):
            return ASK_RMA
        if order != row["order_id"]:
            return "That order ID does not match this serial number. Please check your invoice and try again."

        existing = (_db().table("rma_tickets").select("ticket_id").eq("serial_number", serial)
                    .eq("status", "open").limit(1).execute().data)
        if existing:
            return f"A return ticket is already open for {serial}: **{existing[0]['ticket_id']}**."

        ticket_id = f"RMA-{date.today():%y%m}-{secrets.token_hex(3).upper()}"
        issue = " | ".join(reversed(texts))[:300]
        _db().table("rma_tickets").insert({"ticket_id": ticket_id, "serial_number": serial, "issue": issue}).execute()
        return (f"Done. Your return ticket is **{ticket_id}** for {name} ({serial}).\n\n"
                "Keep this number, you can ask me for its status any time. "
                "Our team will contact you with shipping instructions.")
    except Exception:
        log.exception("CRM lookup failed")
        return DB_DOWN