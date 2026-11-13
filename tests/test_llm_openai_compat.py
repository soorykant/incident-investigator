"""The openai_compat backend (Ollama, LM Studio, vLLM), with HTTP mocked by httpx.MockTransport (Q20)."""

import json
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from incident_investigator.llm.base import LLMError, LLMTimeoutError, LLMUnavailableError
from incident_investigator.llm.openai_compat import DEFAULT_BASE_URL, OpenAICompat

CHAT_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "qwen2.5:7b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"ok": true}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129},
}


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _client(respond: Callable[[httpx.Request], httpx.Response], **kwargs) -> tuple[OpenAICompat, Recorder]:
    recorder = Recorder(respond)
    return OpenAICompat(transport=httpx.MockTransport(recorder), **kwargs), recorder


def _complete(client: OpenAICompat):
    return client.complete(system="sys", prompt="the prompt", model="qwen2.5:7b")


def test_a_valid_reply_becomes_an_llm_response_with_usage():
    client, _ = _client(lambda r: httpx.Response(200, json=CHAT_REPLY))

    response = _complete(client)

    assert response.text == '{"ok": true}'
    assert (response.usage.tokens_in, response.usage.tokens_out, response.usage.cache_tokens) == (120, 9, 0)
    assert response.latency_s >= 0


def test_cached_prompt_tokens_are_reported_apart_from_new_input():
    reply = {**CHAT_REPLY, "usage": {**CHAT_REPLY["usage"], "prompt_tokens_details": {"cached_tokens": 100}}}
    client, _ = _client(lambda r: httpx.Response(200, json=reply))

    usage = _complete(client).usage

    assert (usage.tokens_in, usage.cache_tokens) == (20, 100)


def test_the_request_is_a_chat_completion_with_the_system_prompt_first(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    client, recorder = _client(lambda r: httpx.Response(200, json=CHAT_REPLY))

    _complete(client)

    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{DEFAULT_BASE_URL}/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the prompt"},
    ]


def test_the_default_base_url_is_local_ollama():
    assert DEFAULT_BASE_URL == "http://localhost:11434/v1"


def test_the_base_url_comes_from_the_environment_before_the_config(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://env-host:1234/v1/")
    client, recorder = _client(lambda r: httpx.Response(200, json=CHAT_REPLY), base_url="http://config:8000/v1")

    _complete(client)

    assert str(recorder.requests[0].url) == "http://env-host:1234/v1/chat/completions"


def test_the_config_base_url_is_used_when_the_environment_has_none(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    client, recorder = _client(lambda r: httpx.Response(200, json=CHAT_REPLY), base_url="http://config:8000/v1")

    _complete(client)

    assert str(recorder.requests[0].url) == "http://config:8000/v1/chat/completions"


def test_an_api_key_from_the_environment_is_sent_as_a_bearer_token(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-local")
    client, recorder = _client(lambda r: httpx.Response(200, json=CHAT_REPLY))

    _complete(client)

    assert recorder.requests[0].headers["authorization"] == "Bearer sk-local"


def test_the_call_timeout_is_120_seconds():
    assert OpenAICompat().timeout_s == 120


def test_a_timeout_raises_llm_timeout_error():
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client, _ = _client(hang)

    with pytest.raises(LLMTimeoutError, match="timed out"):
        _complete(client)


def test_an_http_error_status_raises_llm_error_with_the_body():
    client, _ = _client(lambda r: httpx.Response(404, json={"error": "model 'qwen2.5:7b' not found"}))

    with pytest.raises(LLMError, match="404.*not found"):
        _complete(client)


def test_a_dropped_connection_mid_call_raises_llm_error():
    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, _ = _client(drop)

    with pytest.raises(LLMError, match="connection refused"):
        _complete(client)


@pytest.mark.parametrize("payload", [{"choices": []}, {"unexpected": True}, ["not", "an", "object"]])
def test_a_malformed_reply_raises_llm_error(payload: object):
    client, _ = _client(lambda r: httpx.Response(200, json=payload))

    with pytest.raises(LLMError):
        _complete(client)


def test_a_non_json_reply_raises_llm_error():
    client, _ = _client(lambda r: httpx.Response(200, text="<html>proxy error</html>"))

    with pytest.raises(LLMError):
        _complete(client)


def test_preflight_passes_when_the_endpoint_lists_models(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    client, recorder = _client(lambda r: httpx.Response(200, json={"object": "list", "data": []}))

    client.preflight()

    assert recorder.requests[0].method == "GET"
    assert str(recorder.requests[0].url) == f"{DEFAULT_BASE_URL}/models"


def test_preflight_fails_fast_when_the_endpoint_is_unreachable(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, _ = _client(refuse)

    with pytest.raises(LLMUnavailableError) as info:
        client.preflight()

    message = str(info.value)
    assert DEFAULT_BASE_URL in message
    assert "OPENAI_COMPAT_BASE_URL" in message


def test_preflight_fails_when_the_endpoint_is_not_an_openai_compatible_api():
    client, _ = _client(lambda r: httpx.Response(404, text="not found"))

    with pytest.raises(LLMUnavailableError, match="404"):
        client.preflight()


def test_at_most_three_calls_are_in_flight():
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return httpx.Response(200, json=CHAT_REPLY)

    client, _ = _client(slow)
    threads = [threading.Thread(target=_complete, args=(client,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert peak == 3
