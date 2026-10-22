"""阶段 C 状态机（纯函数）。

规则见 docs/PHASE_C_CONTRACT.md §1：cold_start/broad/track，滞回晋级/降级，
unknown 保持状态与 streak，不产生转换/事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .params import PhaseCParams


@dataclass(frozen=True)
class TargetStateInput:
    state: str  # cold_start / broad / track
    observation_count: int
    promotion_streak: int
    demotion_streak: int


@dataclass(frozen=True)
class TransitionDecision:
    from_state: str
    to_state: str
    reason_code: str
    evidence: dict


@dataclass(frozen=True)
class EventDecision:
    type: str
    severity: str
    payload: dict


@dataclass(frozen=True)
class StateDecision:
    new_state: str
    observation_count: int
    promotion_streak: int
    demotion_streak: int
    transitions: tuple[TransitionDecision, ...] = ()
    events: tuple[EventDecision, ...] = ()


def decide_state(
    current: TargetStateInput | None,
    evaluation_result: str,
    *,
    params: PhaseCParams,
    observation_increment: int = 1,
) -> StateDecision:
    """单次状态决策。

    observation_increment：本次评估是否计为一个观察（契约 §3.6：
    曝光达标且计数有效 → 1；insufficient_exposure/missing_data/
    计数倒退 → 0）。unknown 只增加观察数，不清零 streak、不产生转换。
    """
    state = current.state if current is not None else "cold_start"
    obs = current.observation_count if current is not None else 0
    p_streak = current.promotion_streak if current is not None else 0
    d_streak = current.demotion_streak if current is not None else 0

    if state not in ("cold_start", "broad", "track"):
        raise ValueError(f"invalid state: {state!r}")
    if evaluation_result not in ("qualified", "not_qualified", "unknown"):
        raise ValueError(f"invalid result: {evaluation_result!r}")

    obs += observation_increment

    # unknown：状态与 streak 全部保持
    if evaluation_result == "unknown":
        return StateDecision(state, obs, p_streak, d_streak)

    if evaluation_result == "qualified":
        p_streak += 1
        d_streak = 0
    else:
        p_streak = 0

    transitions: list[TransitionDecision] = []
    events: list[EventDecision] = []

    if state == "cold_start":
        if obs >= params.cold_start_min_observations:
            if p_streak >= params.promotion_streak:
                transitions.append(
                    TransitionDecision(
                        "cold_start", "track", "promotion_reached",
                        {"streak": p_streak, "observations": obs},
                    )
                )
                events.append(
                    EventDecision(
                        "target_entered_track", "info",
                        {"streak": p_streak, "observations": obs},
                    )
                )
                new_state = "track"
                p_streak = 0
                d_streak = 0
            else:
                transitions.append(
                    TransitionDecision(
                        "cold_start", "broad", "cold_start_completed",
                        {"observations": obs, "streak": p_streak},
                    )
                )
                events.append(
                    EventDecision(
                        "cold_start_completed", "info",
                        {"observations": obs, "streak": p_streak},
                    )
                )
                new_state = "broad"
                p_streak = 0
        else:
            new_state = "cold_start"
    elif state == "broad":
        if evaluation_result == "qualified" and p_streak >= params.promotion_streak:
            transitions.append(
                TransitionDecision(
                    "broad", "track", "promotion_reached",
                    {"streak": p_streak},
                )
            )
            events.append(
                EventDecision("target_entered_track", "info", {"streak": p_streak})
            )
            new_state = "track"
            p_streak = 0
            d_streak = 0
        else:
            new_state = "broad"
    else:  # track
        if evaluation_result == "not_qualified":
            d_streak += 1
            if d_streak >= params.demotion_streak:
                transitions.append(
                    TransitionDecision(
                        "track", "broad", "demotion_reached",
                        {"streak": d_streak},
                    )
                )
                events.append(
                    EventDecision("target_left_track", "info", {"streak": d_streak})
                )
                new_state = "broad"
                p_streak = 0
                d_streak = 0
            else:
                new_state = "track"
        else:
            new_state = "track"
            # track 已无需晋级 streak；只保留它会造成无界增长和误导 UI。
            p_streak = 0

    return StateDecision(
        new_state, obs, p_streak, d_streak,
        tuple(transitions), tuple(events),
    )
