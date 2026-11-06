"""Selected counting bounds source chunks and exact final embedding inputs."""

from pathlib import Path

import httpx
import pytest

from oam import tokens
from oam.config import load_config
from oam.indexing.chunker import chunk_text, resume_chunks
from oam.models.runtime import RuntimeModels

ROOT = Path(__file__).parents[2]


def configured(tmp_path, extra):
    path = tmp_path / "tokens.yaml"
    path.write_text(extra)
    return load_config(path).config


def test_native_counter_counts_literal_special_tokens_without_changing_legacy(tmp_path):
    config = configured(tmp_path, "tokenizer:\n  type: cl100k_base\n")
    counter = tokens.resolve_counter(config.tokenizer)
    assert counter.count("hello world") == 2
    assert counter.count("<|endoftext|>") > 1
    assert tokens.estimated_tokens(tokens.estimate_units("abcd")) == 1


def test_selected_ingestion_counter_preserves_offsets_and_legacy_metadata(tmp_path):
    config = configured(tmp_path, "tokenizer:\n  type: cl100k_base\n")
    counter = tokens.resolve_counter(config.tokenizer)
    limits = config.chunk.ingestion.model_copy(
        update={"target_estimated_tokens": 4, "max_estimated_tokens": 6}
    )
    text = "0123456789" * 14 + "\n" * 40 + "甲😀é"
    chunks = chunk_text(text, limits, counter=counter)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(chunk.text == text[chunk.start : chunk.end] for chunk in chunks)
    assert all(counter.count(chunk.text) <= 6 for chunk in chunks)
    assert all(
        chunk.estimated_tokens == tokens.estimated_tokens(tokens.estimate_units(chunk.text))
        for chunk in chunks
    )
    resumed = resume_chunks(text, [chunks[0].text], limits, counter=counter)
    assert resumed[0].text == chunks[0].text
    assert "".join(chunk.text for chunk in resumed) == text


@pytest.mark.parametrize(
    "setting,texts",
    [
        ("max_input_tokens: 2", ("hello world", "a b c d")),
        ("max_request_tokens: 3", ("hello world", "hello world")),
        ("max_request_items: 1", ("a", "b")),
    ],
)
def test_embedding_limit_rejects_entire_batch_before_http(tmp_path, setting, texts):
    config = configured(
        tmp_path, f"models:\n  embedding:\n    tokenizer:\n      type: cl100k_base\n    {setting}\n"
    )
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(
            config,
            {
                "OAM_EMBEDDING_MODEL": "synthetic",
                "OAM_EMBEDDING_URL": "https://vector.example/v1",
                "OAM_EMBEDDING_KEY": "synthetic",
            },
            http,
        )
        with pytest.raises(ValueError, match="embedding.*limit"):
            models.embed(texts, timeout_seconds=1)
    assert sent == []


def test_local_tokenizer_requires_explicit_environment_and_counts_untruncated_input(tmp_path):
    import json

    config = configured(
        tmp_path, "tokenizer:\n  type: tokenizer_json\n  file_env: TEST_TOKENIZER_FILE\n"
    )
    asset = tmp_path / "tokenizer.json"
    asset.write_text(
        json.dumps(
            {
                "version": "1.0",
                "truncation": {
                    "direction": "Right",
                    "max_length": 1,
                    "strategy": "LongestFirst",
                    "stride": 0,
                },
                "padding": None,
                "added_tokens": [],
                "normalizer": None,
                "pre_tokenizer": {"type": "Whitespace"},
                "post_processor": None,
                "decoder": None,
                "model": {
                    "type": "WordLevel",
                    "vocab": {"[UNK]": 0, "hello": 1, "world": 2},
                    "unk_token": "[UNK]",
                },
            }
        )
    )
    with pytest.raises(ValueError, match="environment variable"):
        tokens.resolve_counter(config.tokenizer, {})
    counter = tokens.resolve_counter(config.tokenizer, {"TEST_TOKENIZER_FILE": str(asset)})
    assert counter.count("hello world hello") == 3
    copied = tmp_path / "copy.json"
    copied.write_bytes(asset.read_bytes())
    assert (
        tokens.resolve_counter(config.tokenizer, {"TEST_TOKENIZER_FILE": str(copied)}).identity
        == counter.identity
    )


def test_nonmonotonic_counter_never_publishes_an_unchecked_chunk():
    from oam.config import IngestionChunkConfig

    counter = tokens.TokenCounter(
        "nonmonotonic-fixture", lambda text: 1 if len(text) == 5 else len(text)
    )
    limits = IngestionChunkConfig(
        target_estimated_tokens=2,
        max_estimated_tokens=3,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )
    text = "abcdefghijklm"
    chunks = chunk_text(text, limits, counter=counter)
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(counter.count(chunk.text) <= 3 for chunk in chunks)
    assert all(chunk.text == text[chunk.start : chunk.end] for chunk in chunks)


def test_long_unbroken_source_does_not_recount_every_remaining_suffix(tmp_path):
    from oam.tokens import TokenCounter

    config = configured(tmp_path, "")
    limits = config.chunk.ingestion.model_copy(
        update={"target_estimated_tokens": 32, "max_estimated_tokens": 64}
    )
    text = "x" * 65536
    counted_characters = 0

    def count(value):
        nonlocal counted_characters
        counted_characters += len(value)
        return len(value)

    chunks = chunk_text(text, limits, counter=TokenCounter("test", count))
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(len(chunk.text) <= 64 for chunk in chunks)
    assert counted_characters <= 40 * len(text)
