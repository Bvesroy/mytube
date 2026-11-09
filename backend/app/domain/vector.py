"""float32 vector packing and cosine similarity.

Vectors are stored in SQLite as little-endian float32 blobs, the same layout the page uses
for its `Float32Array`, so a port can compare sizes (64 dims = 256 bytes).
"""

from __future__ import annotations

import array
import math
from collections.abc import Sequence

ENDIAN = "<"


def pack(values: Sequence[float]) -> bytes:
    packed = array.array("f", [float(v) for v in values])
    if ENDIAN == "<" and array.array("f").itemsize == 4:
        return packed.tobytes()
    return packed.tobytes()  # pragma: no cover - only reached on big-endian hosts


def unpack(blob: bytes) -> list[float]:
    packed = array.array("f")
    packed.frombytes(blob)
    return list(packed)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def norm(a: Sequence[float]) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Sequence[float]) -> list[float]:
    length = norm(a)
    if length <= 1e-9:
        return [0.0] * len(a)
    return [v / length for v in a]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    denominator = norm(a) * norm(b)
    if denominator <= 1e-9:
        return 0.0
    return max(-1.0, min(1.0, dot(a, b) / denominator))


def centroid(vectors: Sequence[Sequence[float]], *, weights: Sequence[float] | None = None):
    if not vectors:
        return []
    dim = len(vectors[0])
    total = [0.0] * dim
    weights = weights or [1.0] * len(vectors)
    for vector, weight in zip(vectors, weights):
        for index, value in enumerate(vector[:dim]):
            total[index] += value * weight
    return normalize(total)
