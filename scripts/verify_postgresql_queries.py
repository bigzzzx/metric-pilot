"""Run the real PostgreSQL golden suite and optionally the configured LLM graph."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.evaluation.postgresql_runner import (
    run_direct_acceptance,
    run_model_acceptance,
    run_stability_acceptance,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id")
    parser.add_argument("--llm", action="store_true", help="also run ten full LangGraph/model cases")
    parser.add_argument("--stability", action="store_true", help="also run paraphrase, follow-up, ambiguity and permission cases")
    parser.add_argument("--output", default=".data/evals/postgresql-latest.json")
    options = parser.parse_args()

    direct = run_direct_acceptance(options.source_id)
    model = asyncio.run(run_model_acceptance(options.source_id)) if options.llm else None
    stability = asyncio.run(run_stability_acceptance(options.source_id)) if options.stability else None
    report = {"direct": direct, "model": model, "stability": stability}
    output = Path(options.output)
    if not output.is_absolute():
        output = Path(__file__).resolve().parents[1] / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output}")

    failed = direct["failed"] + direct["errors"]
    if model:
        failed += model["failed"] + model["errors"]
    if stability:
        failed += stability["failed"] + stability["errors"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
