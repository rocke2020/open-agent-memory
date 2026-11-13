"""Typed System One judgments; consumers retain semantic and publication policy."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from oam.models.runtime import FrozenResult, ModelOutputError, Usage, _usage


class DecisionQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["choice", "noul", "score"]
    instructions: str
    criteria: dict[str, str | None] | list[str] | None = None

    @model_validator(mode="after")
    def validate_question(self) -> DecisionQuestion:
        if not self.instructions.strip():
            raise ValueError("decision instructions cannot be empty")
        if self.type == "choice":
            if not isinstance(self.criteria, dict) or not self.criteria:
                raise ValueError("choice requires nonempty named criteria")
        elif self.type == "score":
            if not isinstance(self.criteria, list) or len(self.criteria) < 2:
                raise ValueError("score requires ordered criteria with at least two levels")
        elif self.criteria is not None and (
            not isinstance(self.criteria, dict) or set(self.criteria) != {"true", "false"}
        ):
            raise ValueError("noul criteria must name true and false")
        if self.criteria is not None and any(not key.strip() for key in self.criteria):
            raise ValueError("decision criteria labels cannot be empty")
        return self


class DecisionAnswer(FrozenResult):
    type: Literal["choice", "noul", "score"]
    choice: str | None = None
    noul: float | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    legend: dict[str, str] | None = None


class DecisionResult(FrozenResult):
    answers: dict[str, DecisionAnswer]
    usage: Usage
    model: str | None = None


def decision_request(
    model: str, state: str, questions: dict[str, DecisionQuestion]
) -> dict[str, Any]:
    if not isinstance(state, str) or not state.strip():
        raise ValueError("decision state must be nonempty text")
    if not questions or any(not isinstance(key, str) or not key.strip() for key in questions):
        raise ValueError("decision requires nonempty named questions")
    if any(not isinstance(question, DecisionQuestion) for question in questions.values()):
        raise ValueError("decision requires typed questions")
    return {
        "model": model,
        "state": state,
        "questions": {
            key: question.model_dump(exclude_none=True) for key, question in questions.items()
        },
    }


def decision_path(endpoint: str) -> str:
    return "" if endpoint.endswith("/systemone") else "/systemone"


def _number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid decision number")
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError("invalid decision number")
    return float(value)


def parse_decision_response(
    body: dict[str, Any], questions: dict[str, DecisionQuestion]
) -> DecisionResult:
    usage = _usage(body.get("usage"))
    try:
        if body.get("error") is not None or body.get("refusal"):
            raise ValueError
        if body.get("finish_reason") not in (None, "stop"):
            raise ValueError
        raw = body.get("answers")
        if not isinstance(raw, dict) or set(raw) != set(questions):
            raise ValueError
        answers = {}
        for key, question in questions.items():
            item = raw[key]
            if not isinstance(item, dict) or item.get("type") != question.type:
                raise ValueError
            if item.get("refusal") or item.get("error") is not None:
                raise ValueError
            if item.get("finish_reason") not in (None, "stop"):
                raise ValueError
            values: dict[str, Any] = {"type": question.type}
            if question.type == "choice":
                choice = item.get("choice")
                if not isinstance(choice, str) or choice not in (question.criteria or {}):
                    raise ValueError
                values["choice"] = choice
            elif question.type == "noul":
                values["noul"] = _number(item.get("noul"), 0, 1)
            else:
                values["score"] = _number(item.get("score"), 0, len(question.criteria or []) - 1)
            if item.get("confidence") is not None:
                values["confidence"] = _number(item["confidence"], 0, 1)
            if item.get("probabilities") is not None:
                probabilities = item["probabilities"]
                labels = (
                    set(question.criteria or {})
                    if question.type == "choice"
                    else {"true", "false"}
                    if question.type == "noul"
                    else {str(index) for index in range(len(question.criteria or []))}
                )
                if not isinstance(probabilities, dict) or not set(probabilities) <= labels:
                    raise ValueError
                values["probabilities"] = {
                    label: _number(value, 0, 1) for label, value in probabilities.items()
                }
            if item.get("legend") is not None:
                legend = item["legend"]
                if not isinstance(legend, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in legend.items()
                ):
                    raise ValueError
                values["legend"] = legend
            answers[key] = DecisionAnswer(**values)
        model = body.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise ModelOutputError(
            "decision response has invalid or refused answers", usage=usage
        ) from exc
    return DecisionResult(answers=answers, usage=usage, model=model)
