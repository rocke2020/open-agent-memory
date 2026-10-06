import pytest

from oam.config import ModelBinding, ModelCapability, ModelRoles, ModelsConfig
from oam.models.bindings import ModelBindingResolutionError, resolve_model_binding


def _models() -> ModelsConfig:
    return ModelsConfig(
        roles=ModelRoles(extraction="primary"),
        bindings={
            "primary": ModelBinding(
                protocol="openai_compatible",
                model="example-chat",
                endpoint_env="OAM_CHAT_URL",
                api_key_env="OAM_CHAT_KEY",
                capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION}),
            )
        },
    )


def test_resolver_freezes_endpoint_and_redacts_secret_from_all_public_views() -> None:
    environment = {
        "OAM_CHAT_URL": "https://models.example.test/v1/",
        "OAM_CHAT_KEY": "super-secret-key",
    }

    resolved = resolve_model_binding(_models(), "extraction", environment)
    environment["OAM_CHAT_URL"] = "https://changed.example.test/v1"
    environment["OAM_CHAT_KEY"] = "changed-secret"

    assert resolved is not None
    assert resolved.endpoint == "https://models.example.test/v1"
    assert resolved.model == "example-chat"
    assert resolved.credential_configured is True
    assert resolved.redacted()["thinking"] is None
    assert "super-secret-key" not in repr(resolved)
    assert "super-secret-key" not in str(resolved.redacted())
    assert "changed-secret" not in str(resolved.redacted())
    assert "api_key" not in resolved.redacted()


def test_resolver_reads_model_from_environment_without_later_mutation() -> None:
    models = ModelsConfig(
        roles=ModelRoles(extraction="primary"),
        bindings={
            "primary": ModelBinding(
                protocol="openai_compatible",
                model_env="OAM_CHAT_MODEL",
                endpoint_env="OAM_CHAT_URL",
                capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION}),
            )
        },
    )
    environment = {
        "OAM_CHAT_MODEL": "environment-chat",
        "OAM_CHAT_URL": "https://models.example.test/v1",
    }

    resolved = resolve_model_binding(models, "extraction", environment)
    environment["OAM_CHAT_MODEL"] = "changed-chat"

    assert resolved is not None
    assert resolved.model == "environment-chat"


@pytest.mark.parametrize("model_fields", [{}, {"model": "literal", "model_env": "MODEL"}])
def test_model_binding_requires_exactly_one_model_source(model_fields) -> None:
    with pytest.raises(ValueError):
        ModelBinding(
            protocol="openai_compatible",
            endpoint_env="OAM_CHAT_URL",
            capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION}),
            **model_fields,
        )


def test_resolver_fails_closed_when_endpoint_or_declared_key_is_missing() -> None:
    with pytest.raises(ModelBindingResolutionError, match="OAM_CHAT_URL"):
        resolve_model_binding(_models(), "extraction", {})

    with pytest.raises(ModelBindingResolutionError, match="OAM_CHAT_KEY"):
        resolve_model_binding(
            _models(),
            "extraction",
            {"OAM_CHAT_URL": "https://models.example.test/v1"},
        )


def test_resolver_fails_closed_when_environment_model_is_missing() -> None:
    models = ModelsConfig(
        roles=ModelRoles(extraction="primary"),
        bindings={
            "primary": ModelBinding(
                protocol="openai_compatible",
                model_env="OAM_CHAT_MODEL",
                endpoint_env="OAM_CHAT_URL",
                capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION}),
            )
        },
    )

    with pytest.raises(ModelBindingResolutionError, match="OAM_CHAT_MODEL"):
        resolve_model_binding(models, "extraction", {"OAM_CHAT_URL": "http://model.test"})


def test_resolver_reports_an_unbound_optional_role_without_inference() -> None:
    assert resolve_model_binding(_models(), "planner", {}) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret-in-url@models.example.test/v1",
        "https://models.example.test/v1?api_key=secret-in-url",
        "https://models.example.test/v1#secret-in-url",
    ],
)
def test_resolver_rejects_secret_bearing_endpoint_forms_without_echoing(endpoint: str) -> None:
    with pytest.raises(ModelBindingResolutionError) as captured:
        resolve_model_binding(
            _models(),
            "extraction",
            {"OAM_CHAT_URL": endpoint, "OAM_CHAT_KEY": "separate-secret"},
        )

    assert "OAM_CHAT_URL" in str(captured.value)
    assert "secret-in-url" not in str(captured.value)


def test_chat_parameters_fail_closed_without_explicit_effort_or_thinking() -> None:
    resolved = resolve_model_binding(
        _models(),
        "extraction",
        {"OAM_CHAT_URL": "https://models.example.test/v1", "OAM_CHAT_KEY": "secret"},
    )
    assert resolved is not None
    with pytest.raises(ModelBindingResolutionError, match="explicit reasoning_effort and thinking"):
        resolved.chat_parameters()
