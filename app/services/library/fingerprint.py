"""Text fingerprints for duplicate detection.

* ``text_fingerprint``: identical for documents whose text is the same once
  case, punctuation and spacing are ignored (for example a re-saved or
  compressed copy).
* ``simhash``: a 64-bit summary that stays close when most of the text is the
  same, used to propose "very similar" candidates.

These are hints for the user to review. Nothing is ever deleted because of them.
"""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"\w+", re.UNICODE)
MIN_CHARS = 60


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def text_fingerprint(text: str) -> str | None:
    tokens = words(text)
    normalised = " ".join(tokens)
    if len(normalised) < MIN_CHARS:
        return None
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def simhash(text: str) -> str | None:
    tokens = words(text)
    if len(" ".join(tokens)) < MIN_CHARS or len(tokens) < 8:
        return None
    counts = [0] * 64
    for index in range(len(tokens) - 2):
        shingle = " ".join(tokens[index:index + 3])
        value = int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            counts[bit] += 1 if (value >> bit) & 1 else -1
    result = 0
    for bit in range(64):
        if counts[bit] > 0:
            result |= 1 << bit
    return f"{result:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()
