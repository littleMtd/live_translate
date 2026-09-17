import unittest
from utils.api_retry import classify_error, _RETRY_DELAYS


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



if __name__ == "__main__":
    unittest.main()
