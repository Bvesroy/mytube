"""Domain logic tests: weights/scoring parity primitives."""

from __future__ import annotations

from app.domain.weights import (
    DEFAULT_W,
    weight_vector,
    score,
    blend_with_prior,
    metadata_hash,
)


def test_default_weights_match_expected_keys():
    expected = {"lexical", "semantic", "channel", "neighbours", "session", "popular", "rating", "duration", "newChannel", "negative"}
    assert set(DEFAULT_W.keys()) == expected


def test_weight_vector_length():
    vec = weight_vector()
    assert len(vec) == len(DEFAULT_W)


def test_score_returns_float():
    features = [0.5, 0.3, 0.0, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0, 0.0]
    w = weight_vector()
    s = score(features, w)
    assert isinstance(s, (int, float))


def test_blend_with_prior_preserves_new_channel():
    from app.domain.weights import FEATURES
    # blend_with_prior takes a sequence of learned weight values (same order as FEATURES)
    learned = [0.5] * len(FEATURES)
    new_ch_idx = FEATURES.index("newChannel")
    learned[new_ch_idx] = 9.99  # should be pinned by blend
    blended = blend_with_prior(learned)
    assert blended[new_ch_idx] == weight_vector()[new_ch_idx]


def test_metadata_hash_stable():
    h1 = metadata_hash("title", 100, 50, 1700000000000, 1)
    h2 = metadata_hash("title", 100, 50, 1700000000000, 1)
    h3 = metadata_hash("other", 100, 50, 1700000000000, 1)
    assert h1 == h2
    assert h1 != h3
