"""Maximal Marginal Relevance re-ranking with a per-channel cap.

Ported from `diversify()` in site/index.html:1140-1155. The serendipity knob controls both
how hard MMR pushes for variety (`lambda = 1 - serendipity*0.55/100`) and how many videos a
single channel may contribute to the head of a feed (3 → 2 → 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.vector import cosine


@dataclass
class Candidate:
    video_id: str
    channel_id: str
    score: float
    vector: Sequence[float] | None = None
    payload: object | None = None


def channel_cap(serendipity: float) -> int:
    if serendipity > 66:
        return 1
    if serendipity > 33:
        return 2
    return 3


def mmr_lambda(serendipity: float) -> float:
    return 1.0 - (serendipity / 100.0) * 0.55


def rerank(
    candidates: Sequence[Candidate],
    *,
    limit: int,
    serendipity: float = 30.0,
    cap: int | None = None,
) -> list[Candidate]:
    """Greedy MMR: relevance minus redundancy against everything already chosen."""
    lam = mmr_lambda(serendipity)
    per_channel = channel_cap(serendipity) if cap is None else cap
    pool = list(candidates)
    chosen: list[Candidate] = []
    counts: dict[str, int] = {}

    while pool and len(chosen) < limit:
        best_index = -1
        best_value = float("-inf")
        for index, candidate in enumerate(pool):
            if counts.get(candidate.channel_id, 0) >= per_channel:
                continue
            redundancy = 0.0
            if candidate.vector is not None:
                for picked in chosen:
                    if picked.vector is None:
                        continue
                    redundancy = max(redundancy, cosine(candidate.vector, picked.vector))
            value = lam * candidate.score - (1.0 - lam) * redundancy
            if value > best_value:
                best_value = value
                best_index = index
        if best_index < 0:
            break
        pick = pool.pop(best_index)
        chosen.append(pick)
        counts[pick.channel_id] = counts.get(pick.channel_id, 0) + 1
    return chosen
