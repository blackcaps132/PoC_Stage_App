"""Check the configured live model chain without replaying or publishing actions."""

import argparse
import json
import time
from pathlib import Path

from .provider import Budget, DependencyFailure, create_provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/agent.json"
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config.get("provider") not in ("models", "groq", "gemini"):
        parser.error("Smoke check requires live model configuration")
    provider = None
    started = time.monotonic()
    try:
        provider = create_provider(config)
        result = provider.propose(
            {"event": "app_open"},
            [
                {
                    "goal_id": "G01",
                    "context": {},
                    "evidence_refs": [],
                    "reason": "Synthetic connection check: choose this eligible goal.",
                }
            ],
            ["TEST-TITLE-1"],
            Budget(config),
        )
        if result["goal_id"] != "G01" or result["title_ids"] != ["TEST-TITLE-1"]:
            raise DependencyFailure("smoke_response_did_not_select_test_candidate")
        print(
            json.dumps(
                {
                    "success": True,
                    "provider": result.get("provider_used", provider.name),
                    "model": result.get("model_used", getattr(provider, "model", None)),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "proposal": result,
                },
                indent=2,
            )
        )
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Model smoke check failed: {error}\n")
    finally:
        if provider is not None:
            provider.close()


if __name__ == "__main__":
    main()
