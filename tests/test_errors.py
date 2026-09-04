"""The taxonomy is what stops a client's 400 from being amplified across the pool."""

from __future__ import annotations

from tokenbiryani.proxy.errors import (
    AccountAction,
    RequestAction,
    classify,
    classify_exception,
    parse_retry_after,
)


def test_429_cools_the_account_and_retries_elsewhere():
    result = classify(429, {"retry-after": "27"})
    assert result.kind == "rate_limit"
    assert result.account_action is AccountAction.COOLDOWN
    assert result.request_action is RequestAction.RETRY_OTHER
    assert result.cooldown_seconds == 27.0


def test_400_is_never_retried():
    result = classify(400, {}, {"error": {"type": "invalid_request_error", "message": "bad"}})
    assert result.request_action is RequestAction.RETURN
    assert result.account_action is AccountAction.NONE
    assert not result.retryable


def test_401_disables_the_account():
    result = classify(401, {}, {"error": {"type": "authentication_error", "message": "nope"}})
    assert result.account_action is AccountAction.DISABLE
    assert result.request_action is RequestAction.RETRY_OTHER


def test_403_naming_a_model_is_a_capability_gap_not_a_bad_key():
    result = classify(
        403, {}, {"error": {"type": "permission_error", "message": "no access to this model"}}
    )
    assert result.account_action is AccountAction.MARK_MODEL_UNSUPPORTED
    assert result.request_action is RequestAction.RETRY_OTHER


def test_529_is_transient():
    result = classify(529, {}, {"error": {"type": "overloaded_error", "message": "busy"}})
    assert result.account_action is AccountAction.SHORT_COOLDOWN
    assert result.request_action is RequestAction.RETRY_ANY


def test_5xx_ticks_the_breaker():
    for status in (500, 502, 503):
        result = classify(status)
        assert result.account_action is AccountAction.ERROR_TICK
        assert result.request_action is RequestAction.RETRY_ANY


def test_413_returns_to_client():
    assert classify(413).request_action is RequestAction.RETURN


def test_transport_failures_are_retryable():
    result = classify_exception(OSError("connection reset"))
    assert result.request_action is RequestAction.RETRY_ANY
    assert "OSError" in result.detail


def test_retry_after_accepts_http_dates():
    assert parse_retry_after("13") == 13.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None
