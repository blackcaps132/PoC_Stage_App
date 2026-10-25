"""Invalid budgets should fail before a replay or API request starts."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.__main__ import build_parser, read_config


class ConfigTests(unittest.TestCase):
    def test_invalid_numeric_budgets_are_rejected(self):
        valid = {
            "provider": "replay",
            "max_model_calls": 8,
            "max_tool_calls": 20,
            "max_decision_seconds": 60,
            "dependency_timeout_seconds": 0.05,
        }
        for key, value in [
            ("max_model_calls", True),
            ("max_tool_calls", -1),
            ("max_decision_seconds", float("nan")),
            ("max_decision_seconds", float("inf")),
            ("dependency_timeout_seconds", False),
        ]:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps({**valid, key: value}))
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    read_config(path, build_parser())
