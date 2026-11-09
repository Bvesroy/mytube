"""The ten-feature ranking model, ported verbatim from site/index.html.

The maths is deliberately identical to the browser engine so the Python port can be
checked against it (`tests/test_reco.py::test_score_matches_frontend_formula`).
Keep this module free of database and framework imports.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

FEATURES: tuple[str, ...] = (
    "lexical",
    "semantic",
    "channel",
    "neighbours",
    "session",
    "popular",
    "rating",
    "duration",
    "newChannel",
    "negative",
)
NF = len(FEATURES)
IDX = {name: i for i, name in enumerate(FEATURES)}

# index.html:877-879 — the hand-tuned prior.
DEFAULT_W: dict[str, float] = {
    "lexical": 1.00,
    "semantic": 1.30,
    "channel": 0.35,
    "neighbours": 0.55,
    "session": 0.90,
    "popular": 0.12,
    "rating": 0.30,
    "duration": 0.12,
    "newChannel": 0.30,
    "negative": -2.00,
}

RECO_DEFAULTS: dict[str, float] = {
    "affinity": 60.0,
    "halfLife": 90.0,
    "watched": 30.0,
    "serendipity": 30.0,
}

KNOB_RANGES: dict[str, tuple[float, float]] = {
    "affinity": (0.0, 100.0),
    "halfLife": (1.0, 365.0),
    "watched": (0.0, 100.0),
    "serendipity": (0.0, 100.0),
}

# Trained weights are shrunk toward the prior: one user is a very small training set.
SHRINK = 0.35


def weight_vector(source: Mapping[str, float] | Sequence[float] | None = None) -> list[float]:
    """Return weights in FEATURES order, filling gaps from the prior."""
    if source is None:
        return [DEFAULT_W[f] for f in FEATURES]
    if isinstance(source, Mapping):
        return [float(source.get(f, DEFAULT_W[f])) for f in FEATURES]
    values = [float(v) for v in source]
    if len(values) != NF:
        raise ValueError(f"expected {NF} weights, got {len(values)}")
    return values


def apply_knobs(
    weights: Sequence[float], *, affinity: float = 60.0, serendipity: float = 30.0
) -> list[float]:
    """index.html:883-891 — the UI knobs scale two features of the trained model."""
    out = list(weights)
    out[IDX["channel"]] *= affinity / 60.0
    out[IDX["newChannel"]] *= 0.4 + (serendipity / 100.0) * 1.2
    return out


def score(features: Sequence[float], weights: Sequence[float]) -> float:
    """relevance × prior boost + small additive adjustments (index.html:919-921)."""
    relevance = features[0] * weights[0] + features[1] * weights[1] + features[4] * weights[4]
    boost = (
        1.0
        + features[2] * weights[2]
        + features[3] * weights[3]
        + features[8] * weights[8]
    )
    additive = (
        features[5] * weights[5]
        + features[6] * weights[6]
        + features[7] * weights[7]
        + features[9] * weights[9]
    )
    return relevance * boost + additive


def score_grad(features: Sequence[float], weights: Sequence[float]) -> list[float]:
    """d(score)/d(w), matching index.html:926-932."""
    relevance = features[0] * weights[0] + features[1] * weights[1] + features[4] * weights[4]
    boost = (
        1.0
        + features[2] * weights[2]
        + features[3] * weights[3]
        + features[8] * weights[8]
    )
    grad = [0.0] * NF
    grad[0] = features[0] * boost
    grad[1] = features[1] * boost
    grad[4] = features[4] * boost
    grad[2] = relevance * features[2]
    grad[3] = relevance * features[3]
    grad[8] = relevance * features[8]
    grad[5] = features[5]
    grad[6] = features[6]
    grad[7] = features[7]
    grad[9] = features[9]
    return grad


def lexical_feature(raw: float) -> float:
    """min(1, log1p(bm25)/12) — keeps order between two strong matches (index.html:940)."""
    return min(1.0, math.log1p(raw) / 12.0) if raw > 0 else 0.0


def popularity_feature(views: int | None) -> float:
    if not views or views <= 0:
        return 0.0
    return min(1.0, math.log10(1 + views) / 8.0)


def rating_feature(rating: int | None) -> float:
    """index.html:474 — 1★ halves, 5★ boosts 1.5×, 3★ neutral."""
    if not rating:
        return 0.0
    return (1 + (rating - 3) * 0.25) - 1.0


def duration_feature(duration_s: int | None, preferred_s: float | None) -> float:
    if not duration_s or not preferred_s or preferred_s <= 0:
        return 0.5
    return max(0.0, 1.0 - abs(math.log(duration_s / preferred_s)) / 1.6)


def decay(half_life_days: float, age_days: float) -> float:
    return math.pow(0.5, max(0.0, age_days) / max(1.0, half_life_days))


def engagement_weight(seconds: float, completed: bool) -> float:
    """index.html:1567-1580 — end of video 1.4, otherwise s/120 clamped to 0.3..1.3."""
    if completed:
        return 1.4
    return min(1.3, max(0.3, (seconds or 0.0) / 120.0))


def blend_with_prior(learned: Iterable[float], *, shrink: float = SHRINK) -> list[float]:
    """learned·(1-shrink) + prior·shrink, with newChannel pinned to the prior.

    Pinning `newChannel` prevents exposure bias: if the learned model could lower the
    exploration term, the feed would stop discovering channels and the training data would
    stop containing new channels at all.
    """
    prior = weight_vector()
    out = []
    for index, value in enumerate(learned):
        blended = float(value) * (1 - shrink) + prior[index] * shrink
        if index == IDX["newChannel"]:
            blended = DEFAULT_W["newChannel"]
        out.append(blended)
    return out


def validate_knobs(knobs: Mapping[str, object]) -> dict[str, float]:
    """Reject out-of-range tuning values instead of silently clamping them."""
    clean: dict[str, float] = {}
    for key, (low, high) in KNOB_RANGES.items():
        value = knobs.get(key, RECO_DEFAULTS[key])
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{key} must be a number")
        number = float(value)
        if not low <= number <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}")
        clean[key] = number
    return clean



# FTS5 relevance is already capped through the lexical feature; this is a cheap stable hash
# of the same saved fields, used by the importer to avoid re-inserting a video whose on-disk
# representation is identical.


def _md_hash(page: int, count: int) -> int:
    """Deterministic, cheap integer fingerprint for a (title, duration, views, published, channel) blob.

    Not a cryptographic fingerprint — just enough to detect a no-op import attempt.
    """
    acc = page
    for b in str(count).encode("utf-8"):
        acc = ((acc * 31) + b) & 0xFFFFFFFF
    return acc

def metadata_hash(*fields: object) -> int:
    """Stable integer fingerprint for an immutable video+channel key tuple.

    Cheaper than the JSON-dump path used during early prototyping; callers should keep the
    contract: (title, duration_s, view_count, published_at, channel_id).
    """
    h = 0x9e3779b9
    for f in fields:
        if f is None:
            f = 0
        elif isinstance(f, str):
            f = int.from_bytes(f.encode("utf-8")[:8].ljust(8, b"\x00"), "little")
        elif not isinstance(f, int):
            f = int(f)
        h ^= f & 0xFFFFFFFF
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h






def bpr_train(
    pairs: Iterable[tuple[Sequence[float], Sequence[float]]],
    *,
    epochs: int = 200,
    learning_rate: float = 0.05,
    regularisation: float = 0.01,
    init: Sequence[float] | None = None,
) -> tuple[list[float], dict[str, float]]:
    """Pairwise BPR by gradient descent over the feature difference.

    Mirrors `trainRanker` in the page: for each (chosen, rejected) pair we push the margin
    `score(chosen) - score(rejected)` through a logistic link. Returns the raw learned
    vector plus training diagnostics; callers shrink it with `blend_with_prior`.
    """
    weights = list(init) if init is not None else weight_vector()
    pairs = list(pairs)
    if not pairs:
        return weights, {"pairs": 0, "log_loss": 0.0, "epochs": 0}
    loss = 0.0
    for _ in range(epochs):
        loss = 0.0
        for positive, negative in pairs:
            margin = score(positive, weights) - score(negative, weights)
            # log(1 + exp(-margin)) computed stably
            prob = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, margin))))
            loss += -math.log(max(prob, 1e-12))
            g_pos = score_grad(positive, weights)
            g_neg = score_grad(negative, weights)
            for index in range(NF):
                gradient = (1.0 - prob) * (g_pos[index] - g_neg[index])
                weights[index] += learning_rate * (
                    gradient - regularisation * weights[index]
                )
        loss /= len(pairs)
    # Never let training invert a feature's meaning entirely.
    for index, name in enumerate(FEATURES):
        if name == "negative":
            weights[index] = min(weights[index], 0.0)
        else:
            weights[index] = max(0.0, weights[index])
    return weights, {"pairs": len(pairs), "log_loss": loss, "epochs": epochs}
