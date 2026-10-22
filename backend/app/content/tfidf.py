"""无第三方依赖的字符 n-gram TF-IDF。

日文没有天然空格分词，字符 n-gram 比简单按空格切词更稳定。当前实现用于小规模
候选集的轻量特征；它不承担第三层 embedding/语义检索。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

_SPACE_RE = re.compile(r"\s+")


def _normalized(text: str) -> str:
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", text).lower())


def character_ngrams(text: str, *, min_n: int = 2, max_n: int = 4) -> Counter[str]:
    """返回字符 n-gram 词频；空白被忽略，结果确定可复现。"""
    if min_n < 1 or max_n < min_n:
        raise ValueError("invalid n-gram range")
    value = _normalized(text)
    terms: Counter[str] = Counter()
    for size in range(min_n, max_n + 1):
        terms.update(value[index : index + size] for index in range(len(value) - size + 1))
    return terms


def fit_transform(
    documents: list[str], *, min_n: int = 2, max_n: int = 4
) -> list[dict[str, float]]:
    """把一组文本转换为 L2 归一化的稀疏 TF-IDF 向量。"""
    if not documents:
        return []
    counts = [character_ngrams(doc, min_n=min_n, max_n=max_n) for doc in documents]
    document_frequency: Counter[str] = Counter()
    for terms in counts:
        document_frequency.update(terms.keys())
    total = len(documents)
    vectors: list[dict[str, float]] = []
    for terms in counts:
        weighted = {
            term: (1.0 + math.log(count))
            * (math.log((1.0 + total) / (1.0 + document_frequency[term])) + 1.0)
            for term, count in terms.items()
        }
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        vectors.append(
            {term: value / norm for term, value in weighted.items()} if norm else {}
        )
    return vectors


def cosine_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    """计算两个已归一化稀疏向量的余弦相似度。"""
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())
