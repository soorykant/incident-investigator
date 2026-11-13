"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def no_real_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the paid Anthropic API by accident.

    The default backend is `anthropic_api`, so a test that runs the default config with
    a real key in the shell would make live, billed calls. Tests that need a key set a
    fake one (`sk-ant-test`) and inject a fake SDK client."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
