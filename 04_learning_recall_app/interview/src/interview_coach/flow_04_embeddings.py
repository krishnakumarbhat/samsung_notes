from __future__ import annotations

import hashlib
import math
import re


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_+.:-]+", text.lower())


def embed_text(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0 for _ in range(dimensions)]
    for token in _tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], byteorder="big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + (digest[5] / 255.0)
        vector[index] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]
