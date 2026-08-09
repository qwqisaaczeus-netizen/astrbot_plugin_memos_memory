"""QueryPlanner (4.6.0 / unified temporal planning).

Decision-tree style query planning. Each intent owns its own `build_standalone`
so the planner stays readable and new intents slot in cleanly. Returns a single
`QueryPlan` dataclass exposing `search_text` (what retrievers embed) and a
`log` payload written to `[memory][query]` and the Console badge tooltip.

test2 vs test1 advances:
- **reason is a first-class field** (no_candidate/ambiguous/broad_sparse/
  specific_query → planned; +llm_disambiguated when LLM was used)
- **LLM disambiguation is gated extra**: only fires when
  `intent == specific` AND `facets` is effectively empty (the case where the
  local classifier genuinely has nothing to anchor on). Otherwise the LLM is
  skipped even at low confidence, avoiding spurious LLM cost on every short
  reference query.
- separates `search_text` (the string the retriever embeds) from `context_log`
  (the reason/context fields surfaced to logging + Console) — callers no longer
  need to read `plan["query"]` vs `plan["entities"]` ambiguously.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .retrieval_optimizer import parse_temporal_constraint

try:
    import jieba as _jieba
    import jieba.posseg as _pseg
except Exception:  # optional: conservative regex extraction remains available
    _jieba = None
    _pseg = None


AMBIGUOUS_MARKERS = (
    "这", "那", "他", "她", "它", "刚才", "之前", "然后", "为什么", "怎么了",
    "不用我说", "你记得", "你都记得", "你懂", "还记得", "算了", "还是", "又",
    "没可能", "没有可能", "就这样", "继续", "接着", "那件事", "那时候", "说不出口",
)
NARRATIVE_WORDS = (
    "为什么", "怎么", "后来", "之后", "以前", "过程", "发生了什么",
    "从头", "来龙去脉", "完整", "全部", "所有", "每次", "哪些",
)
BROAD_WORDS = ("都", "全部", "所有", "每次", "哪些", "几次", "从头")
TEMPORAL_MARKERS = (
    "今天", "明天", "昨天", "前天", "上周", "下周", "上个月", "下个月",
    "今年", "去年", "明年", "几号", "哪一天", "哪年", "几月", "什么时候",
    "纪念日", "生日", "除夕", "正月", "腊月",
)
RELATION_CUE_WORDS = ("答应", "承诺", "约定", "拒绝", "分手", "和好", "离开", "重逢")
EMOTION_CUE_WORDS = ("生气", "难过", "害怕", "高兴", "紧张", "安心", "委屈")
ENTITY_SUFFIXES = (
    "钥匙", "戒指", "项链", "盒子", "箱子", "房间", "学校", "医院", "车站",
    "天台", "寺庙", "破庙", "公园", "广场", "桥边", "河边", "海边",
)
ENTITY_FINALS = "庙台亭楼阁桥河湖海山城镇村巷街路院宅屋室门窗盒箱"
STOP_WORDS = {
    "什么", "怎么", "这个", "那个", "自己", "我们", "你们", "他们", "没有", "不是",
    "还是", "因为", "所以", "但是", "然后", "可是", "如果", "虽然", "已经", "一直",
    "起来", "下来", "过去", "现在", "今天", "明天", "昨天", "时候", "这样", "那样",
    "就是", "只是", "一夜", "真的", "觉得", "知道", "说话", "开口", "点头", "一样", "一起",
}
TAIL_VIRTUAL = ("了", "的", "在", "是", "还", "一", "刚", "里", "外", "上", "下", "前", "后", "就", "也", "都", "个", "过", "着")
NAME_SUFFIX = ("哥", "姐", "娘", "爷", "叔", "伯", "公子", "姑娘", "夫人", "先生", "将军", "王爷", "少爷", "小姐")


@dataclass
class QueryPlan:
    intent: str  # specific | contextual | narrative | temporal
    search_text: str
    use_context: bool
    context_used_reason: str  # no_candidate / ambiguous / broad_sparse / specific_query [+_llm_disambiguated]
    confidence: float
    resolved_entities: list[str] = field(default_factory=list)
    relation_cues: list[str] = field(default_factory=list)
    emotion_cues: list[str] = field(default_factory=list)
    temporal_constraints: list[str] = field(default_factory=list)
    context_turn_indexes: list[int] = field(default_factory=list)
    rewritten: bool = False  # True when LLM disambiguation was applied
    raw_standalone: str = ""


class QueryPlanner:
    """Decides whether the current message can stand alone or needs context,
    then constructs the retriever-side search string."""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    def _temporal_constraint(self, query: str):
        reference_now = None
        request_now = getattr(self.plugin, "_request_now", None)
        if callable(request_now):
            try:
                reference_now = request_now()
            except Exception:
                reference_now = None
        return parse_temporal_constraint(query, reference_now)

    # ---- classify intent ---------------------------------------------

    @staticmethod
    def _classify(query: str, has_temporal: bool, has_ref: bool) -> str:
        if has_temporal:
            return "temporal"
        if any(w in query for w in NARRATIVE_WORDS) or (
            has_ref and any(w in query for w in ("完整", "全部", "所有", "每次", "从头"))
        ):
            return "narrative"
        if has_ref:
            return "contextual"
        return "specific"

    # ---- entity extraction (windowed) --------------------------------

    def _extract_entities(self, text: str, window: int) -> list[str]:
        """Extract stable retrieval anchors, never arbitrary overlapping bigrams."""
        del window  # retained in the stable public signature
        normalized = " ".join(str(text or "").split())
        out: list[str] = []

        def add(value: str) -> None:
            value = str(value or "").strip(" ，。！？!?、:：；;（）()[]【】《》\"“”'")
            if not 2 <= len(value) <= 12 or value in STOP_WORDS:
                return
            if len(value) == 2 and value[-1] in TAIL_VIRTUAL:
                return
            if value not in out:
                out.append(value)

        for quoted in re.findall(r"[“\"《【]([^”\"》】]{2,12})[”\"》】]", normalized):
            add(quoted)
        character_name = str(getattr(self.plugin, "character_name", "") or "").strip()
        if character_name and character_name in normalized:
            add(character_name)

        if _pseg is not None:
            try:
                for token in _pseg.cut(normalized):
                    word = str(getattr(token, "word", "") or "").strip()
                    flag = str(getattr(token, "flag", "") or "")
                    if flag.startswith(("nr", "ns", "nt", "nz")) or flag in {
                        "n", "ng", "nl", "eng",
                    }:
                        add(word)
            except Exception:
                pass
        if _jieba is not None:
            try:
                for word in _jieba.lcut(normalized):
                    add(word)
            except Exception:
                pass
        for known in ENTITY_SUFFIXES:
            if known in normalized:
                add(known)
        # Unknown fictional places are often two-character compounds ending in
        # a concrete location/object noun. This constrained fallback recovers
        # them without returning every overlapping bigram in the sentence.
        compact = re.sub(r"[^一-鿿]", "", normalized)
        for index, ch in enumerate(compact):
            if ch in ENTITY_FINALS and index > 0:
                add(compact[index - 1:index + 1])
        else:
            # Regex fallback keeps explicit names/titles and ASCII identifiers;
            # it intentionally avoids manufacturing every Chinese bigram.
            for value in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,31}", normalized):
                add(value)
            for value in re.findall(
                r"[一-鿿]{1,6}(?:哥哥|姐姐|小姐|先生|将军|王爷|少爷|公子|老师|医生)",
                normalized,
            ):
                add(value)
        return out[:12]

    @staticmethod
    def _ctx_content(m: Any) -> str:
        c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
        return "" if not isinstance(c, str) else " ".join(c.split())

    def rewrite(self, user_query: str, contexts: list[Any] | None) -> QueryPlan | None:
        plugin = self.plugin
        if not getattr(plugin, "query_plan_enable", True):
            return None
        query = " ".join(str(user_query or "").split()).strip()
        if not query:
            return None
        window = int(getattr(plugin, "query_plan_context_window", 8) or 8)
        if isinstance(contexts, list):
            windowed = contexts[-window:]
        else:
            windowed = []
        constraint = self._temporal_constraint(query)
        temporal = list(constraint.markers)
        for marker in TEMPORAL_MARKERS:
            if marker in query and marker not in temporal:
                temporal.append(marker)
        has_ref = (len(query) <= 14) or any(marker in query for marker in AMBIGUOUS_MARKERS)
        if not has_ref and not temporal:
            return None  # 6.2: 当前消息主体明确 → 只用当前消息
        intent = self._classify(query, bool(temporal), has_ref)
        comp_texts: list[str] = []
        for m in reversed(windowed or []):
            c = self._ctx_content(m)
            if c and c not in comp_texts:
                comp_texts.append(c)
            if len(comp_texts) >= 3:
                break
        text = " ".join(reversed(comp_texts))
        resolved = self._extract_entities(text, window)[:12]
        cue_source = query + " " + text
        relation_cues = [w for w in RELATION_CUE_WORDS if w in cue_source]
        emotion_cues = [w for w in EMOTION_CUE_WORDS if w in cue_source]
        broad = any(w in query for w in BROAD_WORDS)
        sparse_subject = self._query_subject_sparse(query)
        # "no candidate" only when we did try context but extracted nothing
        if intent in ("contextual", "narrative") and not resolved and not comp_texts:
            return None
        context_text = "、".join(resolved)
        reason, use_context = self._decide_use_context(intent, bool(temporal),
                                                       context_text, broad, sparse_subject,
                                                       bool(comp_texts))
        standalone = self._build_standalone(
            intent, query, resolved, temporal, relation_cues, emotion_cues,
        )
        confidence = self._confidence(intent, temporal, resolved)
        return QueryPlan(
            intent=intent,
            search_text=standalone,
            use_context=use_context,
            context_used_reason=reason,
            confidence=confidence,
            resolved_entities=resolved,
            relation_cues=relation_cues,
            emotion_cues=emotion_cues,
            temporal_constraints=temporal[:4],
            context_turn_indexes=list(range(max(0, len(contexts or []) - window), len(contexts or [])))
            if isinstance(contexts, list) else [],
            raw_standalone=query,
            rewritten=False,
        )

    @staticmethod
    def _query_subject_sparse(query: str) -> bool:
        """Whether the current query itself lacks a concrete subject.

        Context-resolved entities must not make a broad query look specific;
        this decision is deliberately made before context is merged.
        """
        clean = re.sub(r"[，。！？!?、:：\s]", "", str(query or ""))
        generic = (
            "把所有相关的细节都完整地讲一遍吧", "所有相关细节", "相关的细节",
            "完整地讲一遍", "完整讲一遍", "都讲一遍", "发生了哪些事情",
            "发生了什么", "后来怎么样", "到底怎么回事", "从头到尾",
            "全部", "所有", "每次", "哪些", "几次", "细节", "相关", "完整",
            "讲一遍", "告诉我", "说一下", "都", "把", "吧",
        )
        for value in sorted(generic, key=len, reverse=True):
            clean = clean.replace(value, "")
        # Quoted/named subjects and meaningful remaining text make it specific.
        if re.search(r"[“\"《【][^”\"》】]{2,}[”\"》】]", query):
            return False
        clean = clean.strip("的地得了呢吗啊呀哦")
        return len(clean) < 2

    @staticmethod
    def _decide_use_context(intent: str, has_temporal: bool, context_text: str,
                            broad: bool, sparse_subject: bool, has_context: bool) -> tuple[str, bool]:
        # temporal → 不拼上文（独立日期约束）
        if intent == "temporal":
            return "temporal_query", False
        if not has_context:
            return "no_candidate", False
        # A broad, subject-sparse query is the most specific reason even when its
        # lexical form also falls into the narrative intent.
        if broad and sparse_subject:
            return "broad_sparse", True
        if intent == "contextual":
            return "ambiguous", True
        if intent == "narrative":
            return "narrative_entities" if context_text else "narrative_context", True
        return "specific_query", False

    @staticmethod
    def _build_standalone(intent: str, query: str, entities: list[str],
                          temporal: list[str], relation_cues: list[str] | None = None,
                          emotion_cues: list[str] | None = None) -> str:
        del temporal  # constraints are preserved separately on QueryPlan
        anchors: list[str] = []
        for value in [*entities, *(relation_cues or []), *(emotion_cues or [])]:
            if value and value not in anchors and value not in query:
                anchors.append(value)
        hint = "、".join(anchors[:12])
        if intent == "temporal":
            return query
        if hint:
            return query + "（上文提到的：" + hint + "）"
        return query

    @staticmethod
    def _confidence(intent: str, temporal: list[str], entities: list[str]) -> float:
        if intent == "temporal":
            return 0.8 if temporal else 0.6
        if entities:
            return 0.75
        if intent == "narrative":
            return 0.6
        if intent == "contextual":
            return 0.55
        return 0.9

    # ---- the older `_plan_episodic_query`-style entry kept for main ----

    def plan_for_search(self, user_query: str, context_text: str = "") -> dict[str, Any]:
        """Compatibility shim for the recall search backends that still expect
        the legacy dict shape with `use_context` + `search_text`. (We surface
        QueryPlan through this method so WebUI `[memory][query]` logging and the
        Console badge keep a single source of truth.)"""
        if not context_text:
            plan = QueryPlan(intent="specific", search_text=user_query,
                             use_context=False, context_used_reason="no_candidate",
                             confidence=0.9)
        else:
            # construct a thin QueryPlan without a full context window
            resolved = self._extract_entities(context_text, 1)[:12]
            constraint = self._temporal_constraint(user_query)
            has_temporal = constraint.active or any(m in user_query for m in TEMPORAL_MARKERS)
            has_ref = (len(user_query) <= 14) or any(m in user_query for m in AMBIGUOUS_MARKERS)
            intent = self._classify(user_query, has_temporal, has_ref)
            broad = any(w in user_query for w in BROAD_WORDS)
            sparse = self._query_subject_sparse(user_query)
            cue_source = user_query + " " + context_text
            relation_cues = [w for w in RELATION_CUE_WORDS if w in cue_source]
            emotion_cues = [w for w in EMOTION_CUE_WORDS if w in cue_source]
            reason, use_context = self._decide_use_context(
                intent, has_temporal, "、".join(resolved), broad, sparse, bool(context_text))
            search_text = self._build_standalone(
                intent, user_query, resolved, [], relation_cues, emotion_cues,
            )
            confidence = self._confidence(intent, list(constraint.markers), resolved)
            plan = QueryPlan(intent=intent, search_text=search_text,
                             use_context=use_context, context_used_reason=reason,
                             confidence=confidence, resolved_entities=resolved,
                             relation_cues=relation_cues, emotion_cues=emotion_cues,
                             temporal_constraints=list(constraint.markers))
        return {
            "intent": plan.intent,
            "search_text": plan.search_text,
            "use_context": plan.use_context,
            "context_used_reason": plan.context_used_reason,
            "target": 0,  # filled by main with episodic_default/narrative inject
            "candidate_pool": 0,
            "narrative": plan.intent == "narrative",
            "broad": False,
            "temporal": plan.intent == "temporal",
            "facets": {"entities": plan.resolved_entities, "temporal": plan.temporal_constraints,
                       "relation": plan.relation_cues},
        }

    # ---- 6.2 optional LLM disambiguation (narrowed condition) --------

    async def maybe_llm_disambiguate(
        self,
        plan: QueryPlan,
        context_text: str,
        llm_caller: Any,  # callable: prompt → str (async)
        prompt_builder: Any,  # callable: (query, context) → str
    ) -> QueryPlan:
        """Only calls the LLM when:
          (a) `query_plan_llm_disambiguate` is enabled, and
          (b) intent=specific AND resolved_entities is empty
              (i.e. local classifier genuinely has nothing to anchor on),
              AND confidence < threshold.
        If LLM is unavailable or returns garbage, the original plan is returned
        unchanged (the `rewritten=False` keeps the local classifier's verdict).
        """
        plugin = self.plugin
        if not getattr(plugin, "query_plan_llm_disambiguate", False):
            return plan
        threshold = float(getattr(plugin, "query_plan_llm_confidence_threshold", 0.5) or 0.5)
        if plan.confidence >= threshold:
            return plan
        # Narrowed gate: only fire when the local plan is genuinely anchor-less.
        if plan.intent != "specific" or plan.resolved_entities:
            return plan
        try:
            prompt = prompt_builder(plan.raw_standalone, context_text)
            response = await llm_caller(prompt)
        except Exception as exc:  # pragma: no cover - LLM failure path
            import logging
            logging.getLogger(__name__).debug("[memory][query] LLM disambiguate failed: %s", exc)
            return plan
        try:
            import json
            data = json.loads(response)
        except Exception:
            return plan
        standalone = str(data.get("standalone_query") or "").strip()
        if not standalone:
            return plan
        plan.raw_standalone = standalone
        plan.search_text = standalone
        plan.resolved_entities = [str(v).strip() for v in (data.get("resolved_entities") or [])
                                  if str(v).strip()][:12]
        plan.relation_cues = [str(v).strip() for v in (data.get("relation_cues") or [])
                              if str(v).strip()][:6]
        plan.emotion_cues = [str(v).strip() for v in (data.get("emotion_cues") or [])
                             if str(v).strip()][:6]
        plan.temporal_constraints = [str(v).strip() for v in (data.get("temporal_constraints") or [])
                                     if str(v).strip()][:4]
        plan.intent = str(data.get("intent") or plan.intent)
        plan.confidence = max(plan.confidence, min(1.0, float(data.get("confidence") or 0.0) or 0.0))
        plan.rewritten = True
        # mark the reason extension so logging shows the LLM was used
        if plan.context_used_reason == "specific_query":
            plan.context_used_reason = "specific_query_llm_disambiguated"
        else:
            plan.context_used_reason = plan.context_used_reason + "_llm_disambiguated"
        return plan
