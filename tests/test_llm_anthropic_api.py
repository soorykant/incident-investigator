"""The anthropic_api backend, with a fake Anthropic client injected at the SDK boundary (Q15)."""

import threading
import time
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from incident_investigator.llm.anthropic_api import AnthropicAPI
from incident_investigator.llm.base import (
    LLMAuthError,
    LLMError,
    LLMFatalError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _message(text: str = '{"ok": true}', **usage: int | None) -> SimpleNamespace:
    counts: dict[str, int | None] = {"input_tokens": 5, "output_tokens": 9, "cache_creation_input_tokens": 700, "cache_read_input_tokens": 300}
    counts.update(usage)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**counts),
        stop_reason="end_turn",
    )


class FakeMessages:
    def __init__(self, reply: Any = None, *, delay_s: float = 0.0) -> None:
        self.reply = reply if reply is not None else _message()
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay_s:
            time.sleep(self.delay_s)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _api(reply: Any = None, **kwargs: Any) -> tuple[AnthropicAPI, FakeMessages]:
    messages = FakeMessages(reply, **kwargs)
    return AnthropicAPI(client=SimpleNamespace(messages=messages)), messages


def _complete(api: AnthropicAPI):
    return api.complete(system="sys", prompt="the prompt", model="claude-haiku-5-5")


def test_a_valid_reply_becomes_an_llm_response_with_cache_writes_counted_as_input():
    api, _ = _api()

    response = _complete(api)

    assert response.text == '{"ok": true}'
    # tokens_in = input + cache writes; cache reads are reported apart (D5).
    assert (response.usage.tokens_in, response.usage.tokens_out, response.usage.cache_tokens) == (705, 9, 300)
    assert response.latency_s >= 0


def test_missing_cache_counts_are_read_as_zero():
    api, _ = _api(_message(cache_creation_input_tokens=None, cache_read_input_tokens=None))

    usage = _complete(api).usage

    assert (usage.tokens_in, usage.cache_tokens) == (5, 0)


def test_the_request_passes_the_system_prompt_as_system_and_the_prompt_as_one_user_turn():
    api, messages = _api()

    _complete(api)

    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "the prompt"}]
    assert call["max_tokens"] > 0
    assert call["timeout"] == 120


def test_text_blocks_are_joined_and_other_blocks_ignored():
    reply = _message()
    reply.content = [
        SimpleNamespace(type="thinking", thinking="hmm"),
        SimpleNamespace(type="text", text='{"a":'),
        SimpleNamespace(type="text", text=" 1}"),
    ]
    api, _ = _api(reply)

    assert _complete(api).text == '{"a": 1}'


def test_a_timeout_raises_llm_timeout_error():
    api, _ = _api(anthropic.APITimeoutError(request=REQUEST))

    with pytest.raises(LLMTimeoutError, match="timed out"):
        _complete(api)


@pytest.mark.parametrize(
    "error",
    [
        anthropic.InternalServerError(
            "overloaded", response=httpx2.Response(529, request=REQUEST), body={"type": "overloaded_error"}
        ),
        anthropic.APIConnectionError(request=REQUEST),
    ],
)
def test_an_api_error_raises_llm_error(error: Exception):
    api, _ = _api(error)

    with pytest.raises(LLMError) as info:
        _complete(api)

    # An overload or a dropped connection is an ordinary failed attempt the agent loop retries.
    assert not isinstance(info.value, (LLMTimeoutError, LLMRateLimitError))


def test_a_rate_limit_raises_llm_rate_limit_error_so_the_eval_stops_cleanly():
    error = anthropic.RateLimitError(
        "rate limited", response=httpx2.Response(429, request=REQUEST), body={"type": "rate_limit_error"}
    )
    api, _ = _api(error)

    with pytest.raises(LLMRateLimitError, match="rate limit"):
        _complete(api)


@pytest.mark.parametrize(
    "error",
    [
        anthropic.AuthenticationError(
            "Invalid bearer token",
            response=httpx2.Response(401, request=REQUEST),
            body={"type": "authentication_error"},
        ),
        anthropic.PermissionDeniedError(
            "forbidden", response=httpx2.Response(403, request=REQUEST), body={"type": "permission_error"}
        ),
    ],
    ids=["401", "403"],
)
def test_a_rejected_api_key_raises_llm_auth_error_which_stops_the_run(error: Exception):
    api, _ = _api(error)

    with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY") as info:
        _complete(api)

    assert isinstance(info.value, LLMFatalError), "it must stop the run, not count as a failed attempt"
    assert not isinstance(info.value, LLMRateLimitError), "waiting does not fix a bad key"


def test_a_rate_limit_is_also_fatal_for_the_run():
    assert issubclass(LLMRateLimitError, LLMFatalError)


def test_a_reply_without_text_raises_llm_error():
    reply = _message()
    reply.content = []
    api, _ = _api(reply)

    with pytest.raises(LLMError, match="no text"):
        _complete(api)


def test_preflight_fails_fast_when_the_api_key_is_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(LLMUnavailableError, match="ANTHROPIC_API_KEY") as info:
        AnthropicAPI().preflight()

    assert "claude_cli" not in str(info.value) and "subscription" not in str(info.value)


def test_preflight_passes_when_the_api_key_is_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    AnthropicAPI().preflight()


def test_the_sdk_client_is_built_from_the_api_key_with_no_hidden_retries(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    built: dict[str, Any] = {}

    def fake_anthropic(**kwargs: Any) -> SimpleNamespace:
        built.update(kwargs)
        return SimpleNamespace(messages=FakeMessages())

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)

    _complete(AnthropicAPI())

    assert built["api_key"] == "sk-ant-test"
    assert built["max_retries"] == 0, "the agent loop owns retries"


def test_at_most_three_calls_are_in_flight():
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    class Counting(FakeMessages):
        def create(self, **kwargs: Any) -> Any:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return _message()

    api = AnthropicAPI(client=SimpleNamespace(messages=Counting()))
    threads = [threading.Thread(target=_complete, args=(api,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 3
