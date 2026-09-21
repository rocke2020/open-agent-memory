import math

import pytest

from oam.indexing.embeddings import DeterministicFakeEmbeddingClient


def test_fake_embeddings_are_deterministic_finite_and_dimensionally_stable() -> None:
    client = DeterministicFakeEmbeddingClient(dimensions=6)

    first = client.embed(("sodium 140 mmol/L", "weekend hiking"))
    repeated = client.embed(("sodium 140 mmol/L", "weekend hiking"))

    assert first == repeated
    assert len(first) == 2
    assert {len(vector) for vector in first} == {6}
    assert all(math.isfinite(value) for vector in first for value in vector)
    assert first[0] != first[1]


def test_fake_embedding_client_rejects_nonpositive_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions must be positive"):
        DeterministicFakeEmbeddingClient(dimensions=0)
