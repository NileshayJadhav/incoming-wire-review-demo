"""Local inventory/review commands. Credentials are read only from the environment."""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a separate incoming-wire draft from local sources.")
    parser.add_argument("action", choices=("inventory", "review"))
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--documents", required=True)
    parser.add_argument("--samples", default="all")
    parser.add_argument("--rules", required=True, help="Explicit applicable reviewer rules JSON.")
    parser.add_argument("--mapping", default="")
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--writer", choices=("artifact", "portable"), default=os.environ.get("IWT_WRITER", "portable"))
    args = parser.parse_args(argv)
    from . import engine
    if args.action == "inventory":
        result = engine.inventory_batch(args.workbook, args.documents, args.samples, args.rules, args.mapping)
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            parser.error("Configure OPENAI_API_KEY privately in your process environment; never put it in a command argument.")
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(model=args.model, api_key=key, temperature=0, timeout=120, max_retries=1, max_tokens=12000)
        os.environ["IWT_WRITER"] = args.writer
        def progress(event):
            # Do not print evidence/customer values or model/provider errors.
            safe = {k: event[k] for k in ("stage", "completed_samples", "total_samples", "elapsed_seconds") if k in event}
            print(json.dumps(safe), file=sys.stderr, flush=True)
        result = engine.review_batch(args.workbook, args.documents, args.samples, args.rules, args.mapping,
                                    model=model, model_name=args.model, workers=args.workers, progress=progress)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
