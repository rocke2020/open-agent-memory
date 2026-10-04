import pytest

from oam.config import IngestionChunkConfig
from oam.indexing.chunker import chunk_text


def limits():
    return IngestionChunkConfig(
        target_estimated_tokens=4,
        max_estimated_tokens=6,
        extractable_overlap_estimated_tokens=0,
        previous_context_estimated_tokens=2,
    )


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
    assert [(item.start, item.end) for item in chunks] == [(0, 2), (2, 4), (4, 6), (6, 8), (8, 12)]
    assert "".join(item.text for item in chunks) == "甲乙丙丁戊己庚辛壬癸子丑"
    assert all(item.estimated_tokens <= 6 for item in chunks)


def test_chunker_keeps_short_paragraph_whole_and_rejects_owned_overlap():
    assert len(chunk_text("甲乙丙", limits())) == 1
    assert chunk_text(" \n ", limits()) == ()
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
def test_blank_split_spans_are_not_model_inputs_and_keep_original_offsets(suffix):
    text = "a" * 16 + "\n\n" + suffix
    chunks = chunk_text(text, limits())
    expected = [("a" * 16 + "\n", 0, 17)]
    if suffix:
        expected.append((suffix, 18, 34))
    assert [(item.text, item.start, item.end) for item in chunks] == expected
    assert all(item.text == text[item.start : item.end] for item in chunks)
    assert all(item.text.strip() for item in chunks)
    assert all(item.estimated_tokens <= limits().max_estimated_tokens for item in chunks)
    if suffix:
        assert chunks[1].previous_context == "aaaaaa\n\n"


def test_long_blank_prefix_keeps_original_start_of_nonblank_chunk():
    text = "\n" * 24 + "Fact"
    chunks = chunk_text(text, limits())
    assert [(item.text, item.start, item.end) for item in chunks] == [("\n" * 8 + "Fact", 16, 28)]
    assert chunks[0].previous_context == "\n" * 8
