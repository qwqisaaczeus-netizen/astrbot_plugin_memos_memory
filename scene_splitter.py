"""对话场景切分与 LLM 场景范围校验（4.6.0-test2）。

本模块只处理普通字典和数据类，不依赖 AstrBot，可在归档后、模型调用前独立复用。
边界统一指向新场景的第一轮（lead），避免把换日、间隔等提示误归到上一场景。
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RELATION_TURN_WORDS = (
    "但是", "不过", "可是", "然而", "其实", "后来", "从此", "从今", "重新",
    "和好", "分开", "告白", "答应", "拒绝", "原谅", "决定", "承诺", "约定",
    "真相", "坦白", "不再", "再也不", "关系", "我们算", "在一起",
)
ARC_END_WORDS = (
    "晚安", "结束了", "就这样吧", "明天见", "再见", "不早了", "那就好",
    "说定了", "一言为定", "说好了", "到此为止", "告一段落", "先这样",
)
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")
_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::?(\d{2}))?$")
HARD_BOUNDARY_REASONS = frozenset({"date_change", "time_gap"})


@dataclass
class Boundary:
    """一个 lead 边界；index 是新场景第一轮的下标。"""

    index: int
    side: str = "lead"
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0


@dataclass
class SceneCandidate:
    """闭区间场景候选，范围始终按消息原始下标表示。"""

    start_turn: int
    end_turn: int
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0


class SceneSplitter:
    """以本地、可解释启发式生成完整覆盖的场景候选。"""

    def __init__(self, gap_seconds: int = 10800, max_scenes: int = 8):
        self.gap_seconds = max(1, int(gap_seconds))
        self.max_scenes = max(1, int(max_scenes))

    @staticmethod
    def _timestamp(message: dict[str, Any]) -> float | None:
        value = message.get("event_ts")
        if value is None or isinstance(value, bool):
            return None
        try:
            ts = float(value)
        except (TypeError, ValueError):
            return None
        return ts if math.isfinite(ts) else None

    @staticmethod
    def _timezone(message: dict[str, Any]):
        raw = message.get("event_timezone")
        if raw in (None, ""):
            raw = message.get("timezone")
        if isinstance(raw, timezone):
            return raw
        name = str(raw or "UTC").strip()
        match = _OFFSET_RE.match(name)
        if match:
            minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
            if match.group(1) == "-":
                minutes = -minutes
            if abs(minutes) <= 24 * 60:
                return timezone(timedelta(minutes=minutes))
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc

    @classmethod
    def _local_date(cls, message: dict[str, Any]):
        ts = cls._timestamp(message)
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(ts, cls._timezone(message)).date()
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _content(message: dict[str, Any]) -> str:
        return " ".join(str(message.get("content") or "").split()).strip()

    @staticmethod
    def _lexical_features(text: str) -> set[str]:
        """中英文混合特征：英文词、中文单字及连续中文 bigram。"""
        features: set[str] = set()
        for part in _WORD_RE.findall(text.lower()):
            if not part:
                continue
            if re.fullmatch(r"[\u3400-\u9fff]+", part):
                features.update("c:" + char for char in part)
                features.update("b:" + part[i:i + 2] for i in range(len(part) - 1))
            else:
                features.add("w:" + part)
                if len(part) >= 4:
                    features.update("g:" + part[i:i + 2] for i in range(len(part) - 1))
        return features

    @classmethod
    def _topic_shift_score(cls, previous: str, current: str) -> float:
        left = cls._lexical_features(previous)
        right = cls._lexical_features(current)
        # 极短寒暄几乎总是低相似，不能据此切出大量伪场景。
        if len(left) < 4 or len(right) < 4:
            return 0.0
        similarity = len(left & right) / max(1, len(left | right))
        if similarity >= 0.14:
            return 0.0
        richness = min(1.0, min(len(left), len(right)) / 12.0)
        return round((0.14 - similarity) / 0.14 * (0.35 + 0.25 * richness), 4)

    def _boundaries(self, messages: list[dict[str, Any]]) -> list[Boundary]:
        boundaries: list[Boundary] = []
        for index in range(1, len(messages)):
            previous = messages[index - 1]
            current = messages[index]
            previous_text = self._content(previous)
            current_text = self._content(current)
            reasons: list[str] = []
            score = 0.0

            previous_date = self._local_date(previous)
            current_date = self._local_date(current)
            if previous_date is not None and current_date is not None and previous_date != current_date:
                reasons.append("date_change")
                score += 1.2

            previous_ts = self._timestamp(previous)
            current_ts = self._timestamp(current)
            if (previous_ts is not None and current_ts is not None
                    and current_ts - previous_ts >= self.gap_seconds):
                reasons.append("time_gap")
                ratio = (current_ts - previous_ts) / self.gap_seconds
                score += 1.0 + min(0.5, max(0.0, ratio - 1.0) * 0.15)

            topic_score = self._topic_shift_score(previous_text, current_text)
            if topic_score:
                reasons.append("topic_shift")
                score += topic_score

            if any(word in current_text for word in RELATION_TURN_WORDS):
                reasons.append("relation_turn")
                score += 0.7

            # 弧线收束发生在上一轮，但边界及理由始终挂到下一场的 lead。
            if any(word in previous_text for word in ARC_END_WORDS):
                reasons.append("arc_end")
                score += 0.65

            if reasons:
                boundaries.append(Boundary(index=index, reasons=reasons, score=round(score, 4)))
        return boundaries

    def detect(self, messages: list[dict]) -> list[SceneCandidate]:
        """检测场景；非空输入必定得到无重叠、无空洞的完整覆盖。"""
        if not messages:
            return []
        boundaries = self._boundaries(messages)
        if len(boundaries) > self.max_scenes - 1:
            # 删除最弱边界等价于合并其左右场景；稳定排序让同分时保留更早边界。
            strongest = sorted(boundaries, key=lambda item: (-item.score, item.index))[
                :self.max_scenes - 1
            ]
            boundaries = sorted(strongest, key=lambda item: item.index)

        starts = [0] + [item.index for item in boundaries]
        by_index = {item.index: item for item in boundaries}
        scenes: list[SceneCandidate] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] - 1 if position + 1 < len(starts) else len(messages) - 1
            boundary = by_index.get(start)
            reasons = list(boundary.reasons) if boundary else []
            score = float(boundary.score) if boundary else 0.0
            # A relation-defining first turn is still valuable diagnostic
            # evidence even when it does not create a second scene.
            if position == 0 and any(word in self._content(messages[0]) for word in RELATION_TURN_WORDS):
                reasons = list(dict.fromkeys(reasons + ["relation_turn"]))
                score = max(score, 0.7)
            scenes.append(SceneCandidate(
                start_turn=start,
                end_turn=end,
                reasons=reasons,
                score=score,
            ))
        return scenes

    def validate(
        self,
        episodes: list[dict],
        messages: list[dict],
        candidates: list[SceneCandidate] | None,
    ) -> dict:
        """就地规范模型场景范围，并返回覆盖、重叠及非法范围诊断。"""
        n = len(messages)
        scene_candidates = list(candidates) if candidates is not None else self.detect(messages)
        raw_usable = sorted(
            (item for item in scene_candidates
             if n and item.start_turn <= item.end_turn and item.end_turn >= 0 and item.start_turn < n),
            key=lambda item: (item.start_turn, item.end_turn),
        )
        # Lexical/topic candidates are intentionally recall-oriented. They help
        # the model notice possible transitions, but only recorded date changes
        # and long real-time gaps are hard constraints during range validation.
        hard_starts = {
            max(0, min(n - 1, int(item.start_turn))): list(item.reasons)
            for item in raw_usable[1:]
            if HARD_BOUNDARY_REASONS & set(item.reasons)
        }
        starts = [0] + sorted(start for start in hard_starts if start > 0)
        usable: list[SceneCandidate] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] - 1 if position + 1 < len(starts) else n - 1
            usable.append(SceneCandidate(
                start_turn=start,
                end_turn=end,
                reasons=hard_starts.get(start, []),
                score=1.0 if start in hard_starts else 0.0,
            ))
        fixed: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        valid_ranges: list[tuple[int, int, int]] = []

        def parse_index(value: Any) -> int | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError):
                return None

        def containing_start(value: int) -> int:
            for item in usable:
                if item.start_turn <= value <= item.end_turn:
                    return max(0, min(n - 1, int(item.start_turn)))
            starts = [max(0, min(n - 1, int(item.start_turn))) for item in usable]
            return max((start for start in starts if start <= value), default=0)

        def containing_end(value: int) -> int:
            for item in usable:
                if item.start_turn <= value <= item.end_turn:
                    return max(0, min(n - 1, int(item.end_turn)))
            ends = [max(0, min(n - 1, int(item.end_turn))) for item in usable]
            return min((end for end in ends if end >= value), default=n - 1)

        for episode_index, episode in enumerate(episodes):
            raw_start = episode.get("scene_start_turn")
            raw_end = episode.get("scene_end_turn")
            start = parse_index(raw_start)
            end = parse_index(raw_end)
            problems: list[str] = []
            if not n:
                problems.append("no_messages")
            if start is None:
                problems.append("invalid_start")
            if end is None:
                problems.append("invalid_end")
            if start is not None and end is not None and start > end:
                problems.append("reversed_range")
            if problems:
                invalid.append({"episode_index": episode_index, "reasons": problems,
                                "start": raw_start, "end": raw_end})

            if not n:
                episode["range_adjusted"] = False
                continue
            normalized_start = max(0, min(n - 1, start if start is not None else 0))
            normalized_end = max(0, min(n - 1, end if end is not None else n - 1))
            if normalized_start > normalized_end:
                normalized_start, normalized_end = normalized_end, normalized_start
            target = None
            if usable:
                # Each Episode must remain inside ONE candidate. Anchor by the
                # normalized start; if start is outside all candidates use the
                # nearest candidate, then clamp both ends into it.
                target = next((item for item in usable
                               if item.start_turn <= normalized_start <= item.end_turn), None)
                if target is None:
                    target = min(usable, key=lambda item: min(
                        abs(normalized_start - item.start_turn),
                        abs(normalized_start - item.end_turn),
                    ))
                normalized_start = max(target.start_turn, min(target.end_turn, normalized_start))
                normalized_end = max(normalized_start, min(target.end_turn, normalized_end))
            if normalized_start > normalized_end:
                normalized_end = normalized_start

            adjusted = not (
                start is not None and end is not None
                and start == normalized_start and end == normalized_end
            )
            episode["scene_start_turn"] = normalized_start
            episode["scene_end_turn"] = normalized_end
            episode["range_adjusted"] = adjusted
            if target is not None:
                owned_reasons = list(target.reasons)
                if adjusted:
                    owned_reasons.append("range_adjusted")
                episode["scene_boundary_reasons"] = list(dict.fromkeys(owned_reasons))
            if adjusted:
                fixed.append({
                    "episode_index": episode_index,
                    "from": [raw_start, raw_end],
                    "to": [normalized_start, normalized_end],
                })
            valid_ranges.append((episode_index, normalized_start, normalized_end))

        size = len(episodes)
        overlap_matrix = [[0 for _ in range(size)] for _ in range(size)]
        overlaps: list[dict[str, Any]] = []
        range_by_index = {index: (start, end) for index, start, end in valid_ranges}
        for index, (start, end) in range_by_index.items():
            overlap_matrix[index][index] = end - start + 1
        for left in range(size):
            if left not in range_by_index:
                continue
            left_start, left_end = range_by_index[left]
            for right in range(left + 1, size):
                if right not in range_by_index:
                    continue
                right_start, right_end = range_by_index[right]
                overlap = max(0, min(left_end, right_end) - max(left_start, right_start) + 1)
                overlap_matrix[left][right] = overlap_matrix[right][left] = overlap
                denominator = min(left_end - left_start + 1, right_end - right_start + 1)
                ratio = overlap / max(1, denominator)
                if ratio > 0.5:
                    overlaps.append({"episodes": [left, right], "turns": overlap,
                                     "ratio": round(ratio, 4), "large": True})

        covered: set[int] = set()
        for _, start, end in valid_ranges:
            covered.update(range(start, end + 1))
        uncovered = [index for index in range(n) if index not in covered]
        return {
            "fixed": fixed,
            "overlaps": overlaps,
            "uncovered": uncovered,
            "invalid": invalid,
            "overlap_matrix": overlap_matrix,
        }

    def dynamic_diary_count(
        self,
        base_count: int,
        messages: list[dict],
        candidates: list[SceneCandidate],
        max_cap: int,
        source_kind: str = "auto",
    ) -> int:
        """按跨日数和场景密度扩容；EOD 只让可靠边界突破软预算。"""
        cap = max(1, int(max_cap))
        base = max(1, int(base_count))
        dates = {date for message in messages if (date := self._local_date(message)) is not None}
        floor = max(base, len(dates) or 1)
        scene_count = max(1, len(candidates))
        if str(source_kind or "auto").lower() == "eod":
            # Local topic/relation heuristics are recall-oriented and may emit
            # several adjacent candidates inside one continuous narrative arc.
            # They are useful extraction hints, but must not each force a diary.
            boundary_candidates = list(candidates[1:])
            hard_boundaries = sum(
                1 for item in boundary_candidates
                if {"date_change", "time_gap"} & set(item.reasons)
            )
            semantic_break = any(
                item.score >= 0.95
                and ({"relation_turn", "topic_shift", "arc_end"} & set(item.reasons))
                for item in boundary_candidates
            )
            # About six user/assistant exchanges (roughly 12 messages) justify
            # one additional literary entry.
            density_capacity = max(1, math.ceil(len(messages) / 12))
            soft_capacity = max(base, density_capacity) + (1 if semantic_break else 0)
            hard_floor = max(len(dates) or 1, 1 + hard_boundaries)
            desired = max(floor, hard_floor, min(scene_count, soft_capacity))
        else:
            # Ordinary compression usually receives paired user/assistant
            # messages. Roughly eight exchanges justify one literary entry;
            # weak lexical boundaries remain extraction hints instead of
            # forcing 25-30 turns into six fragmented diaries.
            density_capacity = max(1, math.ceil(len(messages) / 16))
            boundary_candidates = list(candidates[1:])
            hard_boundaries = sum(
                1 for item in boundary_candidates
                if {"date_change", "time_gap"} & set(item.reasons)
            )
            hard_floor = max(len(dates) or 1, 1 + hard_boundaries)
            desired = max(
                floor,
                hard_floor,
                min(scene_count, max(base, density_capacity)),
            )
        return max(1, min(cap, desired))


def as_prompt_payload(candidates: list[SceneCandidate]) -> list[dict[str, Any]]:
    """转换成可直接 JSON 序列化并注入提示词的场景列表。"""
    return [asdict(candidate) for candidate in candidates]
