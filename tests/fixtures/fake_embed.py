"""Поддельные эмбеддинги для тестов поиска: мешок слов, хэшированный в 64 измерения.

Похожие тексты (общие слова) получают близкие векторы — этого хватает, чтобы проверить векторную часть
поиска и слияние со словарной без обращения к AI Studio.
"""

import hashlib
import math
import re

DIM = 64


def vector(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[\wЁё.]+", text.casefold()):
        h = int(hashlib.md5(w[:6].encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    v[0] += 0.01  # пустой текст — не нулевой вектор (косинус с нулём не определён)
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def fake_embed(texts, query=False):
    return [vector(t) for t in texts]
