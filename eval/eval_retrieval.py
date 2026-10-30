"""Retrieval evaluation for PulseWave AI Support.

Run from the project root (needs the same .env as the app):
    python eval/eval_retrieval.py

Reads eval/eval_set.json, a list of objects:
    {"question": "How do I factory reset the Stone Spark?",
     "product": "stonespark",            # manual key, or null if the question names no product
     "must_contain": ["10 seconds"]}     # short phrase (or list of alternatives) that must appear in the right passage
                                         # null = out-of-scope question (should NOT be answerable)
Run eval/validate_eval_set.py first: it checks every phrase against the real manual text.

Reports, for in-scope questions:
    - unfiltered hit rate @k, and how often the hit came from the RIGHT product
    - product-filtered hit rate @k (what the app does when it knows the product)
    - how far down the answer ranks (hit@1, @k, @8, @12 and mean reciprocal rank), and for every miss the rank
      where the answer first appears, so you can tell a near miss from a total miss
    - median retrieval latency
For out-of-scope questions it prints the best vector distance, so you can pick MAX_DISTANCE.
Everything is written to eval/results.json.
"""
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import TOP_K, vectorstore   # noqa: E402  (same store and embeddings as the app)

tests = json.loads((ROOT / "eval" / "eval_set.json").read_text(encoding="utf-8"))


def contains(doc, phrase):
    """phrase is one string or a list of alternatives; any one of them counts."""
    options = [phrase] if isinstance(phrase, str) else phrase
    text = doc.page_content.lower()
    return any(o.lower() in text for o in options)


# Once the product filter is on, the product name in the question adds noise: every chunk is about that product.
NAME_RE = re.compile(r"\b(airdopes\s*apex|stone\s*spark|stormcall\s*plus|stormcall|apex)\b", re.I)


def without_product_name(q):
    return " ".join(NAME_RE.sub(" ", q).split())


in_scope = [t for t in tests if t.get("must_contain")]
out_scope = [t for t in tests if not t.get("must_contain")]

RANK_K = 12      # how deep to look when a question misses
unf_hit = unf_right = 0
wrong_product_slots = total_slots = 0
latencies, best_in, misses, ranks, ranks2 = [], [], [], [], []

for t in in_scope:
    q, prod, phrase = t["question"], t.get("product"), t["must_contain"]

    # Unfiltered: what a question gets when the app cannot tell which product it is about
    start = time.perf_counter()
    res = vectorstore.similarity_search_with_score(q, k=TOP_K)
    latencies.append((time.perf_counter() - start) * 1000)
    docs = [d for d, _ in res]
    if any(contains(d, phrase) for d in docs):
        unf_hit += 1
    if any(contains(d, phrase) and (not prod or d.metadata.get("product") == prod) for d in docs):
        unf_right += 1
    if prod:
        total_slots += len(docs)
        wrong_product_slots += sum(d.metadata.get("product") != prod for d in docs)

    # Filtered: what the app does once it knows the product. Look RANK_K deep to see where the answer sits.
    kwargs = {"filter": {"product": {"$eq": prod}}} if prod else {}
    fres = vectorstore.similarity_search_with_score(q, k=RANK_K, **kwargs)
    rank = next((i for i, (d, _) in enumerate(fres, 1) if contains(d, phrase)), None)
    ranks.append(rank)
    q2 = without_product_name(q)
    if prod and q2 and q2 != q:
        fres2 = vectorstore.similarity_search_with_score(q2, k=RANK_K, **kwargs)
        rank2 = next((i for i, (d, _) in enumerate(fres2, 1) if contains(d, phrase)), None)
    else:
        rank2 = rank
    ranks2.append(rank2)
    if rank is not None and fres:
        best_in.append(fres[0][1])        # only answerable questions set the MAX_DISTANCE floor
    if rank is None or rank > TOP_K:
        where = f"answer first appears at rank {rank}" if rank else f"answer not in the top {RANK_K}"
        misses.append(f"[{prod or 'any'}] {q}  (looking for: {phrase!r}; {where})")

best_out = []
for t in out_scope:
    res = vectorstore.similarity_search_with_score(t["question"], k=1)
    best_out.append((res[0][1] if res else float("inf"), t["question"]))

n = len(in_scope) or 1


def at(k):
    return sum(1 for r in ranks if r and r <= k)


def at2(k):
    return sum(1 for r in ranks2 if r and r <= k)


mrr = sum(1 / r for r in ranks if r) / n
mrr2 = sum(1 / r for r in ranks2 if r) / n
depths = sorted({1, TOP_K, 8, RANK_K})
report = {
    "k": TOP_K,
    "in_scope_questions": len(in_scope),
    "out_of_scope_questions": len(out_scope),
    "unfiltered_hit_rate": round(unf_hit / n, 3),
    "unfiltered_right_product_hit_rate": round(unf_right / n, 3),
    "filtered_hit_rate": round(at(TOP_K) / n, 3),
    "filtered_hit_at": {str(k): round(at(k) / n, 3) for k in depths},
    "filtered_mean_reciprocal_rank": round(mrr, 3),
    "filtered_name_removed_hit_rate": round(at2(TOP_K) / n, 3),
    "filtered_name_removed_mrr": round(mrr2, 3),
    "wrong_product_share_of_unfiltered_results": round(wrong_product_slots / total_slots, 3) if total_slots else None,
    "retrieval_latency_ms_median": round(statistics.median(latencies), 1) if latencies else None,
    "retrieval_latency_ms_p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1) if latencies else None,
}

print(f"\nIn-scope questions: {len(in_scope)}   Out-of-scope: {len(out_scope)}   k={TOP_K}")
print(f"Unfiltered hit rate @{TOP_K}:              {unf_hit}/{len(in_scope)} = {report['unfiltered_hit_rate']:.0%}")
print(f"  ...with the right product:          {unf_right}/{len(in_scope)} = {report['unfiltered_right_product_hit_rate']:.0%}")
if total_slots:
    print(f"  wrong-product share of results:     {report['wrong_product_share_of_unfiltered_results']:.0%}")
print("Product-filtered hit rate (what the app does once it knows the product):")
for k in depths:
    print(f"   hit@{k:<2}  {at(k)}/{len(in_scope)} = {at(k) / n:.0%}")
print(f"   mean reciprocal rank: {mrr:.2f}")
print(f"   same search with the product name removed from the question: hit@{TOP_K} {at2(TOP_K)}/{len(in_scope)} = {at2(TOP_K) / n:.0%}   (mean reciprocal rank {mrr2:.2f})")
if latencies:
    print(f"Retrieval latency median / p95:       {report['retrieval_latency_ms_median']:.0f} ms / {report['retrieval_latency_ms_p95']:.0f} ms")

if best_out and best_in:
    worst_in, closest_out = max(best_in), min(d for d, _ in best_out)
    report.update({"answerable_questions_used_for_threshold": len(best_in),
                   "max_best_distance_in_scope": round(worst_in, 4), "min_best_distance_out_of_scope": round(closest_out, 4)})
    print(f"\nBest-match distance, answerable in-scope questions (worst case): {worst_in:.3f}   [{len(best_in)} of {len(in_scope)} questions]")
    print(f"Best-match distance, out-of-scope questions (closest):          {closest_out:.3f}")
    if worst_in < closest_out:
        suggested = round((worst_in + closest_out) / 2, 3)
        report["suggested_MAX_DISTANCE"] = suggested
        refused = sum(d > suggested for d, _ in best_out)
        print(f"-> Clean gap. Try MAX_DISTANCE={suggested} in .env  (refuses {refused}/{len(best_out)} out-of-scope, keeps all {len(best_in)} answerable)")
    else:
        print("-> The ranges overlap, so a single threshold cannot separate them. Add more out-of-scope questions or rely on the prompt-level refusal.")
    for d, q in sorted(best_out):
        print(f"   {d:.3f}  {q}")

if misses:
    print(f"\nFiltered misses at k={TOP_K} (read these: bad test case, wording, or chunking?):")
    for m in misses:
        print("  -", m)

(ROOT / "eval" / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("\nSaved eval/results.json")