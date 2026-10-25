"""Warranty lookup and RMA tickets backed by Supabase tables (see db/schema.sql).
Deterministic on purpose: no LLM decides who gets a ticket."""
import logging
import os
import re
import secrets
from datetime import date

log = logging.getLogger("pulsewave")

SERIAL_RE = re.compile(r"\bPW-[A-Z]{3}-\d{5}\b", re.I)
ORDER_RE = re.compile(r"\bORD-\d{6}\b", re.I)
RMA_INTENT = re.compile(r"\b(rma|return|replace|replacement|claim|refund|defect\w*|faulty|broken|repair)\b", re.I)

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


def find_serial(text: str) -> str | None:
    m = SERIAL_RE.search(text)
    return m.group(0).upper() if m else None


def _fmt(d: str) -> str:
    return date.fromisoformat(d).strftime("%d %b %Y")


def handle(message: str) -> str:
    serial = find_serial(message)
    try:
        row = (_db().table("warranty_lookup").select("*").eq("serial_number", serial).limit(1).execute().data or [None])[0]
        if not row:
            return (f"I could not find serial number **{serial}**. Please check the number printed on the "
                    "earbud case or your invoice and try again.")

        name, end, active = row["product_name"], _fmt(row["warranty_end"]), row["warranty_active"]
        wants_rma = bool(RMA_INTENT.search(message))

        if not wants_rma:
            if active:
                return (f"**{name}** ({serial}) bought on {_fmt(row['purchase_date'])}.\n\n"
                        f"Warranty is **ACTIVE** until **{end}**.\n\n"
                        f"To start a return, send: `RMA {serial} ORD-XXXXXX` with your order ID.")
            return (f"**{name}** ({serial}): the warranty **expired on {end}**. "
                    "Please contact PulseWave support about paid repair options.")

        if not active:
            return f"The warranty for {serial} expired on {end}, so I cannot open a free return ticket."

        order = ORDER_RE.search(message)
        if not order:
            return f"To open a return for {serial}, please also send your order ID, for example: `RMA {serial} ORD-123456`."
        if order.group(0).upper() != row["order_id"]:
            return "That order ID does not match this serial number. Please check your invoice and try again."

        existing = (_db().table("rma_tickets").select("ticket_id").eq("serial_number", serial)
                    .eq("status", "open").limit(1).execute().data)
        if existing:
            return f"A return ticket is already open for {serial}: **{existing[0]['ticket_id']}**."

        ticket = f"RMA-{date.today():%y%m}-{secrets.token_hex(3).upper()}"
        _db().table("rma_tickets").insert({"ticket_id": ticket, "serial_number": serial, "issue": message[:300]}).execute()
        return (f"Done. Your return ticket is **{ticket}** for {name} ({serial}).\n\n"
                "Keep this number. Our team will contact you with shipping instructions.")
    except Exception:
        log.exception("CRM lookup failed")
        return "The warranty system is not reachable right now. Please try again in a minute."
