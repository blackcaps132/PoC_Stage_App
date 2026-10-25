"""Groq structured proposals; one bounded HTTP call with no automatic retries."""

import json
import os
from pathlib import Path

from .gemini import SYSTEM_INSTRUCTION
from .provider import (
    DependencyFailure,
    ModelProvider,
    proposal_schema,
    retry_after_seconds,
    validate_model_proposal,
)


class GroqProvider(ModelProvider):
    name = "groq"

    def __init__(self, config, client=None):
        import httpx
        from dotenv import load_dotenv

        super().__init__()
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        self.key = os.environ.get("GROQ_API_KEY")
        if not self.key:
            raise ValueError("Set GROQ_API_KEY before using Groq")
        self.model = config.get("model", "openai/gpt-oss-20b")
        self.timeout = float(config.get("model_timeout_seconds", 20))
        if not 0 < self.timeout < config["max_decision_seconds"]:
            raise ValueError(
                "model_timeout_seconds must be positive and below max_decision_seconds"
            )
        self.client = client or httpx.Client()

    def propose(self, trigger, candidates, title_ids, budget, incident=None, correction=None):
        import httpx

        budget.check()
        if not candidates:
            return {"goal_id": None, "title_ids": [], "reason": "No evidence-backed candidate"}
        if budget.model_calls >= budget.config["max_model_calls"]:
            raise DependencyFailure("model_call_budget_exceeded")
        budget.model_calls += 1
        if incident:
            raise DependencyFailure("model_provider_HTTP_503")
        timeout = min(self.timeout, budget.config["max_decision_seconds"] - budget.elapsed())
        try:
            response = self.client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.key},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_INSTRUCTION},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "trigger_type": trigger["event"],
                                    "candidates": candidates,
                                    "title_ids": title_ids,
                                    **(
                                        {"correction": correction} if correction is not None else {}
                                    ),
                                }
                            ),
                        },
                    ],
                    "max_completion_tokens": 2048,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "proposal",
                            "strict": True,
                            "schema": proposal_schema(candidates, title_ids),
                        },
                    },
                },
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise DependencyFailure(
                    f"groq_request_failed_HTTP_{response.status_code}",
                    retry_after_seconds(response.headers),
                )
            output = response.json()["choices"][0]["message"]["content"]
            budget.check()
            return validate_model_proposal(output, candidates, title_ids)
        except DependencyFailure:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise DependencyFailure("groq_request_failed_" + type(error).__name__) from None

    def close(self):
        self.client.close()
