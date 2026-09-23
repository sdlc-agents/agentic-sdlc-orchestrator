from .base import GenerationRequest, Provider
from .mock_provider import BROWNFIELD, GREENFIELD, MockProvider

__all__ = [
    "BROWNFIELD",
    "GREENFIELD",
    "GenerationRequest",
    "MockProvider",
    "Provider",
    "build_provider",
]


def build_provider(
    mode: str, model: str | None = None, fail_once: set[str] | None = None
) -> Provider:
    """Resolve `--mode` to a provider.

    The OpenAI provider is imported lazily so the platform stays runnable with
    neither the SDK nor a key installed. `fail_once` makes the deterministic
    provider fail the named agents on their first attempt, so recovery can be
    watched in a real run rather than only asserted in tests.
    """
    if mode == "mock":
        return MockProvider(fail_once=fail_once)
    if mode == "openai":
        from .openai_provider import OpenAIProvider

        if fail_once:
            raise ValueError(
                "--inject-failure only applies to the deterministic provider; "
                "a real model fails on its own schedule"
            )
        return OpenAIProvider(model=model)
    raise ValueError(f"unknown provider mode: {mode!r} (expected 'mock' or 'openai')")
