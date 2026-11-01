"""Shared final-input admission; existing caller batches remain indivisible."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from oam.config import EmbeddingModelBinding, OAMConfig, TokenizerConfig
from oam.tokens import TokenCounter, resolve_counter

logger = logging.getLogger(__name__)


class EmbeddingInputLimitError(ValueError):
    """No external request was sent because final embedding input is invalid."""


@dataclass(frozen=True)
class EmbeddingAdmission:
    counter: TokenCounter
    max_input_tokens: int | None
    max_request_tokens: int | None
    max_request_items: int | None
    estimated: bool

    def fits(self, text: str) -> bool:
        count = self.counter.count(text)
        return all(
            bound is None or count <= bound
            for bound in (self.max_input_tokens, self.max_request_tokens)
        )

    def validate(self, texts: tuple[str, ...]) -> tuple[int, ...]:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingInputLimitError("embedding input limit requires nonempty text")
        counts = tuple(self.counter.count(text) for text in texts)
        if self.max_input_tokens is not None and any(
            count > self.max_input_tokens for count in counts
        ):
            logger.info(
                "embedding_admission counter=%s outcome=rejected "
                "reason=input_tokens actual=%s limit=%s",
                self.counter.identity,
                max(counts),
                self.max_input_tokens,
            )
            raise EmbeddingInputLimitError("embedding per-input token limit exceeded")
        if self.max_request_tokens is not None and sum(counts) > self.max_request_tokens:
            logger.info(
                "embedding_admission counter=%s outcome=rejected "
                "reason=request_tokens actual=%s limit=%s",
                self.counter.identity,
                sum(counts),
                self.max_request_tokens,
            )
            raise EmbeddingInputLimitError("embedding request token limit exceeded")
        if self.max_request_items is not None and len(texts) > self.max_request_items:
            logger.info(
                "embedding_admission counter=%s outcome=rejected "
                "reason=request_items actual=%s limit=%s",
                self.counter.identity,
                len(texts),
                self.max_request_items,
            )
            raise EmbeddingInputLimitError("embedding request item limit exceeded")
        logger.info(
            "embedding_admission counter=%s estimated=%s items=%s tokens=%s "
            "max_input=%s max_request=%s max_items=%s outcome=accepted",
            self.counter.identity,
            self.estimated,
            len(texts),
            sum(counts),
            self.max_input_tokens,
            self.max_request_tokens,
            self.max_request_items,
        )
        return counts


def embedding_admission(
    binding: EmbeddingModelBinding | None,
    general: TokenizerConfig,
    environment: Mapping[str, str] | None = None,
) -> EmbeddingAdmission:
    return EmbeddingAdmission(
        resolve_counter(
            binding.tokenizer if binding is not None and binding.tokenizer is not None else general,
            environment,
        ),
        binding.max_input_tokens if binding else None,
        binding.max_request_tokens if binding else None,
        binding.max_request_items if binding else None,
        # Explicit local vocabulary selection does not itself prove serving parity.
        True,
    )


def configured_embedding_admission(
    config: OAMConfig, environment: Mapping[str, str] | None = None
) -> EmbeddingAdmission:
    return embedding_admission(config.models.embedding, config.tokenizer, environment)
