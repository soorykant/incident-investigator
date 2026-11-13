"""Build LLM clients by backend name, and route each role to the backend the models config names (Q20).

`RoleRouter` is itself an `LLMClient`. Today the graph passes one client to every
agent and names the role only through `model=models.model_for(role)`, so
`complete()` dispatches by that model string. A caller that knows the role can use
`client_for(role)` instead, which also allows one model on two backends.
"""

from __future__ import annotations

from collections.abc import Mapping

from incident_investigator.llm.anthropic_api import AnthropicAPI
from incident_investigator.llm.base import LLMClient, LLMError, LLMResponse
from incident_investigator.llm.mock import MockLLM
from incident_investigator.llm.openai_compat import OpenAICompat
from incident_investigator.models_config import BACKENDS, ModelsConfig, ModelsConfigError


def make_client(backend: str, models: ModelsConfig) -> LLMClient:
    """One client for `backend`. Raises ModelsConfigError for an unknown name."""
    if backend == "anthropic_api":
        return AnthropicAPI()
    if backend == "openai_compat":
        return OpenAICompat(base_url=models.backends.openai_compat.base_url)
    if backend == "mock":
        return MockLLM()
    raise ModelsConfigError(f"unknown backend {backend!r}; choose one of: {', '.join(BACKENDS)}")


class RoleRouter:
    def __init__(
        self,
        models: ModelsConfig,
        *,
        clients: Mapping[str, LLMClient] | None = None,
        route_by_model: bool = True,
    ) -> None:
        """`clients` maps backend name to client (tests inject fakes); missing ones are built.

        With `route_by_model`, a model string assigned to two backends is a startup
        error, because `complete(model=...)` could not tell them apart."""
        self.models = models
        self._clients: dict[str, LLMClient] = {}
        self._role_backend = {role: models.backend_for(role) for role in models.role_names()}
        for backend in dict.fromkeys(self._role_backend.values()):
            given = (clients or {}).get(backend)
            self._clients[backend] = given if given is not None else make_client(backend, models)

        self._model_backend: dict[str, str] = {}
        if route_by_model:
            for role, backend in self._role_backend.items():
                model = models.model_for(role)
                other = self._model_backend.setdefault(model, backend)
                if other != backend:
                    raise ModelsConfigError(
                        f"model {model!r} is assigned to two backends ({other}, {backend}); "
                        "the graph routes LLM calls by model name, so give each backend distinct model names"
                    )

    def client_for(self, role: str) -> LLMClient:
        if role not in self._role_backend:
            raise KeyError(f"unknown role {role!r}")
        return self._clients[self._role_backend[role]]

    def preflight(self) -> None:
        for client in self._clients.values():
            client.preflight()

    def complete(self, *, system: str, prompt: str, model: str) -> LLMResponse:
        backend = self._model_backend.get(model)
        if backend is None:
            raise LLMError(f"model {model!r} is not assigned to any role in the models config")
        return self._clients[backend].complete(system=system, prompt=prompt, model=model)
