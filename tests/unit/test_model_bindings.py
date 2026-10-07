import pytest

from oam.config import ModelBinding, ModelCapability, ModelsConfig, ReasoningEfforts
from oam.models.bindings import ModelBindingResolutionError, resolve_model_binding


def test_one_vlm_is_shared_by_roles_with_independent_reasoning_effort() -> None:
    models = ModelsConfig.model_validate(
        {
            "vlm": {
                "protocol": "openai_chat",
                "model_env": "VLM_MODEL",
                "endpoint_env": "VLM_URL",
                "api_key_env": "VLM_KEY",
                "capabilities": ["text", "vision"],
            },
            "reasoning_effort": {
                "extraction": "low",
                "insights": "high",
                "abstract": "none",
                "profile": "max",
                "planner": None,
            },
        }
    )
    environment = {
        "VLM_MODEL": "one-service-model",
        "VLM_URL": "https://vlm.example.test/v1",
        "VLM_KEY": "synthetic-secret",
    }
    resolved = {}
    for role in ("extraction", "insights", "abstract", "profile"):
        binding = resolve_model_binding(models, role, environment)
        assert binding is not None
        resolved[role] = binding
    assert {binding.model for binding in resolved.values()} == {"one-service-model"}
    assert {binding.endpoint for binding in resolved.values()} == {environment["VLM_URL"]}
    assert {binding.protocol for binding in resolved.values()} == {"openai_chat"}
    assert resolved["extraction"].chat_parameters() == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert resolved["insights"].chat_parameters()["reasoning_effort"] == "high"
    assert resolved["profile"].chat_parameters()["reasoning_effort"] == "max"
    assert resolved["abstract"].chat_parameters() == {"thinking": {"type": "disabled"}}
    assert resolve_model_binding(models, "planner", {}) is None


def _models() -> ModelsConfig:
    return ModelsConfig(
        reasoning_effort=ReasoningEfforts(extraction="low"),
        vlm=ModelBinding(
            protocol="openai_chat",
            model="example-chat",
            endpoint_env="OAM_CHAT_URL",
            api_key_env="OAM_CHAT_KEY",
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
        ),
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
    assert resolved.redacted()["thinking"] == "enabled"
    assert "super-secret-key" not in repr(resolved)
    assert "super-secret-key" not in str(resolved.redacted())
    assert "changed-secret" not in str(resolved.redacted())
    assert "api_key" not in resolved.redacted()


def test_resolver_reads_model_from_environment_without_later_mutation() -> None:
    models = ModelsConfig(
        reasoning_effort=ReasoningEfforts(extraction="low"),
        vlm=ModelBinding(
            protocol="openai_chat",
            model_env="OAM_CHAT_MODEL",
            endpoint_env="OAM_CHAT_URL",
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
        ),
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
            protocol="openai_chat",
            endpoint_env="OAM_CHAT_URL",
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
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
        reasoning_effort=ReasoningEfforts(extraction="low"),
        vlm=ModelBinding(
            protocol="openai_chat",
            model_env="OAM_CHAT_MODEL",
            endpoint_env="OAM_CHAT_URL",
            capabilities=frozenset({ModelCapability.TEXT, ModelCapability.VISION}),
        ),
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


@pytest.mark.parametrize("effort", ["medium", "enabled", "", True])
def test_reasoning_effort_rejects_values_outside_the_oam_contract(effort) -> None:
    with pytest.raises(ValueError, match="extraction"):
        ReasoningEfforts(extraction=effort)


def test_model_binding_rejects_an_independent_thinking_setting() -> None:
    with pytest.raises(ValueError, match="thinking"):
        ModelBinding.model_validate(
            {
                "protocol": "openai_chat",
                "model": "synthetic",
                "endpoint_env": "URL",
                "capabilities": ["text", "vision"],
                "thinking": "disabled",
            }
        )
