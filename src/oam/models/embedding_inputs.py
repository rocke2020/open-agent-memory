"""Shared final-input admission; existing caller batches remain indivisible."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from oam.config import EmbeddingModelBinding, OAMConfig, TokenizerConfig
from oam.tokens import TokenCounter, qwen_embedding_counter, resolve_counter

logger = logging.getLogger(__name__)

_OPENAI_MODELS = frozenset(
    {"text-embedding-ada-002", "text-embedding-3-small", "text-embedding-3-large"}
)
_QWEN_MODELS = {
    name: size
    for size in ("0.6b", "4b", "8b")
    for name in (
        f"qwen/qwen3-embedding-{size}",
        f"qwen3-embedding-{size}",
        f"qwen3-embedding:{size}",
    )
}
_KINFRA_MODELS = frozenset({"kinfra-text-embedding-0.6b", "kinfra-text-embedding-4b"})


class EmbeddingInputLimitError(ValueError):
    """No external request was sent because an embedding batch cannot be prepared."""


@dataclass(frozen=True)
class EmbeddingAdmission:
    counter: TokenCounter
    max_input_tokens: int | None
    max_request_tokens: int | None
    max_request_items: int | None
    estimated: bool

    def prepare(self, texts: tuple[str, ...]) -> tuple[str, ...]:
        """Trim per-input payload prefixes, then admit the complete indivisible batch."""
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingInputLimitError("embedding input limit requires nonempty text")
        prepared = []
        warnings = []
        for index, text in enumerate(texts):
            original_tokens = self.counter.count(text)
            if self.max_input_tokens is not None and original_tokens > self.max_input_tokens:
                try:
                    prefix = self.counter.truncate(text, self.max_input_tokens)
                except ValueError as error:
                    raise EmbeddingInputLimitError(
                        "embedding input limit cannot be prepared"
                    ) from error
                retained_tokens = self.counter.count(prefix)
                if not prefix.strip() or retained_tokens > self.max_input_tokens:
                    raise EmbeddingInputLimitError(
                        "embedding input limit cannot fit nonempty prefix"
                    )
                warnings.append((index, original_tokens, retained_tokens))
                text = prefix
            prepared.append(text)
        result = tuple(prepared)
        self.validate(result)
        for index, original_tokens, retained_tokens in warnings:
            logger.warning(
                "embedding_input_trimmed item=%s original_tokens=%s retained_tokens=%s "
                "limit=%s counter=%s",
                index,
                original_tokens,
                retained_tokens,
                self.max_input_tokens,
                self.counter.identity,
            )
        return result

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
    if binding is None:
        return EmbeddingAdmission(resolve_counter(general, environment), None, None, None, True)
    model = binding.model or (environment or {}).get(binding.model_env or "", "").strip()
    model = model.casefold()
    openai = model in _OPENAI_MODELS
    qwen = _QWEN_MODELS.get(model)
    kinfra = model in _KINFRA_MODELS
    selected = binding.tokenizer
    if selected is None:
        counter = resolve_counter(general, environment)
    elif isinstance(selected, TokenizerConfig):
        counter = resolve_counter(selected, environment)
    elif selected == "qwen" or (selected == "auto" and qwen is not None):
        counter = qwen_embedding_counter("0.6b" if qwen in {None, "0.6b"} else "4b-8b")
    else:
        counter = resolve_counter(
            TokenizerConfig(type="cl100k_base" if selected == "openai" or openai else "o200k_base")
        )
    return EmbeddingAdmission(
        counter,
        (
            32768
            if selected == "qwen" or (selected != "openai" and (qwen is not None or kinfra))
            else 8192
        )
        if binding.max_input_tokens == "auto"
        else binding.max_input_tokens,
        (300000 if openai else None)
        if binding.max_request_tokens == "auto"
        else binding.max_request_tokens,
        (2048 if openai else 128 if kinfra else None)
        if binding.max_request_items == "auto"
        else binding.max_request_items,
        # Explicit local vocabulary selection does not itself prove serving parity.
        True,
    )


def configured_embedding_admission(
    config: OAMConfig, environment: Mapping[str, str] | None = None
) -> EmbeddingAdmission:
    return embedding_admission(config.models.embedding, config.tokenizer, environment)
