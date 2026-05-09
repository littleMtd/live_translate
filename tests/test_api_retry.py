import sys
import unittest
from unittest.mock import MagicMock, patch
from utils.api_retry import classify_error, _RETRY_DELAYS

# Stub heavy packages so translator can be imported
for _mod in ("anthropic", "google", "google.genai"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


class TestClassifyError(unittest.TestCase):

    def _exc(self, name: str, msg: str = "") -> Exception:
        cls = type(name, (Exception,), {})
        return cls(msg)

    # --- rate_limit ---
    def test_rate_limit_by_class_name(self):
        self.assertEqual(classify_error(self._exc("RateLimitError")), "rate_limit")

    def test_rate_limit_resource_exhausted(self):
        self.assertEqual(classify_error(self._exc("ResourceExhausted")), "rate_limit")

    def test_rate_limit_by_429_message(self):
        self.assertEqual(classify_error(Exception("status 429 too many requests")), "rate_limit")

    def test_rate_limit_by_quota_message(self):
        self.assertEqual(classify_error(Exception("quota exceeded for today")), "rate_limit")

    # --- network ---
    def test_network_by_class_name(self):
        self.assertEqual(classify_error(self._exc("APIConnectionError")), "network")

    def test_network_timeout_class(self):
        self.assertEqual(classify_error(self._exc("TimeoutError")), "network")

    def test_network_by_503_message(self):
        self.assertEqual(classify_error(Exception("503 service unavailable")), "network")

    def test_network_by_timeout_message(self):
        self.assertEqual(classify_error(Exception("request timeout after 30s")), "network")

    # --- auth ---
    def test_auth_by_class_name(self):
        self.assertEqual(classify_error(self._exc("AuthenticationError")), "auth")

    def test_auth_by_401_message(self):
        self.assertEqual(classify_error(Exception("401 unauthorized")), "auth")

    def test_auth_by_api_key_message(self):
        self.assertEqual(classify_error(Exception("invalid api key provided")), "auth")

    # --- other ---
    def test_other_plain_exception(self):
        self.assertEqual(classify_error(Exception("something unexpected")), "other")

    def test_other_value_error(self):
        self.assertEqual(classify_error(ValueError("bad value")), "other")


class TestTranslatorRetry(unittest.TestCase):

    def _make_claude(self, side_effect):
        from modules.translator import ClaudeEngine
        e = ClaudeEngine.__new__(ClaudeEngine)
        e._client = MagicMock()
        e._client.messages.create.side_effect = side_effect
        return e

    def _make_gemini(self, side_effect):
        from modules.translator import GeminiEngine
        e = GeminiEngine.__new__(GeminiEngine)
        e._client = MagicMock()
        e._client.models.generate_content.side_effect = side_effect
        return e

    def test_no_retry_on_plain_exception(self):
        e = self._make_claude(Exception("generic error"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        mock_sleep.assert_not_called()

    def test_no_retry_on_auth_error(self):
        AuthErr = type("AuthenticationError", (Exception,), {})
        e = self._make_claude(AuthErr("invalid key"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        mock_sleep.assert_not_called()

    def test_rate_limit_returns_none_immediately_no_sleep(self):
        RateErr = type("RateLimitError", (Exception,), {})
        e = self._make_claude(RateErr("429"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        mock_sleep.assert_not_called()
        self.assertEqual(e._client.messages.create.call_count, 1)

    def test_network_error_returns_none_immediately(self):
        NetErr = type("APIConnectionError", (Exception,), {})
        e = self._make_claude(NetErr("connection reset"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        self.assertEqual(e._client.messages.create.call_count, 1)
        mock_sleep.assert_not_called()

    def test_gemini_network_error_returns_none_immediately(self):
        NetErr = type("APIConnectionError", (Exception,), {})
        e = self._make_gemini(NetErr("conn reset"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        self.assertEqual(e._client.models.generate_content.call_count, 1)
        mock_sleep.assert_not_called()

    def test_gemini_no_retry_on_auth_error(self):
        AuthErr = type("AuthenticationError", (Exception,), {})
        e = self._make_gemini(AuthErr("bad key"))
        with patch("time.sleep") as mock_sleep:
            result = e.translate("안녕", "system", False)
        self.assertIsNone(result)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
