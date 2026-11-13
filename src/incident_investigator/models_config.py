"""Load the per-role backend and model choice from models.toml (stdlib tomllib, no YAML dependency).

A role is either a plain model string, which means the `anthropic_api` backend:

    planner = "claude-opus-5-5"

or a table that names the backend too (Q20):

    observability = { backend = "openai_compat", model = "qwen2.5:7b" }
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, field_validator

DEFAULT_PATH = Path(__file__).with_name("models.toml")

DEFAULT_BACKEND = "anthropic_api"
# `mock` is the offline stand-in; it is allowed so a whole config can run offline.
BACKENDS = ("anthropic_api", "openai_compat", "mock")


class ModelsConfigError(ValueError):
    pass


class RoleChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: str = DEFAULT_BACKEND
    model: str

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value not in BACKENDS:
            raise ValueError(f"unknown backend {value!r}; choose one of: {', '.join(BACKENDS)}")
        return value


def _plain_string_means_default_backend(value: Any) -> Any:
    return {"model": value} if isinstance(value, str) else value


Role = Annotated[RoleChoice, BeforeValidator(_plain_string_means_default_backend)]


class _Roles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner: Role
    reasoning: Role
    critic: Role
    baseline: Role
    judge: Role
    observability: Role
    dependency: Role
    reporting: Role


class OpenAICompatSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None: use $OPENAI_COMPAT_BASE_URL, else http://localhost:11434/v1 (Ollama).
    base_url: str | None = None


class BackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai_compat: OpenAICompatSettings = OpenAICompatSettings()


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: _Roles
    backends: BackendSettings = BackendSettings()

    def choice_for(self, role: str) -> RoleChoice:
        if role not in _Roles.model_fields:
            raise KeyError(f"unknown role {role!r}")
        return getattr(self.roles, role)

    def model_for(self, role: str) -> str:
        return self.choice_for(role).model

    def backend_for(self, role: str) -> str:
        return self.choice_for(role).backend

    def role_names(self) -> list[str]:
        return list(_Roles.model_fields)


def _describe(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {message}")
    return "; ".join(lines)


def load_models(path: Path | None = None) -> ModelsConfig:
    source = path or DEFAULT_PATH
    try:
        return ModelsConfig.model_validate(tomllib.loads(source.read_text()))
    except ValidationError as exc:
        raise ModelsConfigError(f"invalid models config {source}: {_describe(exc)}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ModelsConfigError(f"invalid models config {source}: {exc}") from exc
