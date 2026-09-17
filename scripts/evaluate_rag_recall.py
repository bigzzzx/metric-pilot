"""Measure current permission-scoped RAG recall against hand-reviewed evidence IDs."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.catalog.rag_documents import authorized_catalog_documents
from backend.core import db
from backend.knowledge_base import _authorized_chunks
from backend.rag import retrieve

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "backend/evaluation/data/rag_recall_v1.json"
DEFAULT_OUTPUT = ROOT / ".data/evals/rag-recall-current.json"


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 2)


def score_case(case: dict, ids: list[str], k: int) -> dict:
    observed = set(ids[:k])
    groups = case["required_any_of"]
    missing = [group for group in groups if not observed.intersection(group)]
    return {
        "found": len(groups) - len(missing),
        "total": len(groups),
        "recall": round((len(groups) - len(missing)) / len(groups), 4),
        "complete": not missing,
        "missing_groups": missing,
    }


def run(dataset: dict, limit: int) -> dict:
    connection = db()
    try:
        usernames = {row["username"]: row["id"] for row in connection.execute("SELECT id,username FROM users")}
    finally:
        connection.close()
    cases = [*dataset["cases"], *dataset["negative_cases"]]
    requested_users = {case.get("username", dataset["default_username"]) for case in cases}
    missing_users = requested_users - usernames.keys()
    if missing_users:
        raise ValueError(f"Unknown evaluation usernames: {sorted(missing_users)}")
    visible = {
        name: {item["id"] for item in authorized_catalog_documents(usernames[name])}
        | {item["id"] for item in _authorized_chunks(usernames[name])}
        for name in requested_users
    }
    for case in dataset["cases"]:
        available = visible[case.get("username", dataset["default_username"])]
        for group in case["required_any_of"]:
            if not available.intersection(group):
                raise ValueError(f"Stale or inaccessible gold evidence in {case['id']}: {group}")
        if not case.get("expected_fact"):
            raise ValueError(f"Missing expected_fact in {case['id']}")
    results = []
    for number, case in enumerate(cases, 1):
        username = case.get("username", dataset["default_username"])
        start = time.perf_counter()
        print(f"[{number}/{len(cases)}] {case['id']}", flush=True)
        try:
            rows = retrieve(case["query"], limit=limit, fallback=False, user_id=usernames[username])
            ids = [str(item["id"]) for item in rows]
            elapsed = round((time.perf_counter() - start) * 1000, 2)
            record = {"id": case["id"], "category": case["category"], "username": username,
                      "query": case["query"], "expected_fact": case["expected_fact"], "elapsed_ms": elapsed, "retrieved_ids": ids,
                      "unauthorized_ids": [item for item in ids if item not in visible[username]]}
            if case in dataset["cases"]:
                record["at_5"] = score_case(case, ids, 5)
                record["at_12"] = score_case(case, ids, 12)
            else:
                forbidden = set(case.get("forbidden_ids", []))
                record["forbidden_hits"] = [item for item in ids if item in forbidden]
                if case.get("expected_outcome") == "no_supported_evidence":
                    record["no_answer_review"] = "empty" if not ids else "nonempty_needs_manual_review"
            results.append(record)
        except Exception as exc:
            results.append({"id": case["id"], "category": case["category"], "error": f"{type(exc).__name__}: {exc}"})
    positive = [item for item in results if "at_12" in item]
    negative = [item for item in results if "forbidden_hits" in item]
    summary = {"positive_total": len(dataset["cases"]), "positive_completed": len(positive),
               "negative_total": len(dataset["negative_cases"]), "negative_completed": len(negative),
               "errors": [item for item in results if "error" in item]}
    for k in (5, 12):
        key = f"at_{k}"
        total = sum(item[key]["total"] for item in positive)
        found = sum(item[key]["found"] for item in positive)
        summary[f"recall_at_{k}"] = round(found / total, 4) if total else None
        summary[f"complete_at_{k}"] = round(sum(item[key]["complete"] for item in positive) / len(positive), 4) if positive else None
    timings = [item["elapsed_ms"] for item in results if "elapsed_ms" in item]
    summary["latency_ms_p50"] = percentile(timings, .5)
    summary["latency_ms_p95"] = percentile(timings, .95)
    summary["permission_leaks"] = [item["id"] for item in results if item.get("forbidden_hits") or item.get("unauthorized_ids")]
    summary["missing_at_12"] = [item["id"] for item in positive if not item["at_12"]["complete"]]
    summary["by_category"] = {}
    for category in sorted({item["category"] for item in positive}):
        subset = [item for item in positive if item["category"] == category]
        required = sum(item["at_12"]["total"] for item in subset)
        found = sum(item["at_12"]["found"] for item in subset)
        summary["by_category"][category] = {"cases": len(subset), "recall_at_12": round(found / required, 4)}
    return {"dataset": dataset["name"], "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "entrypoint": "backend.rag.retrieve", "limit": limit, "summary": summary, "cases": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if args.limit < 12:
        parser.error("--limit must be at least 12 for Recall@12")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    report = run(dataset, args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"Report: {args.output}", flush=True)
    return 1 if report["summary"]["errors"] or report["summary"]["permission_leaks"] else 0


if __name__ == "__main__":
    raise SystemExit(main())