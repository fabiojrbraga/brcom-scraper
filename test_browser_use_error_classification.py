import unittest

from app.scraper.browser_use_agent import BrowserUseAgent


class _FakeHistory:
    def __init__(self, errors):
        self._errors = errors

    def errors(self):
        return self._errors


class BrowserUseErrorClassificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Avoid full initialization; helpers are pure methods.
        cls.agent = BrowserUseAgent.__new__(BrowserUseAgent)

    def test_rate_limit_detected_from_text(self):
        err = self.agent._classify_agent_failure_error(
            final_result="Error code: 429 - rate_limit_exceeded"
        )
        self.assertEqual(err, "rate_limit_exceeded")

    def test_rate_limit_detected_from_history(self):
        history = _FakeHistory(
            [
                None,
                "ModelRateLimitError: Rate limit reached on tokens per min (TPM)",
            ]
        )
        err = self.agent._classify_agent_failure_error(history=history)
        self.assertEqual(err, "rate_limit_exceeded")

    def test_protocol_error_classification(self):
        err = self.agent._classify_agent_failure_error(
            final_result="connection closed with protocol error"
        )
        self.assertEqual(err, "protocol_error")

    def test_cdp_timeout_classification_treated_as_protocol_error(self):
        err = self.agent._classify_agent_failure_error(
            exc=TimeoutError("CDP requests failed or timed out: snapshot, dom_tree, ax_tree")
        )
        self.assertEqual(err, "protocol_error")

    def test_retry_login_error_for_cdp_timeout(self):
        should_retry = self.agent._should_retry_login_error(
            TimeoutError("CDP requests failed or timed out: snapshot, dom_tree, ax_tree")
        )
        self.assertTrue(should_retry)

    def test_unknown_error_falls_back_to_parse_failed(self):
        err = self.agent._classify_agent_failure_error(final_result="unexpected format")
        self.assertEqual(err, "parse_failed")


if __name__ == "__main__":
    unittest.main()
