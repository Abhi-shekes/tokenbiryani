"""Output estimation: predict what a model returns, never more than it may return."""

from __future__ import annotations

import time

import pytest
from conftest import make_config

from tokenbiryani.core.account import AccountRuntime
from tokenbiryani.core.estimator import MODE_CEILING, OutputEstimator
from tokenbiryani.core.gateway import GatewayError
from tokenbiryani.core.limits import TokenEstimate, estimate_request


def body(max_tokens: int = 32000, model: str = "claude-test-1"):
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": "hi"}],
    }


def feed(estimator: OutputEstimator, model: str, value: int, times: int) -> None:
    for _ in range(times):
        estimator.observe(model, predicted=value * 10, actual=value)


# ---- the problem this exists to fix ----------------------------------------------


def admitted(output_limit: int, estimate: TokenEstimate) -> int:
    """How many concurrent requests an account will admit before it reads as full."""
    config = make_config(["a"])
    account = AccountRuntime(config=config.accounts[0])
    now = time.time()
    account.mirror.update_from_headers(
        {
            "anthropic-ratelimit-output-tokens-limit": str(output_limit),
            "anthropic-ratelimit-output-tokens-remaining": str(output_limit),
        },
        now,
    )
    count = 0
    while account.mirror.can_serve(estimate, now) and count < 500:
        account.mirror.reserve(estimate, "a")
        count += 1
    return count


def test_leasing_the_ceiling_starves_the_pool():
    """The baseline. An agent client sends 32k and gets a couple of slots."""
    assert admitted(64000, TokenEstimate(100, 32000)) == 2
    assert admitted(16000, TokenEstimate(100, 8192)) == 1


def test_predicting_the_real_size_gives_the_capacity_back():
    """Same account, same requests, leased at what the model actually returns."""
    predicted = TokenEstimate(100, 800, output_ceiling=32000)
    assert admitted(64000, predicted) == 80
    assert admitted(16000, predicted) == 20


# ---- the estimator ---------------------------------------------------------------


def test_the_ceiling_is_used_until_there_is_evidence():
    estimator = OutputEstimator(min_samples=20)
    assert estimator.predict("claude-test-1", 32000) == 32000
    feed(estimator, "claude-test-1", 500, times=19)
    assert estimator.predict("claude-test-1", 32000) == 32000, "19 samples is not 20"


def test_a_prediction_takes_over_once_it_has_samples():
    estimator = OutputEstimator(min_samples=20, floor=1)
    feed(estimator, "claude-test-1", 500, times=40)
    assert estimator.predict("claude-test-1", 32000) == 500


def test_a_prediction_never_exceeds_the_caller_s_ceiling():
    """The estimator may only ever reserve less than the old behaviour."""
    estimator = OutputEstimator(min_samples=5, floor=1)
    feed(estimator, "claude-test-1", 9000, times=20)
    assert estimator.predict("claude-test-1", 1024) == 1024


def test_the_floor_protects_a_terse_model():
    estimator = OutputEstimator(min_samples=5, floor=256)
    feed(estimator, "claude-test-1", 3, times=20)
    assert estimator.predict("claude-test-1", 32000) == 256


def test_it_predicts_a_high_quantile_not_the_median():
    """A median lease would be beaten by half of all traffic. Reserve for the tail."""
    estimator = OutputEstimator(min_samples=5, floor=1, quantile=0.95)
    for value in [100] * 80 + [9000] * 20:
        estimator.observe("claude-test-1", predicted=32000, actual=value)
    assert estimator.snapshot()["models"]["claude-test-1"]["median"] == 100
    assert estimator.predict("claude-test-1", 32000) == 9000


def test_the_quantile_is_nearest_rank():
    """95 short answers in 100 put the p95 on the last short one, not the first long
    one. Stated because it is the boundary someone will otherwise call a bug."""
    estimator = OutputEstimator(min_samples=5, floor=1, quantile=0.95)
    for value in [100] * 95 + [9000] * 5:
        estimator.observe("claude-test-1", predicted=32000, actual=value)
    assert estimator.predict("claude-test-1", 32000) == 100


def test_models_are_predicted_separately():
    estimator = OutputEstimator(min_samples=5, floor=1)
    feed(estimator, "terse", 200, times=20)
    feed(estimator, "verbose", 4000, times=20)
    assert estimator.predict("terse", 32000) == 200
    assert estimator.predict("verbose", 32000) == 4000


def test_being_beaten_too_often_falls_back_to_the_ceiling():
    """The safety valve: a distribution that moved out from under the estimator."""
    estimator = OutputEstimator(min_samples=10, floor=1, max_undershoot=0.2)
    for _ in range(20):
        estimator.observe("claude-test-1", predicted=100, actual=5000)
    assert estimator.predict("claude-test-1", 32000) == 32000


def test_a_failed_request_teaches_it_nothing():
    """Zero output tokens is an error, not a short answer."""
    estimator = OutputEstimator(min_samples=5, floor=1)
    for _ in range(20):
        estimator.observe("claude-test-1", predicted=32000, actual=0)
    assert estimator.predict("claude-test-1", 32000) == 32000


def test_the_ceiling_mode_never_predicts():
    estimator = OutputEstimator(mode=MODE_CEILING, min_samples=5, floor=1)
    feed(estimator, "claude-test-1", 500, times=40)
    assert estimator.predict("claude-test-1", 32000) == 32000


def test_the_snapshot_reports_what_it_believes():
    estimator = OutputEstimator(min_samples=5, floor=1)
    feed(estimator, "claude-test-1", 500, times=20)
    snapshot = estimator.snapshot()
    assert snapshot["mode"] == "adaptive"
    assert snapshot["models"]["claude-test-1"]["samples"] == 20
    assert snapshot["models"]["claude-test-1"]["predicting"] is True


# ---- estimate_request ------------------------------------------------------------


def test_the_ceiling_is_kept_on_the_estimate():
    """`exceeds_capacity` asks what the caller may receive, not what we expect."""
    estimator = OutputEstimator(min_samples=5, floor=1)
    feed(estimator, "claude-test-1", 500, times=20)
    estimate = estimate_request(body(32000), 1.15, predictor=estimator.predict)
    assert estimate.output_tokens == 500
    assert estimate.output_ceiling == 32000


def test_a_request_bigger_than_the_window_is_still_unservable():
    """A prediction must not smuggle a request past the could-never-serve check."""
    estimator = OutputEstimator(min_samples=5, floor=1)
    feed(estimator, "claude-test-1", 500, times=20)
    estimate = estimate_request(body(32000), 1.15, predictor=estimator.predict)

    config = make_config(["a"])
    account = AccountRuntime(config=config.accounts[0])
    account.mirror.update_from_headers(
        {"anthropic-ratelimit-output-tokens-limit": "8000"}, time.time()
    )
    assert account.mirror.exceeds_capacity(estimate)


def test_no_predictor_behaves_exactly_as_before():
    estimate = estimate_request(body(4096), 1.15)
    assert estimate.output_tokens == 4096
    assert estimate.output_ceiling == 4096


def test_a_broken_predictor_falls_back_to_the_ceiling():
    def explode(model: str, ceiling: int) -> int:
        raise RuntimeError("no")

    estimate = estimate_request(body(4096), 1.15, predictor=explode)
    assert estimate.output_tokens == 4096


# ---- through the gateway ---------------------------------------------------------

#: Under the mock's 20,000-token output window, so it is servable — and still eight
#: times what the mock actually returns, which is the over-reservation being fixed.
AGENT_MAX_TOKENS = 8192


async def test_the_gateway_learns_from_what_it_serves(gateway_factory, key):
    """The loop closes: serve requests, and the lease stops being the ceiling."""
    gateway = gateway_factory(["acct-01"])

    for _ in range(30):
        await gateway.complete(body(AGENT_MAX_TOKENS), {}, key)

    model = gateway.estimator.snapshot()["models"]["claude-test-1"]
    assert model["samples"] == 30
    assert model["predicting"] is True
    assert gateway.estimator.predict("claude-test-1", AGENT_MAX_TOKENS) < AGENT_MAX_TOKENS


async def test_the_ceiling_mode_keeps_the_old_behaviour(gateway_factory, key):
    gateway = gateway_factory(
        ["acct-01"], overrides={"routing": {"output_estimate": "max_tokens"}}
    )
    for _ in range(30):
        await gateway.complete(body(AGENT_MAX_TOKENS), {}, key)
    assert (
        gateway.estimator.predict("claude-test-1", AGENT_MAX_TOKENS) == AGENT_MAX_TOKENS
    )


async def test_estimation_is_visible_to_an_operator(gateway_factory, key):
    gateway = gateway_factory(["acct-01"])
    await gateway.complete(body(4096), {}, key)
    snapshot = gateway.estimator.snapshot()
    assert snapshot["mode"] == "adaptive"
    assert "claude-test-1" in snapshot["models"]


async def test_a_ceiling_above_the_window_is_still_refused(gateway_factory, key):
    """Pinned deliberately. The estimator may know this request returns 20 tokens,
    but `max_tokens` above the account's whole output window is the caller asking
    for something the account could never be obliged to deliver, and admitting it on
    a prediction would be the estimator overruling a stated bound. Predicting sizes
    the *lease*; it does not relax admission."""
    gateway = gateway_factory(["acct-01"])
    for _ in range(30):
        await gateway.complete(body(4096), {}, key)

    with pytest.raises(GatewayError) as raised:
        await gateway.complete(body(32000), {}, key)
    assert raised.value.status == 503
