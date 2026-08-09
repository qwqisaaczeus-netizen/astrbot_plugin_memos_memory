"""DiaryPipeline (4.6.0 / tier coverage + first-person + anti-transcript fallback).

External entry (kept stable for main.py): `DiaryPipeline(plugin).coverage(content, episode)`
and `DiaryPipeline(plugin).transcript_risk(content, raw)` plus
`first_person_check`; `fallback_diary(episode)` renders the structured
top/middle/bottom fallback that only iterates must_write evidence.

test2 vs test1 advances:
- LCS via **rolling-hash + binary search on LCP** (O(n log n)) — no >1200 fallback
- Coverage is **tier-aware** (must_write strict ≥ threshold, supporting overall,
  archive_only never)
- First-person check adds "引用占/同质化句子率" light stats
- Fallback renders the **fact→emotion→afterglow** three-section structure
  rather than `JSON.join`
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

RELATION_WORDS = ("答应", "承诺", "约定", "再也不", "从今", "一定", "发誓", "喜欢", "爱", "走", "别", "不许", "记住")
ARC_END_WORDS = ("晚安", "结束了", "就这样吧", "明天见", "再见", "不早了", "那就好", "说定了", "一言为定", "说好了", "到此为止", "告一段落")
MACHINE_FIELDS = (
    "episode_key", "evidence", "must_write", "supporting", "archive_only",
    "turn_indexes", "scene_start", "scene_end", "turn_range", "tier",
    "情景模型", "证据等级", "系统指令", "记忆模型", "滚动状态文档", "根据对话",
)
PLATFORM_ROLE = re.compile(r"(用户|assistant|Assistant|user)\s*[:：]?\s*说")
FLOW_WORDS = ("他说", "我说", "然后", "接着", "她又说", "我又说", "他说完", "我说完")


@dataclass
class RiskReport:
    """Quantified "is the diary a verbatim replay" verdict."""
    longest_common_ratio: float
    quote_ratio: float
    flow_density: float
    compression_ratio: float
    risk: float

    @property
    def high(self) -> bool:
        return self.risk >= 0.42


@dataclass
class FirstPersonReport:
    passed: bool
    reason: str


class DiaryPipeline:
    """Owns diary quality checks (no LLM calls)."""

    def __init__(self, plugin):
        self.plugin = plugin

    # ---- shared helpers ----------------------------------------------

    @staticmethod
    def _evidence_overlap(detail: str, source: str) -> float:
        def terms(value: str) -> set[str]:
            clean = re.sub(r"\s+", "", str(value or ""))
            if len(clean) < 2:
                return {clean} if clean else set()
            return {clean[index:index + 2] for index in range(len(clean) - 1)}

        expected = terms(detail)
        actual = terms(source)
        return len(expected & actual) / max(1, len(expected))

    # ---- 3.2 tier-aware coverage --------------------------------------

    def coverage(self, content: str, episode: dict[str, Any]) -> tuple[float, list[str]]:
        evidence = episode.get("evidence") or []
        has_tier = any(isinstance(x, dict) and str(x.get("tier") or "") in
                       ("must_write", "supporting", "archive_only") for x in evidence)
        if has_tier:
            must = [x for x in evidence if isinstance(x, dict)
                    and str(x.get("tier") or "") == "must_write"
                    and str(x.get("detail") or x.get("quote") or "").strip()]
            support = [x for x in evidence if isinstance(x, dict)
                       and str(x.get("tier") or "") == "supporting"
                       and str(x.get("detail") or x.get("quote") or "").strip()]
        else:
            # legacy data without tiers → treat every grounded item as must
            must = [x for x in evidence if isinstance(x, dict)
                    and str(x.get("detail") or x.get("quote") or "").strip()]
            support = []
        if not must and not support:
            return 1.0, []
        covered = 0
        missing: list[str] = []
        for item in must:
            detail = str(item.get("detail") or "").strip()
            quote = str(item.get("quote") or "").strip()
            candidates = [v for v in (detail, quote) if v]
            overlap = max((self._evidence_overlap(v, content) for v in candidates), default=0.0)
            if overlap >= 0.16:
                covered += 1
            else:
                missing.append(detail or quote)
        base = covered / len(must) if must else 1.0
        # Supporting only nudges base *down* when supporting failed AND must was
        # incomplete; a fully-canonical must_render is itself enough, by design
        # (archive_only never participates).
        if support and has_tier and base < 1.0:
            sup_covered = sum(
                1 for item in support
                if max((self._evidence_overlap(v, content) for v in (
                    str(item.get("detail") or "").strip(),
                    str(item.get("quote") or "").strip()) if v), default=0.0) >= 0.16
            )
            sup_ratio = sup_covered / len(support)
            if sup_ratio < 0.5:
                base = min(base, 0.5 + sup_ratio * 0.5)
        return base, missing

    def support_coverage(self, content: str, episode: dict[str, Any]) -> float:
        support = [x for x in (episode.get("evidence") or [])
                   if isinstance(x, dict) and str(x.get("tier") or "") == "supporting"
                   and str(x.get("detail") or x.get("quote") or "").strip()]
        if not support:
            return 1.0
        covered = sum(
            1 for item in support
            if max((self._evidence_overlap(v, content) for v in (
                str(item.get("detail") or "").strip(),
                str(item.get("quote") or "").strip()) if v), default=0.0) >= 0.16
        )
        return covered / len(support)

    # ---- 5.3 rolling-hash LCS transcript risk ------------------------

    def transcript_risk(self, content: str, raw: str) -> float:
        return self._risk_report(content, raw).risk

    def risk_report(self, content: str, raw: str) -> RiskReport:
        return self._risk_report(content, raw)

    def _risk_report(self, content: str, raw: str) -> RiskReport:
        if not content or not raw:
            return RiskReport(0.0, 0.0, 0.0, 0.0, 0.0)
        c = " ".join(str(content).split())
        r = " ".join(str(raw).split())
        if not c or not r:
            return RiskReport(0.0, 0.0, 0.0, 0.0, 0.0)
        # 1) rolling-hash binary search for the longest common substring ratio.
        # O((n+m) log min(n,m)). Heuristic cap on m keeps it fast for huge raw.
        longest = _lcs_rolling_hash(c, r, cap_left=min(len(c), 2000), cap_right=min(len(r), 20000))
        overlap_ratio = longest / max(1, len(c))
        # 2) direct-quote ratio
        quotes = re.findall(r'[“"]([^”"]{2,40})[”"]', c)
        quote_chars = sum(len(q) for q in quotes)
        quote_ratio = quote_chars / max(1, len(c))
        # 3) flow-word density
        flow_count = sum(c.count(w) for w in FLOW_WORDS)
        flow_density = flow_count / max(1, len(c) / 40)
        # 4) diary/raw length ratio. test2 accidentally calculated the inverse,
        # which collapsed almost every normal compressed diary to 1.0.
        compression = len(c) / max(1, len(r))
        length_replay_risk = min(1.0, compression / 0.85) if len(c) > 200 else 0.0
        risk = max(overlap_ratio * 0.5,
                   quote_ratio * 0.45,
                   min(1.0, flow_density * 0.25),
                   length_replay_risk * 0.35)
        return RiskReport(overlap_ratio, quote_ratio, flow_density, round(compression, 4),
                          round(max(0.0, min(1.0, risk)), 3))

    # ---- 3.4 first-person contract check ------------------------------

    def first_person_check(self, content: str, episode: dict[str, Any] | None = None) -> FirstPersonReport:
        text = str(content or "")
        if not text.strip():
            return FirstPersonReport(False, "empty")
        for field in MACHINE_FIELDS:
            if field in text:
                return FirstPersonReport(False, "machine_field:" + field)
        if PLATFORM_ROLE.search(text):
            return FirstPersonReport(False, "platform_role_subject")
        if "我" not in text:
            return FirstPersonReport(False, "third_person_report")
        # light heuristics: too much verbatim quoting, too homogeneous sentences
        if len(text) > 200:
            quotes = re.findall(r'[“"]([^”"]{2,40})[”"]', text)
            quote_chars = sum(len(q) for q in quotes)
            if quote_chars / max(1, len(text)) > 0.55:
                return FirstPersonReport(False, "excessive_direct_quote")
            # very short sentences shorter than 10 chinese chars in a row indicate a chat-reply mash
            short_runs = sum(1 for s in re.split(r"[。！？；\n]", text) if 0 < len(s.strip()) < 10)
            if short_runs / max(1, len(text) // 20) > 0.7:
                return FirstPersonReport(False, "homogeneous_short_sentences")
        return FirstPersonReport(True, "")

    # ---- 5.4 structured fallback (must_write only, three sections) ----

    @staticmethod
    def fallback_diary(episode: dict[str, Any]) -> str:
        """Generate a top/middle/bottom first-person fallback.

        Top: date + scene. Middle: must_write evidence as one chained
        memory ('我记得，…；…。'). Bottom: emotional transition + afterglow +
        unresolved. Only iterates must_write when tiers exist, otherwise
        falls back to all grounded evidence (4.5.4 parity).
        """
        lines: list[str] = []
        occurred = str(episode.get("occurred_at") or episode.get("event_date") or "").strip()
        scene = str(episode.get("scene_anchor") or "").strip()
        if occurred and scene:
            lines.append(f"{occurred}，关于{scene}，我仍记得那一幕。")
        elif occurred:
            lines.append(f"{occurred}，我仍记得那一天。")
        elif scene:
            lines.append(f"关于{scene}，我仍记得那一幕。")
        evidence = episode.get("evidence") or []
        has_tier = any(isinstance(x, dict) and str(x.get("tier") or "") in
                       ("must_write", "supporting", "archive_only") for x in evidence)
        items = ([x for x in evidence if isinstance(x, dict)
                  and str(x.get("tier") or "") == "must_write"] if has_tier
                 else [x for x in evidence if isinstance(x, dict)])
        facts: list[str] = []
        for item in items[:16]:
            detail = " ".join(str(item.get("detail") or "").split()).strip()
            quote = " ".join(str(item.get("quote") or "").split()).strip()
            if detail:
                fact = detail
                if quote and quote not in detail:
                    fact += f"（「{quote}」）"
            else:
                fact = f"「{quote}」" if quote else ""
            if fact and fact not in facts:
                facts.append(fact)
        if facts:
            lines.append("我记得，" + "；".join(facts) + "。")
        before = str(episode.get("affect_before") or "").strip()
        after = str(episode.get("affect_after") or "").strip()
        if before and after:
            lines.append(f"我的感受从{before}，走到了{after}。")
        elif after:
            lines.append(f"那之后，我感到{after}。")
        state_change = str(episode.get("state_change") or "").strip()
        long_effect = str(episode.get("long_effect") or "").strip()
        if state_change:
            lines.append("我也记住了这份变化：" + state_change.rstrip("。") + "。")
        if long_effect:
            lines.append("它留给我的影响是：" + long_effect.rstrip("。") + "。")
        unresolved = [str(v).strip() for v in (episode.get("unresolved") or []) if str(v).strip()]
        if unresolved:
            lines.append("我仍放不下的是：" + "；".join(unresolved[:8]) + "。")
        result = "\n".join(v for v in lines if v.strip()).strip()
        return result if "我" in result else ("我记得，" + result)


def _lcs_rolling_hash(s: str, t: str, *, cap_left: int, cap_right: int) -> int:
    """Longest common substring length via binary search on length + rolling
    hash membership. Falls back gracefully to 0 when one side is empty.

    Capped widths keep the work bounded; for normal diary sizes (~hundreds to
    a few thousand chars) it completes in milliseconds.
    """
    if not s or not t:
        return 0
    s = s[:cap_left]
    t = t[:cap_right]
    base = 1_000_003
    mask = (1 << 64) - 1

    def prefix_hash(text: str) -> tuple[list[int], list[int]]:
        prefix = [0] * (len(text) + 1)
        powers = [1] * (len(text) + 1)
        for index, ch in enumerate(text, 1):
            prefix[index] = (prefix[index - 1] * base + ord(ch) + 1) & mask
            powers[index] = (powers[index - 1] * base) & mask
        return prefix, powers

    prefix_s, powers = prefix_hash(s)
    prefix_t, _ = prefix_hash(t)

    def window_hash(prefix: list[int], start: int, length: int) -> int:
        return (prefix[start + length] - prefix[start] * powers[length]) & mask

    def has_common(length: int) -> bool:
        if length <= 0 or length > len(s) or length > len(t):
            return False
        # Keep all starts for a hash so collision checks remain exact.
        seen: dict[int, list[int]] = {}
        for start in range(len(s) - length + 1):
            seen.setdefault(window_hash(prefix_s, start, length), []).append(start)
        for start_t in range(len(t) - length + 1):
            candidates = seen.get(window_hash(prefix_t, start_t, length), ())
            if not candidates:
                continue
            needle = t[start_t:start_t + length]
            if any(s[start_s:start_s + length] == needle for start_s in candidates):
                return True
        return False

    lo, hi, best = 1, min(len(s), len(t)), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if has_common(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best
