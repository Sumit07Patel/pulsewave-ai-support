"""Check eval/eval_set.json against the ACTUAL manual text before you trust any number from it.

    python eval/validate_eval_set.py                    # checks the chunks stored in Supabase (needs .env)
    python eval/validate_eval_set.py --local manuals    # checks local files named like AirdopesApex.txt

For every in-scope question it checks that at least one of the must_contain phrases really occurs in that
product's manual. The draft phrases are educated guesses, so expect some to be reported as MISSING:
open the manual, find the real wording, and fix the phrase in eval_set.json.
Also reports schema problems and duplicate questions. Exit code 1 if anything needs fixing.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--local", metavar="DIR", help="read manuals from this folder instead of Supabase")
ap.add_argument("--set", default=str(ROOT / "eval" / "eval_set.json"))
args = ap.parse_args()

tests = json.loads(Path(args.set).read_text(encoding="utf-8"))

# ---------- load manual text: product key -> list of text units (chunks, or whole files) ----------
units: dict[str, list[str]] = {}
if args.local:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    CHUNK_SIZE, CHUNK_OVERLAP = 1000, 100            # keep in sync with ingest.py
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    for f in sorted(Path(args.local).glob("*")):
        if f.suffix.lower() in {".txt", ".md"}:
            text = f.read_text(encoding="utf-8", errors="replace")
            units[f.stem.lower()] = [c.lower() for c in splitter.split_text(text)]
    source = f"local files in {args.local}, split like ingest.py"
else:
    from dotenv import load_dotenv
    from sqlalchemy import create_engine, text
    load_dotenv()
    engine = create_engine(os.environ["SUPABASE_DB_URL"])
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT e.cmetadata->>'product' AS product, e.document FROM langchain_pg_embedding e "
            "JOIN langchain_pg_collection c ON c.uuid = e.collection_id WHERE c.name = :c"), {"c": "smartrma_manuals"}).all()
    for product, doc in rows:
        units.setdefault(product, []).append((doc or "").lower())
    source = "chunks stored in Supabase"
if not units:
    sys.exit("No manual text found. Run ingest.py first, or point --local at a folder of .txt files.")
print(f"Checking against {source}: " + ", ".join(f"{p} ({len(u)} chunks)" for p, u in sorted(units.items())))


def options(value):
    return [value] if isinstance(value, str) else list(value)


def found_in(product, opts):
    return [o for o in opts if any(o.lower() in u for u in units.get(product, []))]


problems, missing, seen = [], [], set()
n_in = n_out = n_ok = 0
for i, t in enumerate(tests):
    tag = t.get("id") or f"#{i + 1}"
    q = t.get("question")
    if not isinstance(q, str) or not q.strip():
        problems.append(f"{tag}: missing question")
        continue
    key = (q.strip().lower(), t.get("product"))          # the same wording is fine for different products
    if key in seen:
        problems.append(f"{tag}: duplicate question for the same product: {q!r}")
    seen.add(key)
    must = t.get("must_contain")
    if must is None:
        n_out += 1
        continue
    n_in += 1
    if not (isinstance(must, str) and must.strip()) and not (isinstance(must, list) and must and all(isinstance(x, str) and x.strip() for x in must)):
        problems.append(f"{tag}: must_contain should be a string, a list of strings, or null")
        continue
    product = t.get("product")
    if product not in units:
        problems.append(f"{tag}: product {product!r} is not one of {sorted(units)}")
        continue
    opts = options(must)
    hits = found_in(product, opts)
    if hits:
        n_ok += 1
        continue
    elsewhere = sorted(p for p in units if p != product and found_in(p, opts))
    missing.append((tag, q, opts, product, elsewhere))

print(f"\n{n_in} in-scope questions, {n_out} out-of-scope")
print(f"{n_ok}/{n_in} in-scope questions have a must_contain phrase that really occurs in their manual")
if missing:
    print(f"\nMISSING ({len(missing)}): none of these phrases occur in the product's manual. Fix the wording in eval_set.json:")
    for tag, q, opts, product, elsewhere in missing:
        extra = f"   (but found in: {', '.join(elsewhere)}, is the product tag wrong?)" if elsewhere else ""
        print(f"  - {tag} [{product}] {q}\n      tried: {opts}{extra}")
if problems:
    print(f"\nPROBLEMS ({len(problems)}):")
    for p in problems:
        print("  -", p)
if not missing and not problems:
    print("\nAll good. Remember the phrases only prove the answer EXISTS in the manual; read a few questions yourself too.")
sys.exit(1 if (missing or problems) else 0)