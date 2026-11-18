"""Shared-call allocation conserves integer allowances with deterministic rounding."""

import pytest

from oam.indexing.budget import _batch_seed_weights, _batch_shares


@pytest.mark.parametrize(
    ("total", "weights", "expected"),
    [
        (2, {("c", 1): 1, ("b", 1): 1, ("a", 1): 1}, {("a", 1): 1, ("b", 1): 1, ("c", 1): 0}),
        (5, {("a", 1): 1, ("b", 1): 3}, {("a", 1): 1, ("b", 1): 4}),
        (1, {("a", 1): 1, ("b", 1): 3}, {("a", 1): 0, ("b", 1): 1}),
    ],
)
def test_remainders_go_to_largest_fraction_then_source_identity(total, weights, expected):
    assert _batch_shares(total, weights) == expected
    assert _batch_shares(total, dict(reversed(list(weights.items())))) == expected
    assert sum(_batch_shares(total, weights).values()) == total


def test_seed_weight_uses_public_utf8_bytes_and_preserves_colons_in_source_ids():
    inputs = {
        "private_facts": [{"id": "m", "source_refs": ["source:with:colon:2"]}],
        "envelope": {"seed_ids": ["m"], "facts": [{"id": "m", "text": "茶"}]},
    }
    # Compact {"id":"m","text":"茶"}: 20 ASCII bytes plus three UTF-8 bytes.
    assert _batch_seed_weights(inputs) == {("source:with:colon", 2): 23}
