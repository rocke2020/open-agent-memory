from itertools import pairwise

import pytest

from oam.config import IngestionChunkConfig
from oam.indexing.chunker import chunk_text, resume_chunks, split_chunk_for_output_retry


def limits():
    return IngestionChunkConfig(
        target_estimated_tokens=4,
        max_estimated_tokens=6,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )


def test_resume_preserves_published_chunks_when_limits_change():
    text = "甲乙丙丁戊己庚辛壬癸子丑"
    chunks = resume_chunks(text, ["甲乙", "丙丁戊己"], limits())
    assert [(item.text, item.start, item.end) for item in chunks[:2]] == [
        ("甲乙", 0, 2),
        ("丙丁戊己", 2, 6),
    ]
    assert "".join(item.text for item in chunks) == text
    assert all(left.end == right.start for left, right in pairwise(chunks))


def test_resume_rejects_published_text_that_is_not_the_source_prefix():
    with pytest.raises(ValueError, match="published passage"):
        resume_chunks("甲乙丙丁", ["不存在"], limits())


def test_chunks_keep_exact_nonoverlapping_offsets_and_separate_previous_context():
    chunks = chunk_text("甲乙\n丙丁\n戊己", limits(), previous_context="前文")
    assert [(item.text, item.start, item.end) for item in chunks] == [
        ("甲乙\n", 0, 3),
        ("丙丁\n", 3, 6),
        ("戊己", 6, 8),
    ]
    assert chunks[0].previous_context == "文"
    assert chunks[1].previous_context == "乙\n"
    assert chunks[1].text == "丙丁\n"


def test_oversized_unicode_paragraph_splits_without_losing_source_characters():
    chunks = chunk_text("甲乙丙丁戊己庚辛壬癸子丑", limits())
    assert [(item.start, item.end) for item in chunks] == [(0, 4), (4, 8), (8, 12)]
    assert "".join(item.text for item in chunks) == "甲乙丙丁戊己庚辛壬癸子丑"
    assert all(item.estimated_tokens <= 6 for item in chunks)


def test_chunker_keeps_short_paragraph_whole_and_rejects_owned_overlap():
    assert len(chunk_text("甲乙丙", limits())) == 1
    assert "".join(item.text for item in chunk_text(" \n ", limits())) == " \n "
    assert chunk_text("", limits()) == ()
    changed = limits().model_copy(update={"extractable_overlap_estimated_tokens": 1})
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("A source", changed)


def test_cjk_and_emoji_chunks_preserve_source_and_reject_impossible_hard_max():
    chunks = chunk_text("甲乙😀丙丁", limits())
    assert "".join(item.text for item in chunks) == "甲乙😀丙丁"
    assert all(item.estimated_tokens <= limits().max_estimated_tokens for item in chunks)
    impossible = limits().model_copy(update={"max_estimated_tokens": 1})
    with pytest.raises(ValueError, match="hard maximum"):
        chunk_text("😀", impossible)


@pytest.mark.parametrize("suffix", ["", "b" * 16])
def test_blank_split_spans_keep_every_source_character_and_original_offset(suffix):
    text = "a" * 16 + "\n\n" + suffix
    chunks = chunk_text(text, limits())
    assert "".join(item.text for item in chunks) == text
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(left.end == right.start for left, right in pairwise(chunks))
    assert all(item.text == text[item.start : item.end] for item in chunks)
    assert all(item.estimated_tokens <= limits().max_estimated_tokens for item in chunks)


def test_long_blank_prefix_is_owned_without_exceeding_the_hard_max():
    text = "\n" * 24 + "Fact"
    chunks = chunk_text(text, limits())
    assert "".join(item.text for item in chunks) == text
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(left.end == right.start for left, right in pairwise(chunks))
    assert all(item.estimated_tokens <= limits().max_estimated_tokens for item in chunks)


def test_sentence_units_can_cross_the_soft_target_without_crossing_the_hard_max():
    config = limits().model_copy(update={"target_estimated_tokens": 5, "max_estimated_tokens": 9})
    text = "Alpha sentence. Beta sentence. Gamma sentence."
    chunks = chunk_text(text, config)
    assert [c.text for c in chunks] == ["Alpha sentence. Beta sentence.", " Gamma sentence."]
    assert "".join(c.text for c in chunks) == text
    assert (
        config.target_estimated_tokens < chunks[0].estimated_tokens <= config.max_estimated_tokens
    )


def test_single_wrapped_newline_does_not_break_a_sentence():
    config = limits().model_copy(update={"target_estimated_tokens": 4, "max_estimated_tokens": 10})
    first = "One wrapped\nsentence ends here."
    chunks = chunk_text(first + " Another sentence follows.", config)
    assert chunks[0].text == first
    assert "".join(c.text for c in chunks) == first + " Another sentence follows."


def test_blank_line_paragraphs_are_kept_whole_when_they_fit():
    config = limits().model_copy(update={"target_estimated_tokens": 4, "max_estimated_tokens": 12})
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    assert [c.text for c in chunk_text(text, config)] == [
        "First paragraph.\n\n",
        "Second paragraph.\n\n",
        "Third paragraph.",
    ]


def test_oversized_sentence_uses_complete_clauses_before_words():
    config = limits().model_copy(update={"target_estimated_tokens": 5, "max_estimated_tokens": 8})
    text = "Alpha beta gamma delta epsilon, zeta eta theta iota kappa."
    chunks = chunk_text(text, config)
    assert chunks[0].text == "Alpha beta gamma delta epsilon,"
    assert "".join(c.text for c in chunks) == text
    assert all(c.estimated_tokens <= 8 for c in chunks)


@pytest.mark.parametrize("text", ["甲乙。丙丁。戊己。", "مرحبا؟ أهلا؟ نهاية؟", "नमस्ते। फिर मिलेंगे।"])
def test_multilingual_sentence_splitting_is_lossless_and_bounded(text):
    chunks = chunk_text(text, limits())
    assert "".join(c.text for c in chunks) == text
    assert all(c.estimated_tokens <= 6 for c in chunks)
    assert all(c.text == text[c.start : c.end] for c in chunks)
    if text.startswith("甲"):
        assert [c.text for c in chunks] == ["甲乙。", "丙丁。", "戊己。"]


def test_retry_split_uses_multilingual_sentence_boundary_and_keeps_offsets():
    from oam.indexing.chunker import TextChunk

    text = "甲" * 200 + "。" + "乙" * 400 + "。"
    chunk = TextChunk(text, 50, 50 + len(text), "prior", 903)
    config = limits().model_copy(
        update={"target_estimated_tokens": 512, "max_estimated_tokens": 1024}
    )
    halves = split_chunk_for_output_retry(chunk, config)
    assert halves is not None
    assert [c.text for c in halves] == ["甲" * 200 + "。", "乙" * 400 + "。"]
    assert [(c.start, c.end) for c in halves] == [(50, 251), (251, 652)]
    assert halves[0].previous_context == "prior"
