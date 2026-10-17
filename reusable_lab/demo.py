"""Offline fictional workflow demo. This mode uses a deterministic fixture parser."""
from __future__ import annotations

import json
import os
from pathlib import Path


def main():
    from . import engine
    try:
        from .demo_extractor import fixture_extractor
    except ImportError:  # Original lab; the release builder extracts this function.
        from .test_engine import fixture_extractor
    lab = Path(__file__).resolve().parent
    fixture = lab / "test_fixtures" / "batch_b"
    rules = lab / "test_fixtures" / "fictional_2026_rules.json"
    os.environ.setdefault("IWT_WRITER", "portable")
    result = engine.review_batch(str((fixture / "Incoming_Wire_Batch_B.xlsx").resolve()),
                                str((fixture / "evidence").resolve()), "ALPHA-17,OMEGA-3", str(rules.resolve()),
                                extractor=fixture_extractor, workers=2, model_name="fictional_parser_no_model")
    result["demonstration_mode"] = "OFFLINE_DETERMINISTIC_FICTIONAL_ONLY"
    result["live_model_evaluated"] = False
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
