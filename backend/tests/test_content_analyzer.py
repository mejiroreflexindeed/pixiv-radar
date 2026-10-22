from app.content.analyzer import ANALYZER_VERSION, analyze_content
from app.content.tfidf import cosine_similarity, fit_transform


def test_analyzer_returns_explainable_bounded_features():
    text = (
        "トレーナーとウマ娘は朝の駅で会った。しかし、昨日とは空気が違った。\n\n"
        "「今日は一緒に走ろう」彼女は笑った。\n\n"
        "そして二人は遠回りを選んだ。その後、夕暮れまで話し続けた。"
    ) * 8
    result = analyze_content(text)
    assert result.analyzer_version == ANALYZER_VERSION
    assert result.language == "ja"
    assert result.char_count > 300
    assert result.paragraph_count >= 3
    assert 0 <= result.dialogue_ratio <= 1
    assert 0 <= result.structure_score <= 100
    assert result.keyword_hits["トレーナー"] == 8
    assert not result.is_clutter


def test_analyzer_flags_short_image_and_repetition_without_claiming_quality():
    text = "[pixivimage:1]\n\n[pixivimage:2]\n\n[pixivimage:3]\n\n宣伝 リンク\n\n宣伝 リンク"
    result = analyze_content(text)
    assert result.is_clutter
    assert result.is_suspect
    assert "text_too_short" in result.reason_codes
    assert "image_dominant" in result.reason_codes
    assert "promotion_markers" in result.reason_codes


def test_tfidf_is_deterministic_and_similar_texts_are_closer():
    vectors = fit_transform(["トレーナーと走る朝", "トレーナーと走った朝", "宇宙船の整備記録"])
    assert vectors == fit_transform(["トレーナーと走る朝", "トレーナーと走った朝", "宇宙船の整備記録"])
    assert cosine_similarity(vectors[0], vectors[1]) > cosine_similarity(vectors[0], vectors[2])
    assert cosine_similarity(vectors[0], vectors[0]) > 0.999


def test_tfidf_handles_empty_documents():
    assert fit_transform([]) == []
    assert fit_transform([""]) == [{}]
