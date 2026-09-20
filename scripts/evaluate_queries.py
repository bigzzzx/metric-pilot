"""python scripts/evaluate_queries.py [--mode live] [--cases gmv,followup]."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.evaluation.environment import evaluation_environment
from backend.evaluation.runner import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--cases", default="")
    parser.add_argument("--output", default=".data/evals/latest.json")
    parser.add_argument("--no-cache", action="store_true", help="disable agent route/plan/answer caches")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (root / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="query-eval-", dir=output.parent) as temporary:
        with evaluation_environment(Path(temporary)):
            report = asyncio.run(evaluate(
                args.mode,
                set(args.cases.split(",")) if args.cases else None,
                no_cache=args.no_cache,
            ))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    return 1 if report["failed"] or report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
