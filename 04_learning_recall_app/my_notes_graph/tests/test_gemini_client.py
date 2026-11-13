"""No-network retry, root configuration, exact-token guard, and fallback tests."""
from __future__ import annotations
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from src.gemini_client import GeminiClient, GeminiError, _EmptyResponse


class GeminiClientTests(unittest.TestCase):
    """Do not spend user API quota while verifying all failure modes."""

    def client(self, **kwargs):
        return GeminiClient(api_key="test-not-a-real-key", primary_model="primary", fallback_model="fallback",
                            max_retries=1, initial_backoff_sec=0, **kwargs)

    def test_primary_unavailable_uses_fallback(self):
        unavailable = urllib.error.HTTPError("redacted", 404, "missing", {}, None)
        with patch.object(GeminiClient, "_generate_once", side_effect=[unavailable, "answer"]) as call:
            result = self.client().generate("q")
        self.assertEqual(result, "answer")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["primary", "fallback"])

    def test_empty_primary_answer_uses_fallback_immediately(self):
        empty = _EmptyResponse("Provider returned no usable answer")
        with patch.object(GeminiClient, "_generate_once", side_effect=[empty, "answer"]) as call:
            result = self.client().generate("q")
        self.assertEqual(result, "answer")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["primary", "fallback"])

    def test_dead_primary_does_not_exhaust_all_retries(self):
        unavailable = urllib.error.HTTPError("redacted", 503, "busy", {}, None)
        client = GeminiClient(api_key="test-not-a-real-key", primary_model="primary",
                              fallback_model="fallback", max_retries=5,
                              initial_backoff_sec=0)
        with patch.object(GeminiClient, "_generate_once",
                          side_effect=[unavailable, unavailable, "answer"]) as call, \
             patch("src.gemini_client.time.sleep"):
            result = client.generate("q")
        self.assertEqual(result, "answer")
        self.assertEqual([c.args[0] for c in call.call_args_list], ["primary", "primary", "fallback"])

    def test_transient_retries_then_succeeds(self):
        error = urllib.error.HTTPError("redacted", 429, "limited", {}, None)
        with patch.object(GeminiClient, "_generate_once", side_effect=[error, "ok"]) as call, \
             patch("src.gemini_client.time.sleep") as sleep:
            self.assertEqual(self.client().generate("q", timeout=7), "ok")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(call.call_args.args[-1], 7)

    def test_auth_failure_never_retries_or_falls_back(self):
        error = urllib.error.HTTPError("redacted", 403, "forbidden", {}, None)
        with patch.object(GeminiClient, "_generate_once", side_effect=error) as call:
            with self.assertRaisesRegex(GeminiError, "retries stopped"):
                self.client().generate("q")
        self.assertEqual(call.call_count, 1)

    def test_exact_prompt_count_enforced(self):
        with patch.object(GeminiClient, "_post", return_value={"totalTokens": 2000}) as call:
            with self.assertRaisesRegex(GeminiError, "strict token budget"):
                self.client().generate("q")
        self.assertEqual(call.call_args.args[1], "countTokens")

    def test_root_env_path_and_key_are_loaded_without_echoing(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            root = Path(temporary)
            (root / ".env").write_text("GEMINI_API_KEY=fake-root-key\nGEMINI_PRIMARY_MODEL=root-primary\n")
            client = GeminiClient(root=root)
            self.assertEqual(client.primary_model, "root-primary")
            self.assertEqual(client.api_key, "fake-root-key")


if __name__ == "__main__":
    unittest.main()
