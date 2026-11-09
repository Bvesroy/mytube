"""Category bucketing, ported from `buildCategories()` in site/index.html:1774-1825.

A weighted keyword lexicon scored over each channel's name, description and up to 30 recent
titles. Term weight is multiplied by inverse document frequency, a name hit counts 3×, a
title hit 2.2×, a description hit 1×, and the score is length-normalised so a channel with
a long description cannot win on volume alone. Terms present in more than 35% of the library
are dropped as signal-free; a channel under 0.33 falls back to `other`.

The lexicon lives in `app/resources/categories.json`, extracted from the page so there is
exactly one taxonomy in the project.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FALLBACK_SLUG = "other"
FALLBACK_NAME = "Uncategorised"
THRESHOLD = 0.33
MAX_DF_RATIO = 0.35
MIN_EFFECTIVE_WEIGHT = 0.6
RESOURCE = Path(__file__).resolve().parent.parent / "resources" / "categories.json"

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def token_set(text: str) -> set[str]:
    """Words plus adjacent bigrams, matching the page's `tokenSet`."""
    words = _TOKEN.findall(str(text).lower())
    tokens = set(words)
    for index in range(1, len(words)):
        tokens.add(f"{words[index - 1]} {words[index]}")
    return tokens


@dataclass(frozen=True)
class Category:
    slug: str
    name: str
    terms: dict[str, float]
    norm: float


@dataclass(frozen=True)
class ChannelText:
    yt_id: str
    name: str
    description: str = ""
    titles: Sequence[str] = ()


@lru_cache(maxsize=1)
def raw_categories() -> tuple[dict, ...]:
    return tuple(json.loads(RESOURCE.read_text(encoding="utf-8")))


def _term_universe() -> list[str]:
    terms: list[str] = []
    for category in raw_categories():
        for term in category["terms"]:
            if term not in terms:
                terms.append(term)
    return terms


def build(channels: Sequence[ChannelText]) -> list[Category]:
    """Compute idf-weighted categories for this specific library."""
    total = len(channels)
    if total == 0:
        return []
    title_sets = [token_set(" \n ".join(channel.titles[:30])) for channel in channels]
    name_sets = [token_set(channel.name) for channel in channels]
    desc_sets = [token_set(channel.description) for channel in channels]

    document_frequency = {term: 0 for term in _term_universe()}
    for index in range(total):
        for term in document_frequency:
            if term in title_sets[index] or term in name_sets[index] or term in desc_sets[index]:
                document_frequency[term] += 1

    built: list[Category] = []
    for category in raw_categories():
        effective: list[tuple[str, float]] = []
        squared = 0.0
        for term, weight in category["terms"].items():
            frequency = document_frequency.get(term, 0)
            if not frequency or frequency > total * MAX_DF_RATIO:
                continue
            value = weight * math.log((total + 1) / (frequency + 1))
            if value < MIN_EFFECTIVE_WEIGHT:
                continue
            effective.append((term, value))
            squared += value * value
        built.append(
            Category(
                slug=category["id"],
                name=category["name"],
                terms=dict(effective),
                norm=math.sqrt(squared) or 1.0,
            )
        )
    return built


def classify(
    channels: Sequence[ChannelText], categories: Sequence[Category] | None = None
) -> dict[str, str]:
    """Best category slug per channel, or `other` below the confidence threshold."""
    total = len(channels)
    cats = list(categories) if categories is not None else build(channels)
    out: dict[str, str] = {}
    for channel in channels:
        titles = token_set(" \n ".join(channel.titles[:30]))
        names = token_set(channel.name)
        descs = token_set(channel.description)
        length = (
            len(" \n ".join(channel.titles[:30])) / 6
            + len(channel.description) / 6
            + len(channel.name) * 3
        )
        best_slug, best_score = FALLBACK_SLUG, 0.0
        for category in cats:
            raw = 0.0
            for term, weight in category.terms.items():
                if term in titles:
                    raw += weight * 2.2
                elif term in names:
                    raw += weight * 3.0
                elif term in descs:
                    raw += weight
            if raw <= 0:
                continue
            value = (raw / category.norm) / math.pow(max(1.0, length) / 500.0, 0.25)
            if value > best_score:
                best_score, best_slug = value, category.slug
        out[channel.yt_id] = best_slug if best_score >= THRESHOLD else FALLBACK_SLUG
    return out


def all_slugs() -> list[str]:
    return [category["id"] for category in raw_categories()] + [FALLBACK_SLUG]


def names() -> dict[str, str]:
    mapping = {category["id"]: category["name"] for category in raw_categories()}
    mapping[FALLBACK_SLUG] = FALLBACK_NAME
    return mapping


def apply_overrides(
    auto: dict[str, str], overrides: Iterable[tuple[str, str]]
) -> dict[str, str]:
    """A manual move always beats the classifier, including back to `other`."""
    merged = dict(auto)
    known = set(all_slugs())
    for yt_id, slug in overrides:
        if slug in known:
            merged[yt_id] = slug
    return merged
