import json
from pathlib import Path

import httpx
import pytest

from oam.config import ModelsConfig, load_config
from oam.models.bindings import ModelBindingResolutionError, resolve_model_binding
from oam.models.runtime import ModelOutputError, RuntimeModels


def decision_config(enabled=True):
    base = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    values = base.models.model_dump(mode="json")
    values["decision"] = {
        "enabled": enabled,
        "protocol": "system_one",
        "model_env": "DECISION_MODEL",
        "endpoint_env": "DECISION_BASE_URL",
        "api_key_env": "DECISION_API_KEY",
        "capabilities": ["decision"],
    }
    return base.model_copy(update={"models": ModelsConfig.model_validate(values)})


ENV = {
    "DECISION_MODEL": "jev-1.13.0",
    "DECISION_BASE_URL": "https://decision.example/v1",
    "DECISION_API_KEY": "synthetic-decision-secret",
}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("model", [None, "", "  ", "jev-1.13.0"])
def test_decision_requires_yaml_enablement_and_nonblank_model(enabled, model):
    environment = {**ENV}
    if model is None:
        environment.pop("DECISION_MODEL")
    else:
        environment["DECISION_MODEL"] = model
    binding = resolve_model_binding(decision_config(enabled).models, "decision", environment)
    if enabled and model and model.strip():
        assert binding is not None
        assert binding.alias == "decision"
        assert binding.model == model
        assert binding.reasoning_effort is None
        assert binding.endpoint == ENV["DECISION_BASE_URL"]
        assert ENV["DECISION_API_KEY"] not in repr(binding)
    else:
        assert binding is None


@pytest.mark.parametrize("enabled,environment", [(False, ENV), (True, {})])
def test_disabled_decision_does_not_require_endpoint_or_key(enabled, environment):
    environment = {key: value for key, value in environment.items() if key == "DECISION_MODEL"}
    assert resolve_model_binding(decision_config(enabled).models, "decision", environment) is None


@pytest.mark.parametrize("missing", ["DECISION_BASE_URL", "DECISION_API_KEY"])
def test_active_decision_requires_its_own_route_and_credential(missing):
    environment = {key: value for key, value in ENV.items() if key != missing}
    with pytest.raises(ModelBindingResolutionError, match=missing):
        resolve_model_binding(decision_config().models, "decision", environment)


def test_decision_model_name_is_an_opaque_operator_choice():
    binding = resolve_model_binding(
        decision_config().models, "decision", {**ENV, "DECISION_MODEL": "operator/model-v2"}
    )
    assert binding is not None
    assert binding.model == "operator/model-v2"


def questions():
    from oam.models.decision import DecisionQuestion

    return {
        "pick": DecisionQuestion(
            type="choice", instructions="Pick a color.", criteria={"red": "Red", "blue": "Blue"}
        ),
        "yes": DecisionQuestion(type="noul", instructions="Is it red?"),
        "level": DecisionQuestion(
            type="score", instructions="Rate redness.", criteria=["None", "Some", "Strong"]
        ),
    }


def response_body():
    return {
        "model": "provider-reported-model",
        "answers": {
            "pick": {
                "type": "choice",
                "choice": "red",
                "confidence": 0.9,
                "probabilities": {"red": 0.9, "blue": 0.1},
            },
            "yes": {"type": "noul", "noul": 0.95},
            "level": {"type": "score", "score": 1.8, "legend": {"0": "None", "2": "Strong"}},
        },
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


@pytest.mark.parametrize("suffix", ["", "/systemone", "/systemone/"])
def test_general_decision_runtime_uses_jev_wire_and_typed_results(suffix):
    def respond(request):
        assert str(request.url) == "https://decision.example/v1/systemone"
        assert request.headers["authorization"] == "Bearer synthetic-decision-secret"
        body = json.loads(request.content)
        assert body == {
            "model": ENV["DECISION_MODEL"],
            "state": "A red block.",
            "questions": {k: v.model_dump(exclude_none=True) for k, v in questions().items()},
        }
        assert "max_tokens" not in body and "messages" not in body
        return httpx.Response(200, json=response_body())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = RuntimeModels(
            decision_config(), {**ENV, "DECISION_BASE_URL": ENV["DECISION_BASE_URL"] + suffix}, http
        ).decide("A red block.", questions(), timeout_seconds=2)
    assert result.answers["pick"].choice == "red"
    assert result.answers["yes"].noul == 0.95
    assert result.answers["level"].score == 1.8
    assert result.answers["level"].legend == {"0": "None", "2": "Strong"}
    assert result.model == "provider-reported-model"
    assert result.usage.total_tokens == 15
    assert result.usage.reasoning_tokens is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"error": {"message": "business rejected despite HTTP 200"}},
        {"answers": {}},
        {"answers": {"extra": {"type": "choice", "choice": "red"}}},
    ],
)
def test_decision_rejects_envelope_success_without_requested_answers(bad):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=bad))
        ) as http,
        pytest.raises(ModelOutputError),
    ):
        RuntimeModels(decision_config(), ENV, http).decide(
            "A red block.", questions(), timeout_seconds=2
        )


@pytest.mark.parametrize(
    "key,answer",
    [
        ("pick", {"type": "choice", "choice": "unknown"}),
        ("pick", {"type": "score", "score": 1}),
        ("pick", {"type": "choice", "choice": "red", "probabilities": {"unknown": 1}}),
        ("pick", {"type": "choice", "choice": "red", "confidence": True}),
        ("yes", {"type": "noul", "noul": 1.01}),
        ("yes", {"type": "noul", "noul": "0.9"}),
        ("yes", {"type": "noul", "noul": float("inf")}),
        ("level", {"type": "score", "score": 3}),
        ("level", {"type": "score", "score": True}),
        ("pick", {"type": "refusal"}),
    ],
)
def test_decision_validates_answer_semantics_and_retains_usage(key, answer):
    from oam.models.decision import parse_decision_response

    body = response_body()
    body["answers"][key] = answer
    with pytest.raises(ModelOutputError) as captured:
        parse_decision_response(body, questions())
    assert captured.value.usage.total_tokens == 15


def test_decision_missing_usage_is_unknown_and_choice_is_not_replaced_by_probability():
    from oam.models.decision import parse_decision_response

    body = response_body()
    body.pop("usage")
    body["answers"]["pick"]["probabilities"] = {"red": 0.1, "blue": 0.9}
    result = parse_decision_response(body, questions())
    assert result.answers["pick"].choice == "red"
    assert result.usage.coverage == "unavailable"
    assert result.usage.total_tokens is None


@pytest.mark.parametrize("enabled,environment", [(False, ENV), (True, {})])
def test_disabled_runtime_never_dispatches(enabled, environment):
    def reject(request):
        pytest.fail("disabled decision dispatched HTTP")

    with (
        httpx.Client(transport=httpx.MockTransport(reject)) as http,
        pytest.raises(ValueError, match="not configured"),
    ):
        RuntimeModels(decision_config(enabled), environment, http).decide(
            "state", questions(), timeout_seconds=2
        )


@pytest.mark.parametrize("status", [429, 500])
def test_decision_transport_has_one_attempt(status):
    attempts = []

    def respond(request):
        attempts.append(request)
        return httpx.Response(status)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        pytest.raises(httpx.HTTPStatusError),
    ):
        RuntimeModels(decision_config(), ENV, http).decide("state", questions(), timeout_seconds=2)
    assert len(attempts) == 1


def test_decision_question_validates_rubric_and_supports_single_choice():
    from oam.models.decision import DecisionQuestion

    assert DecisionQuestion(type="choice", instructions="Pick.", criteria={"one": None})
    for values in [
        {"type": "choice", "instructions": "Pick.", "criteria": {}},
        {"type": "choice", "instructions": " ", "criteria": {"one": "One"}},
        {"type": "score", "instructions": "Rate.", "criteria": ["Only"]},
        {"type": "noul", "instructions": "True?", "criteria": ["Wrong"]},
    ]:
        with pytest.raises(ValueError):
            DecisionQuestion.model_validate(values)


def test_startup_validates_active_decision_without_making_requests():
    from oam.config import ConfigurationError
    from oam.startup import validate_model_environment

    config = decision_config()
    environment = {
        **ENV,
        "VLM_MODEL": "vlm",
        "VLM_BASE_URL": "https://vlm.example/v1",
        "VLM_API_KEY": "vlm-key",
        "OAM_EMBEDDING_MODEL": "embed",
        "OAM_EMBEDDING_URL": "https://embed.example/v1",
        "OAM_EMBEDDING_KEY": "embed-key",
    }
    validate_model_environment(config, environment)
    environment.pop("DECISION_API_KEY")
    with pytest.raises(ConfigurationError, match="DECISION_API_KEY"):
        validate_model_environment(config, environment)
    environment.pop("DECISION_MODEL")
    validate_model_environment(config, environment)


@pytest.mark.parametrize(
    "outcome", ["good", "wrong", "missing", "error", "disabled", "missing_model"]
)
def test_preflight_probes_active_decision_semantics_and_gates_readiness(outcome):
    from oam.models.client import OpenAICompatibleProbeClient
    from oam.models.preflight import run_model_preflight

    config = decision_config(enabled=outcome != "disabled")
    values = config.models.model_dump(mode="json")
    values.update({"vlm": None, "embedding": None, "reasoning_effort": {}})
    models = ModelsConfig.model_validate(values)
    environment = {} if outcome == "missing_model" else ENV
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if outcome == "error":
            return httpx.Response(200, json={"error": {"message": "rejected"}})
        return httpx.Response(
            200,
            json={
                "answers": {}
                if outcome == "missing"
                else {
                    "color": {"type": "choice", "choice": "red" if outcome == "good" else "blue"},
                    "positive": {"type": "noul", "noul": 0.99},
                    "negative": {"type": "noul", "noul": 0.01},
                    "level": {"type": "score", "score": 1},
                },
                "usage": {"input_tokens": 20, "output_tokens": 4},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        report = run_model_preflight(
            models, environment, OpenAICompatibleProbeClient(http=http), required_roles=frozenset()
        )
    role = next(role for role in report.roles if role.role == "decision")
    assert role.status.value == (
        "disabled"
        if outcome in {"disabled", "missing_model"}
        else "passed"
        if outcome == "good"
        else "failed"
    )
    assert report.runnable is (outcome in {"good", "disabled", "missing_model"})
    assert len(requests) == (0 if outcome in {"disabled", "missing_model"} else 1)
    assert "synthetic-decision-secret" not in str(report)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("model", [None, "jev-1.13.0"])
def test_fresh_cli_preflight_respects_yaml_and_environment(tmp_path, enabled, model):
    import os
    import subprocess
    import sys

    path = tmp_path / "operator.yaml"
    path.write_text("models:\n  decision:\n    enabled: " + str(enabled).lower() + "\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DECISION_MODEL",
            "DECISION_BASE_URL",
            "DECISION_API_KEY",
            "VLM_MODEL",
            "OAM_EMBEDDING_MODEL",
        }
    }
    if model is not None:
        environment["DECISION_MODEL"] = model
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from oam.cli import main; "
            "raise SystemExit(main(['preflight','--json','--config',sys.argv[1]]))",
            str(path),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    role = next(role for role in payload["roles"] if role["role"] == "decision")
    assert role["status"] == ("failed" if enabled and model else "disabled")
    if enabled and model:
        assert "DECISION_BASE_URL" in role["detail"]


def test_decision_timeout_does_not_retry_or_hold_runtime_capacity():
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json=response_body())

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(decision_config(), ENV, http)
        with pytest.raises(httpx.ReadTimeout):
            models.decide("state", questions(), timeout_seconds=2)
        assert len(calls) == 1
        assert (
            models.decide("state", questions(), timeout_seconds=2).answers["pick"].choice == "red"
        )
    assert len(calls) == 2


@pytest.mark.parametrize("location", ["envelope", "answer"])
@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_decision_rejects_incomplete_answers(location, reason):
    from oam.models.decision import parse_decision_response

    body = response_body()
    target = body if location == "envelope" else body["answers"]["pick"]
    target["finish_reason"] = reason
    with pytest.raises(ModelOutputError):
        parse_decision_response(body, questions())


def test_decision_oversized_numeric_answer_is_a_model_output_error():
    from oam.models.decision import parse_decision_response

    body = response_body()
    body["answers"]["yes"]["noul"] = 10**400
    with pytest.raises(ModelOutputError) as captured:
        parse_decision_response(body, questions())
    assert captured.value.usage.total_tokens == 15
