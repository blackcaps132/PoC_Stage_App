"""Provider boundary tests without network access or credentials."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.gemini import GeminiProvider
from src.provider import Budget, DependencyFailure, create_provider


class GeminiTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(max_decision_seconds=60, max_model_calls=2, max_tool_calls=20)
        self.client = Mock()
        self.provider = GeminiProvider(self.config, client=self.client)
        self.candidates = [{"goal_id": "G01", "context": {}, "evidence_refs": []}]

    def propose(self, text):
        self.client.interactions.create.return_value = SimpleNamespace(
            output_text=text, status="completed"
        )
        return self.provider.propose(
            {"event": "app_open"}, self.candidates, ["T1"], Budget(self.config)
        )

    def test_valid_response_and_bounded_request(self):
        self.assertEqual(self.propose('{"goal_id":"G01","title_ids":["T1"]}')["goal_id"], "G01")
        request = self.client.interactions.create.call_args.kwargs
        self.assertLessEqual(request["timeout"], 20)
        self.assertFalse(request["store"])
        self.assertEqual(request["response_format"]["mime_type"], "application/json")

    def test_invalid_outputs_fail_closed(self):
        for text in [
            "bad",
            "{}",
            '{"goal_id":"G04","title_ids":[]}',
            '{"goal_id":"G01","title_ids":["invented"]}',
        ]:
            with self.subTest(text=text), self.assertRaises(DependencyFailure):
                self.propose(text)

    def test_errors_do_not_expose_credentials(self):
        self.client.interactions.create.side_effect = RuntimeError("secret-key")
        with self.assertRaisesRegex(DependencyFailure, "^gemini_request_failed_RuntimeError$"):
            self.propose("{}")

    def test_empty_candidates_skip_network(self):
        result = self.provider.propose({"event": "app_open"}, [], [], Budget(self.config))
        self.assertIsNone(result["goal_id"])
        self.client.interactions.create.assert_not_called()

    def test_budget_prevents_network(self):
        budget = Budget(self.config, model_calls=2)
        with self.assertRaisesRegex(DependencyFailure, "model_call_budget_exceeded"):
            self.provider.propose({"event": "app_open"}, self.candidates, ["T1"], budget)
        self.client.interactions.create.assert_not_called()

    def test_missing_key_is_configuration_error(self):
        with patch("dotenv.load_dotenv"), patch.dict(
            "os.environ", {}, clear=True
        ), self.assertRaisesRegex(ValueError, "Set GOOGLE_API_KEY"):
            create_provider({**self.config, "provider": "gemini"})

    def test_incomplete_response_fails_closed(self):
        self.client.interactions.create.return_value = SimpleNamespace(
            status="in_progress", output_text="{}"
        )
        with self.assertRaisesRegex(DependencyFailure, "gemini_response_incomplete"):
            self.provider.propose(
                {"event": "app_open"}, self.candidates, ["T1"], Budget(self.config)
            )

    def test_http_status_is_logged_without_error_body(self):
        error = RuntimeError("secret-key")
        error.status_code = 429
        self.client.interactions.create.side_effect = error
        with self.assertRaisesRegex(
            DependencyFailure, "^gemini_request_failed_RuntimeError_HTTP_429$"
        ):
            self.propose("{}")


class RouterTests(unittest.TestCase):
    def setUp(self):
        sleep = patch("src.provider.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.config = {
            "max_decision_seconds": 60,
            "max_model_calls": 8,
            "max_tool_calls": 20,
            "provider": "models",
            "providers": [
                {"provider": "groq", "model": "first"},
                {"provider": "gemini", "model": "second"},
            ],
        }
        self.candidates = [{"goal_id": "G01"}]

    def test_second_model_used_when_first_hits_quota(self):
        from src.provider import ModelRouter

        first, second = Mock(), Mock()
        first.propose.side_effect = DependencyFailure("groq_request_failed_HTTP_429")
        second.propose.return_value = {"goal_id": "G01", "title_ids": ["T1"]}
        router = ModelRouter(self.config)
        with patch("src.provider.create_live_provider", side_effect=[first, second]):
            output = router.propose({}, self.candidates, ["T1"], Budget(self.config))
            self.assertEqual(output["provider_used"], "gemini")
            router.propose({}, self.candidates, ["T1"], Budget(self.config))
        self.assertEqual(first.propose.call_count, 1)
        self.assertEqual(second.propose.call_count, 2)

    def test_all_failures_stop_without_local_proposal(self):
        from src.provider import ModelRouter, ModelsUnavailable

        model = Mock()
        model.propose.side_effect = DependencyFailure("request_failed_HTTP_429")
        with patch("src.provider.create_live_provider", return_value=model):
            with self.assertRaises(ModelsUnavailable) as error:
                ModelRouter(self.config).propose({}, self.candidates, ["T1"], Budget(self.config))
        self.assertEqual(len(error.exception.attempts), 2)

    def test_missing_keys_are_reported_without_secrets(self):
        from src.provider import ModelRouter, ModelsUnavailable

        with patch("src.provider.create_live_provider", side_effect=ValueError("secret-key")):
            with self.assertRaises(ModelsUnavailable) as error:
                ModelRouter(self.config).propose({}, self.candidates, ["T1"], Budget(self.config))
        self.assertNotIn("secret-key", str(error.exception.attempts))

    def test_invalid_proposal_tries_next_model(self):
        from src.provider import ModelRouter

        first, second = Mock(), Mock()
        first.propose.return_value = {"goal_id": "invented", "title_ids": []}
        second.propose.return_value = {"goal_id": "G01", "title_ids": ["T1"]}
        with patch("src.provider.create_live_provider", side_effect=[first, second]):
            output = ModelRouter(self.config).propose(
                {}, self.candidates, ["T1"], Budget(self.config)
            )
        self.assertEqual(output["provider_used"], "gemini")

    def test_router_rejects_local_provider(self):
        from src.provider import ModelRouter

        self.config["providers"][0]["provider"] = "replay"
        with self.assertRaises(ValueError):
            ModelRouter(self.config)


class GroqTests(unittest.TestCase):
    def test_structured_output_and_sanitized_quota_error(self):
        import os

        from src.groq import GroqProvider

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-secret"}):
            client = Mock()
            provider = GroqProvider({"max_decision_seconds": 60}, client=client)
        client.post.return_value.status_code = 429
        config = {"max_decision_seconds": 60, "max_model_calls": 8, "max_tool_calls": 20}
        with self.assertRaisesRegex(DependencyFailure, "^groq_request_failed_HTTP_429$"):
            provider.propose({"event": "app_open"}, [{"goal_id": "G01"}], ["T1"], Budget(config))
        self.assertEqual(client.post.call_count, 1)
        self.assertTrue(
            client.post.call_args.kwargs["json"]["response_format"]["json_schema"]["strict"]
        )


class ProviderBoundaryRegressionTests(unittest.TestCase):
    def test_request_schema_enumerates_only_supplied_identifiers(self):
        from src.provider import proposal_schema

        schema = proposal_schema([{"goal_id": "G01"}], ["T1", "T2", "T1"])
        self.assertEqual(schema["properties"]["goal_id"]["enum"], ["G01", None])
        self.assertEqual(schema["properties"]["title_ids"]["items"]["enum"], ["T1", "T2"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(proposal_schema([], [])["properties"]["title_ids"]["maxItems"], 0)

    def test_rate_limited_provider_is_retried_after_cooldown(self):
        from src.provider import ModelRouter

        config = {
            "provider": "models",
            "max_decision_seconds": 60,
            "max_model_calls": 8,
            "max_tool_calls": 20,
            "providers": [
                {"provider": "groq", "model": "first"},
                {"provider": "gemini", "model": "second"},
            ],
        }
        first = Mock()
        valid = {"goal_id": "G01", "title_ids": ["T1"]}
        first.propose.side_effect = [DependencyFailure("HTTP_429", retry_after_seconds=5), valid]
        router = ModelRouter(config)
        with patch("src.provider.create_live_provider", return_value=first), patch(
            "src.provider.time.sleep"
        ) as sleep:
            result = router.propose({}, [{"goal_id": "G01"}], ["T1"], Budget(config))
        self.assertEqual(result["provider_used"], "groq")
        self.assertEqual(first.propose.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_retry_after_is_sanitized(self):
        from src.provider import retry_after_seconds

        self.assertEqual(retry_after_seconds({"retry-after": "12.5"}), 12.5)
        for value in ["bad", "-1", "nan", "inf", None]:
            self.assertIsNone(retry_after_seconds({"retry-after": value}))


class RetryTests(unittest.TestCase):
    def setUp(self):
        from src.provider import ModelRouter

        self.config = {
            "max_decision_seconds": 60,
            "max_model_calls": 8,
            "max_tool_calls": 20,
            "retry_max_attempts": 5,
            "providers": [{"provider": "groq", "model": "test"}],
        }
        self.router = ModelRouter(self.config)
        self.model = Mock()
        self.good = {"goal_id": "G01", "title_ids": ["T1"]}
        self.candidates = [{"goal_id": "G01"}]

    def run_model(self, budget=None):
        return self.router.propose({}, self.candidates, ["T1"], budget or Budget(self.config))

    def test_exponential_backoff_and_fifth_attempt_success(self):
        self.model.propose.side_effect = [DependencyFailure("HTTP_503")] * 4 + [self.good]
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ) as sleep, patch("src.provider.random.uniform", return_value=0):
            result = self.run_model()
        self.assertEqual(result["goal_id"], "G01")
        self.assertEqual(self.model.propose.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4, 8])

    def test_bad_response_sent_back_to_same_model_for_correction(self):
        bad = {"goal_id": "G01", "title_ids": ["invented"]}
        self.model.propose.side_effect = [bad, self.good]
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ):
            result = self.run_model()
        correction = self.model.propose.call_args_list[1].kwargs["correction"]
        self.assertEqual(correction["validation_error"], "model_titles_outside_candidates")
        self.assertIn("invented", correction["previous_response"])
        self.assertTrue(result["provider_attempts"][0]["correction_requested"])

    def test_five_bad_responses_stop_without_publishing(self):
        from src.provider import ModelsUnavailable

        self.model.propose.return_value = {"goal_id": "invented", "title_ids": []}
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ):
            with self.assertRaises(ModelsUnavailable) as error:
                self.run_model()
        self.assertEqual(self.model.propose.call_count, 5)
        self.assertEqual(len(error.exception.attempts), 5)

    def test_long_retry_after_does_not_block_or_retry_early(self):
        from src.provider import ModelsUnavailable

        self.model.propose.side_effect = DependencyFailure("HTTP_429", retry_after_seconds=3600)
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ) as sleep:
            with self.assertRaises(ModelsUnavailable):
                self.run_model()
        sleep.assert_not_called()
        self.assertEqual(self.model.propose.call_count, 1)

    def test_credentials_are_not_retried(self):
        from src.provider import ModelsUnavailable

        self.model.propose.side_effect = DependencyFailure("HTTP_401")
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ) as sleep:
            with self.assertRaises(ModelsUnavailable):
                self.run_model()
        sleep.assert_not_called()
        self.assertEqual(self.model.propose.call_count, 1)

    def test_deadline_prevents_backoff(self):
        from src.provider import ModelsUnavailable

        budget = Budget(self.config)
        self.model.propose.side_effect = DependencyFailure("HTTP_503")
        with patch.object(budget, "elapsed", return_value=59.5), patch(
            "src.provider.create_live_provider", return_value=self.model
        ), patch("src.provider.time.sleep") as sleep:
            with self.assertRaises(ModelsUnavailable):
                self.run_model(budget)
        sleep.assert_not_called()

    def test_shared_call_budget_caps_retries(self):
        from src.provider import ModelRouter, ModelsUnavailable

        self.config["providers"].append({"provider": "gemini", "model": "backup"})
        router = ModelRouter(self.config)
        budget = Budget(self.config)

        def fail(*args, **kwargs):
            budget.model_calls += 1
            raise DependencyFailure("HTTP_503")

        self.model.propose.side_effect = fail
        with patch("src.provider.create_live_provider", return_value=self.model), patch(
            "src.provider.time.sleep"
        ):
            with self.assertRaises(ModelsUnavailable):
                router.propose({}, self.candidates, ["T1"], budget)
        self.assertEqual(budget.model_calls, 8)
        self.assertEqual(self.model.propose.call_count, 8)

    def test_inconsistent_or_duplicated_ids_are_rejected(self):
        from src.provider import InvalidModelResponse, validate_model_proposal

        for proposal in [
            {"goal_id": None, "title_ids": ["T1"]},
            {"goal_id": "G01", "title_ids": ["T1", "T1"]},
        ]:
            with self.assertRaises(InvalidModelResponse):
                validate_model_proposal(proposal, self.candidates, ["T1"])


class CorrectionPayloadTests(unittest.TestCase):
    def test_gemini_receives_rejected_output_and_error(self):
        import json

        client = Mock()
        client.interactions.create.return_value = SimpleNamespace(
            status="completed",
            output_text='{"goal_id":"G01","title_ids":["T1"],"reason":"supplied ID"}',
        )
        config = {"max_decision_seconds": 60, "max_model_calls": 8, "max_tool_calls": 20}
        provider = GeminiProvider(config, client=client)
        correction = {
            "previous_response": "bad output",
            "validation_error": "malformed_model_output",
        }
        provider.propose(
            {"event": "app_open"},
            [{"goal_id": "G01"}],
            ["T1"],
            Budget(config),
            correction=correction,
        )
        payload = json.loads(client.interactions.create.call_args.kwargs["input"])
        self.assertEqual(payload["correction"], correction)

    def test_groq_receives_rejected_output_and_error(self):
        import json
        import os

        from src.groq import GroqProvider

        client = Mock()
        client.post.return_value.status_code = 200
        client.post.return_value.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"goal_id":"G01","title_ids":["T1"],"reason":"supplied ID"}'
                    }
                }
            ]
        }
        config = {"max_decision_seconds": 60, "max_model_calls": 8, "max_tool_calls": 20}
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}):
            provider = GroqProvider(config, client=client)
        correction = {
            "previous_response": "bad output",
            "validation_error": "malformed_model_output",
        }
        provider.propose(
            {"event": "app_open"},
            [{"goal_id": "G01"}],
            ["T1"],
            Budget(config),
            correction=correction,
        )
        payload = json.loads(client.post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(payload["correction"], correction)
