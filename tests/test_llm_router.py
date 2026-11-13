"""Per-role backend routing from the models config (ticket 12, Q20)."""

from pathlib import Path

import pytest

from incident_investigator.llm.anthropic_api import AnthropicAPI
from incident_investigator.llm.base import LLMError, LLMResponse, LLMUnavailableError, Usage
from incident_investigator.llm.mock import MockLLM
from incident_investigator.llm.openai_compat import OpenAICompat
from incident_investigator.llm.router import RoleRouter, make_client
from incident_investigator.models_config import ModelsConfig, ModelsConfigError, load_models

ROLES = ["planner", "reasoning", "critic", "baseline", "judge", "observability", "dependency", "reporting"]


class Fake:
    def __init__(self, name: str, *, unavailable: bool = False) -> None:
        self.name = name
        self.unavailable = unavailable
        self.preflights = 0
        self.calls: list[str] = []

    def preflight(self) -> None:
        self.preflights += 1
        if self.unavailable:
            raise LLMUnavailableError(f"{self.name} is down")

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        self.calls.append(model)
        return LLMResponse(text=self.name, usage=Usage(tokens_in=1))


def _config(tmp_path: Path, **overrides: str) -> ModelsConfig:
    lines = [f"{role} = {overrides.get(role, '{ backend = \"anthropic_api\", model = \"opus\" }')}" for role in ROLES]
    path = tmp_path / "models.toml"
    path.write_text("[roles]\n" + "\n".join(lines) + "\n")
    return load_models(path)


def _mixed(tmp_path: Path) -> ModelsConfig:
    return _config(
        tmp_path,
        observability='{ backend = "openai_compat", model = "qwen" }',
        judge='{ backend = "anthropic_api", model = "opus-judge" }',
    )


def _fakes() -> dict[str, Fake]:
    return {name: Fake(name) for name in ("anthropic_api", "openai_compat", "mock")}


@pytest.mark.parametrize(
    ("backend", "cls"),
    [("anthropic_api", AnthropicAPI), ("openai_compat", OpenAICompat), ("mock", MockLLM)],
)
def test_the_factory_builds_each_known_backend(backend: str, cls: type):
    assert isinstance(make_client(backend, load_models()), cls)


def test_the_factory_passes_the_configured_base_url_to_openai_compat(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    path = tmp_path / "models.toml"
    path.write_text(
        "[roles]\n" + "\n".join(f'{r} = "m"' for r in ROLES) + '\n[backends.openai_compat]\nbase_url = "http://box:1/v1"\n'
    )

    client = make_client("openai_compat", load_models(path))

    assert isinstance(client, OpenAICompat)
    assert client.base_url == "http://box:1/v1"


def test_the_factory_rejects_an_unknown_backend_clearly():
    with pytest.raises(ModelsConfigError, match="unknown backend 'gpt-cloud'.*anthropic_api"):
        make_client("gpt-cloud", load_models())


def test_client_for_returns_the_backend_each_role_names(tmp_path: Path):
    fakes = _fakes()
    router = RoleRouter(_mixed(tmp_path), clients=fakes)

    assert router.client_for("observability") is fakes["openai_compat"]
    assert router.client_for("judge") is fakes["anthropic_api"]
    assert router.client_for("planner") is fakes["anthropic_api"]


def test_complete_dispatches_by_the_model_the_role_was_given(tmp_path: Path):
    fakes = _fakes()
    router = RoleRouter(_mixed(tmp_path), clients=fakes)

    assert router.complete(system="s", prompt="p", model="qwen").text == "openai_compat"
    assert router.complete(system="s", prompt="p", model="opus-judge").text == "anthropic_api"
    assert router.complete(system="s", prompt="p", model="opus").text == "anthropic_api"
    assert fakes["openai_compat"].calls == ["qwen"]


def test_complete_with_a_model_no_role_uses_is_an_llm_error(tmp_path: Path):
    router = RoleRouter(_mixed(tmp_path), clients=_fakes())

    with pytest.raises(LLMError, match="'gpt-9'.*not assigned"):
        router.complete(system="s", prompt="p", model="gpt-9")


def test_one_model_on_two_backends_is_rejected_at_startup_when_routing_by_model(tmp_path: Path):
    models = _config(tmp_path, judge='{ backend = "openai_compat", model = "opus" }')

    with pytest.raises(ModelsConfigError, match="'opus'.*anthropic_api.*openai_compat|'opus'.*openai_compat.*anthropic_api"):
        RoleRouter(models, clients=_fakes())

    # A caller that routes by role (client_for) can still use such a config.
    router = RoleRouter(models, clients=_fakes(), route_by_model=False)
    assert router.client_for("judge") is not router.client_for("critic")


def test_preflight_checks_each_backend_in_use_once(tmp_path: Path):
    fakes = _fakes()

    RoleRouter(_mixed(tmp_path), clients=fakes).preflight()

    assert (fakes["anthropic_api"].preflights, fakes["openai_compat"].preflights) == (1, 1)
    assert fakes["mock"].preflights == 0, "a backend no role uses is not checked"


def test_preflight_fails_fast_when_any_backend_in_use_is_unavailable(tmp_path: Path):
    fakes = _fakes()
    fakes["openai_compat"] = Fake("openai_compat", unavailable=True)

    with pytest.raises(LLMUnavailableError, match="openai_compat is down"):
        RoleRouter(_mixed(tmp_path), clients=fakes).preflight()


def test_by_default_the_router_builds_real_clients_once_per_backend(tmp_path: Path):
    router = RoleRouter(_mixed(tmp_path))

    assert isinstance(router.client_for("observability"), OpenAICompat)
    assert isinstance(router.client_for("judge"), AnthropicAPI)
    assert router.client_for("planner") is router.client_for("judge"), "one client per backend"


def test_the_default_config_routes_every_role_to_the_anthropic_api():
    router = RoleRouter(load_models())

    assert {type(router.client_for(role)) for role in ROLES} == {AnthropicAPI}


def test_the_subscription_cli_backend_is_gone():
    with pytest.raises(ModelsConfigError, match="unknown backend 'claude_cli'"):
        make_client("claude_cli", load_models())
