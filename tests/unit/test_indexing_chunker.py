import pytest

from oam.config import ChunkConfig
from oam.indexing.chunker import chunk_text


def limits():
    return ChunkConfig(
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
    assert chunks[0].previous_context == "前文"
    assert chunks[1].previous_context == "乙\n"
    assert chunks[1].text == "丙丁\n"


def test_oversized_unicode_paragraph_splits_without_losing_source_characters():
    chunks = chunk_text("甲乙丙丁戊己庚辛壬癸子丑", limits())
    assert [(item.start, item.end) for item in chunks] == [(0, 4), (4, 8), (8, 12)]
    assert "".join(item.text for item in chunks) == "甲乙丙丁戊己庚辛壬癸子丑"
    assert all(item.estimated_tokens <= 6 for item in chunks)


def test_chunker_keeps_short_paragraph_whole_and_rejects_owned_overlap():
    assert len(chunk_text("甲乙丙丁戊", limits())) == 1
    assert chunk_text(" \n ", limits()) == ()
    changed = limits().model_copy(update={"extractable_overlap_estimated_tokens": 1})
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("A source", changed)
