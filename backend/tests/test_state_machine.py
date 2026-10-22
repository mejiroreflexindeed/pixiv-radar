"""状态机纯函数测试（WP4）：滞回、unknown 保持、转换与事件。"""

import pytest

from app.scoring.params import DEFAULT_PARAMS, PhaseCParams
from app.scoring.state_machine import TargetStateInput, decide_state

P = DEFAULT_PARAMS


def cold(obs=0, p_streak=0, d_streak=0):
    return TargetStateInput("cold_start", obs, p_streak, d_streak)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        decide_state(TargetStateInput("weird", 0, 0, 0), "qualified", params=P)
    with pytest.raises(ValueError):
        decide_state(None, "weird", params=P)


def test_unknown_keeps_state_and_streaks():
    # observation_increment=0 模拟"曝光不足/数据缺失"的 unknown：
    # 状态、观察数、streak 全部保持
    d = decide_state(
        TargetStateInput("track", 5, 2, 2), "unknown",
        params=P, observation_increment=0,
    )
    assert d.new_state == "track"
    assert d.observation_count == 5
    assert d.promotion_streak == 2
    assert d.demotion_streak == 2
    assert d.transitions == ()
    assert d.events == ()


def test_unknown_eligible_counts_observation():
    # 曝光达标的 unknown（如无有效增长对）仍计观察数，但不清零 streak
    d = decide_state(
        TargetStateInput("cold_start", 1, 1, 0), "unknown",
        params=P, observation_increment=1,
    )
    assert d.new_state == "cold_start"
    assert d.observation_count == 2
    assert d.promotion_streak == 1


def test_cold_start_needs_observations():
    # 观察只有 2（< 3）→ 留在 cold_start，即使 streak 达标也不晋级
    d = decide_state(cold(1, 1, 0), "qualified", params=P)
    assert d.new_state == "cold_start"
    assert d.observation_count == 2
    assert d.transitions == ()


def test_cold_start_promotion_streak_to_track():
    # 观察满 3 且连续 2 次 qualified → track
    d = decide_state(cold(2, 1, 0), "qualified", params=P)
    assert d.new_state == "track"
    assert d.transitions[0].from_state == "cold_start"
    assert d.transitions[0].to_state == "track"
    assert d.transitions[0].reason_code == "promotion_reached"
    assert d.events[0].type == "target_entered_track"
    assert d.promotion_streak == 0  # 晋级后清零


def test_cold_start_not_promoted_goes_broad():
    d = decide_state(cold(2, 0, 0), "qualified", params=P)
    assert d.new_state == "broad"
    assert d.transitions[0].reason_code == "cold_start_completed"
    assert d.events[0].type == "cold_start_completed"


def test_broad_qualified_streak_to_track():
    current = TargetStateInput("broad", 3, 1, 0)
    d = decide_state(current, "qualified", params=P)
    assert d.new_state == "track"
    assert d.events[0].type == "target_entered_track"


def test_broad_not_qualified_resets_promotion():
    current = TargetStateInput("broad", 3, 1, 0)
    d = decide_state(current, "not_qualified", params=P)
    assert d.new_state == "broad"
    assert d.promotion_streak == 0


def test_broad_never_demotes():
    current = TargetStateInput("broad", 5, 0, 0)
    d = decide_state(current, "not_qualified", params=P)
    assert d.new_state == "broad"
    assert d.transitions == ()


def test_track_qualified_clears_demotion_streak():
    current = TargetStateInput("track", 5, 7, 2)
    d = decide_state(current, "qualified", params=P)
    assert d.new_state == "track"
    assert d.demotion_streak == 0
    assert d.promotion_streak == 0
    assert d.transitions == ()


def test_track_demotion_after_three_not_qualified():
    current = TargetStateInput("track", 5, 0, 2)
    d = decide_state(current, "not_qualified", params=P)
    assert d.new_state == "broad"
    assert d.transitions[0].reason_code == "demotion_reached"
    assert d.events[0].type == "target_left_track"
    assert d.demotion_streak == 0


def test_track_demotion_hysteresis_not_early():
    current = TargetStateInput("track", 5, 0, 1)
    d = decide_state(current, "not_qualified", params=P)
    assert d.new_state == "track"
    assert d.demotion_streak == 2
    assert d.transitions == ()


def test_track_unknown_interrupts_no_streak_change():
    current = TargetStateInput("track", 5, 0, 2)
    d = decide_state(current, "unknown", params=P, observation_increment=0)
    assert d.new_state == "track"
    assert d.demotion_streak == 2
    # 下一次 not_qualified 才达到 3
    d2 = decide_state(
        TargetStateInput(d.new_state, d.observation_count, d.promotion_streak, d.demotion_streak),
        "not_qualified",
        params=P,
    )
    assert d2.new_state == "broad"


def test_custom_params_streaks():
    params = PhaseCParams(promotion_streak=3, demotion_streak=2)
    current = TargetStateInput("track", 5, 0, 1)
    d = decide_state(current, "not_qualified", params=params)
    assert d.new_state == "broad"
