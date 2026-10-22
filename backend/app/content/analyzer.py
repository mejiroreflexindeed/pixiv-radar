"""可解释、确定性的正文轻量分析器。

这里只做第二层的便宜特征，不声称判断文学质量。规则结果会连同 reason code 保存，
方便以后用真实样本校准，而不是把当前阈值伪装成训练好的模型。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass

ANALYZER_VERSION = "content-v1"

_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_DIALOGUE_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"\n]*\"")
_IMAGE_RE = re.compile(
    r"\[(?:pixivimage|uploadedimage|image):[^\]]+\]|https?://\S+\.(?:png|jpe?g|gif|webp)(?:\?\S*)?",
    re.IGNORECASE,
)

# 这些词只作为解释性命中，不直接等同于质量。后续可从配置/用户反馈扩充。
_KEYWORDS = (
    "トレーナー",
    "ウマ娘",
    "友情",
    "恋人",
    "好き",
    "デート",
    "キス",
    "しかし",
    "けれど",
    "そして",
    "その後",
    "翌日",
    "一方",
    "結局",
)
_PROMOTION_WORDS = ("フォロー", "宣伝", "販売中", "購入", "リンク", "拡散希望")


@dataclass(frozen=True)
class ContentFeatures:
    analyzer_version: str
    language: str
    char_count: int
    paragraph_count: int
    dialogue_ratio: float
    image_marker_count: int
    structure_score: float
    is_clutter: bool
    is_suspect: bool
    keyword_hits: dict[str, int]
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def _language(text: str) -> str:
    kana = len(_KANA_RE.findall(text))
    cjk = len(_CJK_RE.findall(text))
    letters = sum(character.isalpha() for character in text)
    if kana:
        return "ja"
    if cjk:
        return "zh"
    if letters:
        return "other"
    return "unknown"


def _paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]


def _repeated_paragraph_ratio(paragraphs: list[str]) -> float:
    normalized = [re.sub(r"\s+", "", paragraph) for paragraph in paragraphs]
    useful = [paragraph for paragraph in normalized if len(paragraph) >= 8]
    if not useful:
        return 0.0
    counts = Counter(useful)
    repeats = sum(count - 1 for count in counts.values() if count > 1)
    return repeats / len(useful)


def analyze_content(text: str) -> ContentFeatures:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    normalized = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"\s+", "", normalized)
    char_count = len(compact)
    paragraphs = _paragraphs(normalized)
    dialogue_chars = sum(len(match.group(0)) for match in _DIALOGUE_RE.finditer(normalized))
    dialogue_ratio = min(1.0, dialogue_chars / max(char_count, 1))
    image_marker_count = len(_IMAGE_RE.findall(normalized))
    repeated_ratio = _repeated_paragraph_ratio(paragraphs)
    keyword_hits = {
        keyword: count
        for keyword in _KEYWORDS
        if (count := normalized.count(keyword)) > 0
    }
    promotion_hits = sum(normalized.lower().count(word.lower()) for word in _PROMOTION_WORDS)

    reasons: list[str] = []
    if char_count < 300:
        reasons.append("text_too_short")
    if image_marker_count >= 3 and char_count / image_marker_count < 200:
        reasons.append("image_dominant")
    if dialogue_ratio >= 0.85:
        reasons.append("dialogue_dominant")
    if repeated_ratio >= 0.35:
        reasons.append("repeated_paragraphs")
    if promotion_hits >= 2:
        reasons.append("promotion_markers")

    is_clutter = any(
        reason in {"text_too_short", "image_dominant", "dialogue_dominant"}
        for reason in reasons
    )
    is_suspect = any(
        reason in {"repeated_paragraphs", "promotion_markers"} for reason in reasons
    )

    # 0..100 的解释性结构分：长度、分段、适度对话、结构连接词、段落多样性。
    length_score = min(25.0, char_count / 120.0)
    paragraph_score = min(20.0, len(paragraphs) * 2.5)
    dialogue_score = max(0.0, 20.0 - abs(dialogue_ratio - 0.35) * 40.0)
    marker_score = min(20.0, sum(keyword_hits.values()) * 2.0)
    diversity_score = 15.0 * (1.0 - repeated_ratio)
    penalty = (20.0 if is_clutter else 0.0) + (15.0 if is_suspect else 0.0)
    structure_score = round(
        max(0.0, min(100.0, length_score + paragraph_score + dialogue_score + marker_score + diversity_score - penalty)),
        2,
    )

    return ContentFeatures(
        analyzer_version=ANALYZER_VERSION,
        language=_language(normalized),
        char_count=char_count,
        paragraph_count=len(paragraphs),
        dialogue_ratio=round(dialogue_ratio, 4),
        image_marker_count=image_marker_count,
        structure_score=structure_score,
        is_clutter=is_clutter,
        is_suspect=is_suspect,
        keyword_hits=keyword_hits,
        reason_codes=tuple(reasons),
    )
