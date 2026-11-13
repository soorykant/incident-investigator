"""Per-agent model choice comes from the models config (Q8, Q20)."""

from pathlib import Path

import pytest

from incident_investigator.models_config import ModelsConfigError, load_models

OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"


@pytest.mark.parametrize(
    ("role", "model"),
    [
        ("planner", OPUS),
        ("reasoning", OPUS),
        ("critic", OPUS),
        ("baseline", OPUS),
        ("judge", OPUS),
        ("observability", HAIKU),
        ("dependency", HAIKU),
        ("reporting", HAIKU),
    ],
)
def test_default_config_assigns_the_decided_model_to_each_role(role: str, model: str):
    assert load_models().model_for(role) == model


def test_a_custom_config_file_overrides_the_defaults(tmp_path: Path):
    path = tmp_path / "models.toml"
    path.write_text(
        "[roles]\n"
        'planner = "a"\nreasoning = "a"\ncritic = "a"\nbaseline = "a"\njudge = "a"\n'
        'observability = "local-small"\ndependency = "b"\nreporting = "b"\n'
    )

    assert load_models(path).model_for("observability") == "local-small"


def test_a_config_missing_a_role_is_rejected(tmp_path: Path):
    path = tmp_path / "models.toml"
    path.write_text('[roles]\nplanner = "a"\n')

    with pytest.raises(ModelsConfigError, match="reasoning"):
        load_models(path)


def test_asking_for_an_unknown_role_is_an_error():
    with pytest.raises(KeyError):
        load_models().model_for("orchestrator")


# --- Per-role backend choice (ticket 12, Q15, Q20) ---

ROLES = ["planner", "reasoning", "critic", "baseline", "judge", "observability", "dependency", "reporting"]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(body)
    return path


def _roles(**overrides: str) -> str:
    default = '"m"'
    lines = [f"{role} = {overrides.get(role, default)}" for role in ROLES]
    return "[roles]\n" + "\n".join(lines) + "\n"


@pytest.mark.parametrize("role", ROLES)
def test_default_config_uses_the_anthropic_api_for_every_role(role: str):
    assert load_models().backend_for(role) == "anthropic_api"


def test_a_plain_model_string_means_the_anthropic_api_backend(tmp_path: Path):
    models = load_models(_write(tmp_path, _roles()))

    assert models.backend_for("planner") == "anthropic_api"
    assert models.model_for("planner") == "m"


def test_a_role_can_name_its_backend_and_model(tmp_path: Path):
    path = _write(
        tmp_path,
        _roles(
            observability='{ backend = "openai_compat", model = "qwen2.5:7b" }',
            judge='{ backend = "mock", model = "claude-opus-5-5" }',
        ),
    )

    models = load_models(path)

    assert (models.backend_for("observability"), models.model_for("observability")) == (
        "openai_compat",
        "qwen2.5:7b",
    )
    assert models.backend_for("judge") == "mock"
    assert models.backend_for("planner") == "anthropic_api"


def test_an_unknown_backend_fails_at_load_with_a_clear_message(tmp_path: Path):
    path = _write(tmp_path, _roles(critic='{ backend = "gpt-cloud", model = "x" }'))

    with pytest.raises(ModelsConfigError) as info:
        load_models(path)

    message = str(info.value)
    assert "unknown backend 'gpt-cloud'" in message
    assert "critic" in message
    assert "anthropic_api" in message and "openai_compat" in message and "mock" in message
    assert "claude_cli" not in message, "the subscription CLI backend was removed"


def test_the_removed_claude_cli_backend_is_an_unknown_backend(tmp_path: Path):
    with pytest.raises(ModelsConfigError, match="unknown backend 'claude_cli'"):
        load_models(_write(tmp_path, _roles(planner='{ backend = "claude_cli", model = "m" }')))


def test_the_openai_compat_base_url_can_be_set_in_the_config(tmp_path: Path):
    path = _write(tmp_path, _roles() + '\n[backends.openai_compat]\nbase_url = "http://gpu-box:8000/v1"\n')

    assert load_models(path).backends.openai_compat.base_url == "http://gpu-box:8000/v1"


def test_the_openai_compat_base_url_is_unset_by_default():
    assert load_models().backends.openai_compat.base_url is None
