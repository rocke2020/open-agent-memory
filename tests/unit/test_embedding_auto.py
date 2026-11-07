"""Automatic embedding policies are exact-name defaults, independent of overrides."""

import pytest
from pydantic import ValidationError

from oam.config import EmbeddingModelBinding, TokenizerConfig
from oam.models.embedding_inputs import embedding_admission


def binding(model="text-embedding-3-small", **overrides):
    return EmbeddingModelBinding.model_validate(
        {
            "protocol": "openai_compatible",
            "model": model,
            "endpoint_env": "UNSET_URL",
            "capabilities": ["embeddings"],
            **overrides,
        }
    )


GENERAL = TokenizerConfig(type="oam-unicode-quarter-v1")


def test_openai_auto_admits_full_input_and_bounds_entire_request_without_credentials():
    admission = embedding_admission(binding(), GENERAL, {})
    assert admission.counter.identity == "cl100k_base"
    assert (
        admission.max_input_tokens,
        admission.max_request_tokens,
        admission.max_request_items,
    ) == (8192, 300000, 2048)
    assert admission.validate(("hello world",)) == (2,)


@pytest.mark.parametrize("field", ["max_input_tokens", "max_request_tokens", "max_request_items"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, -1, "unknown"])
def test_embedding_limit_overrides_require_strict_positive_integers(field, value):
    with pytest.raises(ValidationError):
        binding(**{field: value})


@pytest.mark.parametrize(
    "model,counter,limits",
    [
        ("TEXT-EMBEDDING-ADA-002", "cl100k_base", (8192, 300000, 2048)),
        ("text-embedding-3-large", "cl100k_base", (8192, 300000, 2048)),
        ("kinfra-text-embedding-0.6b", "o200k_base", (32768, None, 128)),
        ("KINFRA-TEXT-EMBEDDING-4B", "o200k_base", (32768, None, 128)),
        ("unknown", "o200k_base", (8192, None, None)),
        ("spoof/text-embedding-3-small", "o200k_base", (8192, None, None)),
        ("other/Qwen3-Embedding-0.6B", "o200k_base", (8192, None, None)),
        ("Qwen/Qwen3-Embedding-0.6B-extra", "o200k_base", (8192, None, None)),
        ("Qwen/Qwen3-Reranker-0.6B", "o200k_base", (8192, None, None)),
        ("Qwen/Qwen3-VL-Embedding-2B", "o200k_base", (8192, None, None)),
        ("Qwen/Qwen3-0.6B", "o200k_base", (8192, None, None)),
        ("text-embedding-v4", "o200k_base", (8192, None, None)),
        ("kinfra-text-embedding-r3", "o200k_base", (8192, None, None)),
    ],
)
def test_exact_model_allowlist(model, counter, limits):
    admission = embedding_admission(binding(model), GENERAL)
    assert admission.counter.identity == counter
    assert (
        admission.max_input_tokens,
        admission.max_request_tokens,
        admission.max_request_items,
    ) == limits
    assert admission.estimated is True


@pytest.mark.parametrize("size", ["0.6B", "4B", "8B"])
@pytest.mark.parametrize(
    "name", ["Qwen/Qwen3-Embedding-{}", "Qwen3-Embedding-{}", "qwen3-embedding:{}"]
)
def test_qwen_auto_uses_variant_asset_and_eos(size, name):
    admission = embedding_admission(binding(name.format(size)), GENERAL)
    digest = (
        "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"
        if size == "0.6B"
        else "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d"
    )
    assert admission.counter.identity == f"tokenizer_json:{digest}:special=true"
    assert admission.max_input_tokens == 32768
    assert admission.max_request_tokens is None
    assert admission.max_request_items is None
    assert admission.validate(("hello",) * 64) == (2,) * 64


@pytest.mark.parametrize(
    "text,count",
    [
        ("hello", 2),
        ("hello world", 3),
        ("Hello 世界", 4),
        ("é café 東京 مرحبا", 9),
        ("नमस्ते தமிழ் ไทย", 15),
        ("<|endoftext|>", 2),
        ("A\nB\t12345", 10),
        ("<think>", 2),
        ("<tool_response>", 2),
    ],
)
def test_explicit_qwen_fallback_counts_official_embedding_vocabulary(text, count):
    admission = embedding_admission(binding("unknown", tokenizer="qwen"), GENERAL)
    assert admission.counter.count(text) == count
    assert admission.max_input_tokens == 32768


@pytest.mark.parametrize("limit,expected", [("auto", 32768), (8192, 8192)])
def test_qwen_preset_supplies_32k_for_online_model_but_respects_numeric_override(limit, expected):
    admission = embedding_admission(
        binding("online-qwen", tokenizer="qwen", max_input_tokens=limit), GENERAL
    )
    assert admission.max_input_tokens == expected
    assert admission.max_request_tokens is None
    assert admission.max_request_items is None
    original = ("hello " * 9000,)
    prepared = admission.prepare(original)
    assert (prepared == original) == (limit == "auto")
    assert admission.counter.count(prepared[0]) <= expected


def test_legacy_null_and_absent_binding_preserve_general_and_unbounded_behavior():
    for selected in (
        None,
        binding(
            tokenizer=None,
            max_input_tokens=None,
            max_request_tokens=None,
            max_request_items=None,
        ),
    ):
        admission = embedding_admission(selected, GENERAL)
        assert admission.counter.identity == "oam-unicode-quarter-v1"
        assert admission.validate(("abcd",) * 64) == (1,) * 64
        assert (
            admission.max_input_tokens,
            admission.max_request_tokens,
            admission.max_request_items,
        ) == (None, None, None)


def test_model_environment_selects_request_policy_and_scalar_preset_selects_input_limit():
    selected = binding(None, model_env="VECTOR_MODEL", tokenizer="openai", max_request_items=9)
    admission = embedding_admission(selected, GENERAL, {"VECTOR_MODEL": "Qwen/Qwen3-Embedding-4B"})
    assert admission.counter.identity == "cl100k_base"
    assert admission.max_input_tokens == 8192
    assert admission.max_request_items == 9
    assert admission.max_request_tokens is None


@pytest.mark.parametrize("model", ["Qwen/Qwen3-Embedding-4B", "kinfra-text-embedding-0.6b"])
def test_openai_scalar_preset_uses_8192_without_changing_request_policy(model):
    admission = embedding_admission(binding(model, tokenizer="openai"), GENERAL)
    assert admission.counter.identity == "cl100k_base"
    assert admission.max_input_tokens == 8192
    assert admission.max_request_tokens is None
    assert admission.max_request_items == (128 if model.startswith("kinfra") else None)


@pytest.mark.parametrize("alias", ["qwen3.5", "o200k_base", "unknown", "AUTO", 1, True])
def test_unknown_scalar_tokenizer_is_rejected(alias):
    with pytest.raises(ValidationError):
        binding(tokenizer=alias)


def test_qwen_variants_distinguish_added_tags_and_share_content_identity(tmp_path):
    import gzip
    from importlib.resources import files

    small = embedding_admission(binding("Qwen/Qwen3-Embedding-0.6B"), GENERAL)
    large = embedding_admission(binding("Qwen/Qwen3-Embedding-4B"), GENERAL)
    largest = embedding_admission(binding("Qwen/Qwen3-Embedding-8B"), GENERAL)
    assert [small.counter.count(tag) for tag in ("<think>", "<tool_response>")] == [2, 2]
    assert [large.counter.count(tag) for tag in ("<think>", "<tool_response>")] == [4, 5]
    assert large.counter is largest.counter
    path = tmp_path / "copied-tokenizer.json"
    path.write_bytes(
        gzip.decompress(
            files("oam").joinpath("tokenizer_assets", "qwen3-embedding-0.6b.json.gz").read_bytes()
        )
    )
    explicit = embedding_admission(
        binding(
            tokenizer=TokenizerConfig(
                type="tokenizer_json", file_env="TOKENIZER", add_special_tokens=True
            )
        ),
        GENERAL,
        {"TOKENIZER": str(path)},
    )
    assert explicit.counter.identity == small.counter.identity


def model_config(selected):
    from oam.config import ModelsConfig, load_config
    from oam.paths import default_config_path

    config = load_config(default_config_path()).config
    return config.model_copy(
        update={
            "models": ModelsConfig(embedding=selected),
            "index": config.index.model_copy(update={"embedding_dimensions": 2}),
        }
    )


def test_auto_and_manual_effective_source_recipe_match():
    from oam.indexing.identity import index_generation

    auto = model_config(binding())
    manual = model_config(
        binding(
            tokenizer=TokenizerConfig(type="cl100k_base"),
            max_input_tokens=8192,
            max_request_tokens=300000,
            max_request_items=2048,
        )
    )
    env = {"UNSET_URL": "https://vector.test/v1"}
    assert index_generation(auto, env) == index_generation(manual, env)
    schedule = model_config(binding(max_request_tokens=200000, max_request_items=16))
    assert index_generation(auto, env) == index_generation(schedule, env)
    changed = model_config(binding(max_input_tokens=8191))
    assert index_generation(auto, env) != index_generation(changed, env)


@pytest.mark.parametrize("model", ["text-embedding-3-small", "Qwen/Qwen3-Embedding-4B", "unknown"])
def test_runtime_preflight_and_worker_resolve_same_environment_snapshot(model):
    import json

    import httpx

    from oam.models.bindings import resolve_model_binding
    from oam.models.client import OpenAICompatibleProbeClient
    from oam.models.runtime import RuntimeModels
    from oam.workers.index_source import _embedding_admission

    config = model_config(binding(None, model_env="VECTOR_MODEL"))
    environment = {"VECTOR_MODEL": model, "UNSET_URL": "https://vector.test/v1"}
    payloads = []

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 0.0]} for i in range(len(payload["input"]))
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        runtime = RuntimeModels(config, environment, http)
        preflight = OpenAICompatibleProbeClient(http=http, config=config, environment=environment)
        bare = OpenAICompatibleProbeClient(http=http)
        resolved = resolve_model_binding(config.models, "embedding", environment)
        assert resolved is not None
        environment["VECTOR_MODEL"] = "another-model"
        assert _embedding_admission(config, runtime) is runtime.embedding_admission
        assert runtime.embedding_admission == preflight._embedding_admission
        runtime.embed(("hello",) * 16, timeout_seconds=1)
        preflight.probe_embeddings(resolved, ("hello",) * 64)
        bare.probe_embeddings(resolved, ("hello",) * 64)
    assert [len(payload["input"]) for payload in payloads] == [16, 64, 64]
    assert all(payload["model"] == model for payload in payloads)


@pytest.mark.parametrize(
    "setting,accepted,rejected",
    [
        ({"max_input_tokens": 2}, ("hello world",), ("hello world extra",)),
        ({"max_request_tokens": 4}, ("hello world",) * 2, ("hello world", "hello world extra")),
        ({"max_request_items": 2}, ("hello",) * 2, ("hello",) * 3),
    ],
)
def test_strict_admission_accepts_limit_and_rejects_limit_plus_one(setting, accepted, rejected):
    from oam.models.embedding_inputs import EmbeddingInputLimitError

    admission = embedding_admission(binding(tokenizer="openai", **setting), GENERAL)
    assert admission.validate(accepted)
    with pytest.raises(EmbeddingInputLimitError):
        admission.validate(rejected)


def test_embedding_prepare_trims_unicode_prefix_only_and_warns_without_source_text(caplog):
    admission = embedding_admission(binding(tokenizer="openai", max_input_tokens=2), GENERAL)
    text = "hello world secret-tail😀"
    with caplog.at_level("WARNING"):
        actual = admission.prepare((text, "small"))
    assert actual == ("hello world", "small")
    assert text == "hello world secret-tail😀"
    assert "original_tokens=" in caplog.text
    assert "retained_tokens=2" in caplog.text
    assert "limit=2" in caplog.text
    assert "cl100k_base" in caplog.text
    assert "secret-tail" not in caplog.text
    assert len(caplog.records) == 1


def test_embedding_prepare_qwen_includes_eos_and_preserves_multibyte_prefix():
    admission = embedding_admission(binding(tokenizer="qwen", max_input_tokens=3), GENERAL)
    text = "Hello 世界😀 extra"
    actual = admission.prepare((text,))[0]
    assert text.startswith(actual)
    assert actual.strip()
    assert admission.counter.count(actual) <= 3
    assert actual.encode().decode() == actual


def test_embedding_prepare_trims_before_total_validation_but_never_trims_for_request_total():
    from oam.models.embedding_inputs import EmbeddingInputLimitError

    admission = embedding_admission(
        binding(tokenizer="openai", max_input_tokens=2, max_request_tokens=4), GENERAL
    )
    assert admission.prepare(("hello world extra", "hello world extra")) == (
        "hello world",
        "hello world",
    )
    with pytest.raises(EmbeddingInputLimitError, match="request token"):
        admission.prepare(("hello world",) * 3)


def test_embedding_prepare_rejects_when_special_token_overhead_cannot_fit_nonempty_text():
    from oam.models.embedding_inputs import EmbeddingInputLimitError

    admission = embedding_admission(binding(tokenizer="qwen", max_input_tokens=1), GENERAL)
    with pytest.raises(EmbeddingInputLimitError):
        admission.prepare(("hello",))


@pytest.mark.parametrize(
    "model,limit", [("text-embedding-3-small", 8192), ("Qwen/Qwen3-Embedding-0.6B", 32768)]
)
def test_runtime_and_both_preflight_paths_send_same_trimmed_payload(model, limit):
    import json

    import httpx

    from oam.models.bindings import resolve_model_binding
    from oam.models.client import OpenAICompatibleProbeClient
    from oam.models.runtime import RuntimeModels

    config = model_config(binding(model))
    env = {"UNSET_URL": "https://vector.test/v1"}
    original = ("hello " * (limit + 8) + "😀secret-tail", "small")
    payloads = []

    def respond(request):
        payload = json.loads(request.content)
        payloads.append(payload["input"])
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(2)]},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        runtime = RuntimeModels(config, env, http)
        resolved = resolve_model_binding(config.models, "embedding", env)
        assert resolved is not None
        runtime.embed(original, timeout_seconds=1)
        OpenAICompatibleProbeClient(http=http, config=config, environment=env).probe_embeddings(
            resolved, original
        )
        OpenAICompatibleProbeClient(http=http).probe_embeddings(resolved, original)
    assert payloads[0] == payloads[1] == payloads[2]
    assert original[0].startswith(payloads[0][0])
    assert runtime.embedding_admission.counter.count(payloads[0][0]) <= limit
    assert payloads[0][0] != original[0]
    assert payloads[0][1] == "small"
    assert original[0].endswith("😀secret-tail")


def test_runtime_aggregate_rejection_after_trimming_sends_no_partial_request():
    import httpx

    from oam.models.embedding_inputs import EmbeddingInputLimitError
    from oam.models.runtime import RuntimeModels

    config = model_config(binding(tokenizer="openai", max_input_tokens=2, max_request_tokens=4))
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        runtime = RuntimeModels(config, {"UNSET_URL": "https://vector.test/v1"}, http)
        with pytest.raises(EmbeddingInputLimitError, match="request token"):
            runtime.embed(("hello world extra",) * 3, timeout_seconds=1)
    assert sent == []


def test_null_per_input_bound_does_not_trim_or_warn(caplog):
    admission = embedding_admission(binding(tokenizer="openai", max_input_tokens=None), GENERAL)
    original = ("hello " * 9000,)
    with caplog.at_level("WARNING"):
        assert admission.prepare(original) == original
    assert not caplog.records


@pytest.mark.parametrize(
    "text", ["😀甲乙 tail", "é café 東京 مرحبا tail", "<think>😀 rest", "नमस्ते தமிழ் ไทย"]
)
@pytest.mark.parametrize("alias", ["openai", "qwen", None])
def test_trim_retains_valid_original_prefix_under_whole_string_counter(text, alias):
    admission = embedding_admission(binding(tokenizer=alias), GENERAL)
    prefix = admission.counter.truncate(text, 3)
    assert text.startswith(prefix)
    assert prefix.encode().decode() == prefix
    assert admission.counter.count(prefix) <= 3


def test_qwen_offset_hint_preserves_original_combining_sequence_when_it_fits():
    admission = embedding_admission(binding(tokenizer="qwen"), GENERAL)
    assert admission.counter.truncate("é café suffix", 2) == "é"


def test_rejected_aggregate_does_not_emit_trimming_warnings(caplog):
    from oam.models.embedding_inputs import EmbeddingInputLimitError

    admission = embedding_admission(
        binding(tokenizer="openai", max_input_tokens=2, max_request_items=1), GENERAL
    )
    with caplog.at_level("WARNING"), pytest.raises(EmbeddingInputLimitError):
        admission.prepare(("hello world extra",) * 2)
    assert not caplog.records


def test_cached_asset_counter_has_no_cross_call_truncation_state():
    from concurrent.futures import ThreadPoolExecutor

    counter = embedding_admission(binding(tokenizer="qwen"), GENERAL).counter
    text = "é café 東京 مرحبا"
    caps = [2, 3, 5, 8] * 8
    sequential = [counter.truncate(text, cap) for cap in caps]
    with ThreadPoolExecutor(max_workers=4) as executor:
        concurrent = list(executor.map(lambda cap: counter.truncate(text, cap), caps))
    assert concurrent == sequential
    assert counter.count(text) == 9
    assert all(text.startswith(prefix) for prefix in concurrent)
    assert all(counter.count(prefix) <= cap for prefix, cap in zip(concurrent, caps, strict=True))


@pytest.mark.parametrize("specials", [False, True])
def test_explicit_normalizing_asset_trims_original_offsets_without_saved_limits(tmp_path, specials):
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

    asset = Tokenizer(
        models.WordLevel(
            {"[UNK]": 0, "é": 1, "café": 2, "suffix": 3, "[EOS]": 4}, unk_token="[UNK]"
        )
    )
    asset.normalizer = normalizers.NFC()
    asset.pre_tokenizer = pre_tokenizers.Whitespace()
    asset.post_processor = processors.TemplateProcessing(
        single="$A [EOS]", special_tokens=[("[EOS]", 4)]
    )
    asset.enable_truncation(max_length=1)
    asset.enable_padding(length=6, pad_id=0, pad_token="[UNK]")
    path = tmp_path / "normalized.json"
    asset.save(str(path))
    admission = embedding_admission(
        binding(
            tokenizer=TokenizerConfig(
                type="tokenizer_json", file_env="TOK", add_special_tokens=specials
            )
        ),
        GENERAL,
        {"TOK": str(path)},
    )
    text = "é café suffix"
    assert admission.counter.count(text) == (4 if specials else 3)
    cap = 2 if specials else 1
    assert admission.counter.truncate(text, cap) == "é "
    assert admission.counter.count(text) == (4 if specials else 3)


@pytest.mark.parametrize("supplied_environment", [None, {"VECTOR_MODEL": "unknown"}])
@pytest.mark.parametrize(
    "tokenizer,expected", [("auto", "你"), (TokenizerConfig(type="o200k_base"), "你好")]
)
def test_configured_probe_uses_resolved_model_for_auto_without_overriding_explicit_counter(
    supplied_environment, tokenizer, expected
):
    import json

    import httpx

    from oam.models.bindings import resolve_model_binding
    from oam.models.client import OpenAICompatibleProbeClient
    from oam.models.runtime import RuntimeModels

    config = model_config(
        binding(None, model_env="VECTOR_MODEL", tokenizer=tokenizer, max_input_tokens=1)
    )
    actual_environment = {
        "VECTOR_MODEL": "text-embedding-3-small",
        "UNSET_URL": "https://vector.test/v1",
    }
    resolved = resolve_model_binding(config.models, "embedding", actual_environment)
    assert resolved is not None
    sent = []

    def respond(request):
        sent.append(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        RuntimeModels(config, actual_environment, http).embed(("你好",), timeout_seconds=1)
        OpenAICompatibleProbeClient(
            http=http, config=config, environment=supplied_environment
        ).probe_embeddings(resolved, ("你好",))
    assert sent == [[expected], [expected]]


def test_configured_probe_retains_explicit_asset_environment_with_resolved_model(tmp_path):
    import json

    import httpx
    from tokenizers import Tokenizer, models, pre_tokenizers

    from oam.models.bindings import resolve_model_binding
    from oam.models.client import OpenAICompatibleProbeClient

    asset = Tokenizer(models.WordLevel({"[UNK]": 0, "你好": 1}, unk_token="[UNK]"))
    asset.pre_tokenizer = pre_tokenizers.Whitespace()
    path = tmp_path / "probe-tokenizer.json"
    asset.save(str(path))
    config = model_config(
        binding(
            None,
            model_env="VECTOR_MODEL",
            tokenizer=TokenizerConfig(type="tokenizer_json", file_env="TOK"),
            max_input_tokens=1,
        )
    )
    resolved = resolve_model_binding(
        config.models,
        "embedding",
        {"VECTOR_MODEL": "text-embedding-3-small", "UNSET_URL": "https://vector.test/v1"},
    )
    assert resolved is not None
    sent = []

    def respond(request):
        sent.append(json.loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        OpenAICompatibleProbeClient(
            http=http, config=config, environment={"TOK": str(path)}
        ).probe_embeddings(resolved, ("你好",))
    assert sent == [["你好"]]
