"""
AstrBot Memos 长期记忆插件 v4.6.2。

时间模型严格区分事件发生时间、Memos 来源时间、本地索引时间和本轮当前时间。

架构:
  - on_llm_response 累积对话,每 N 轮触发压缩
  - 压缩 -> N 篇第一人称日记(角色名可配) -> 写 memos + 写 sqlite-vec
  - on_llm_request 用 query 检索 top-k 完整日记注入(缓存友好 extra_user_content_parts)
  - /memos-remember 手动钉一条高优先级记忆
  - /memos-reindex 换 emb 模型后全量重灌向量
  - 长期运行安全:换 emb 模型自动检测提示,None 守卫防 crash
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

from .compress import (
    build_compress_prompt,
    build_diary_literary_rewrite_prompt,
    build_diary_render_prompt,
    build_episode_extraction_prompt,
    build_query_plan_disambiguation_prompt,
    build_semantic_state_update_prompt,
    format_messages,
    format_messages_for_prompt,
    recorded_message_dates,
    recorded_time_context,
    recorded_time_labels_by_date,
)
from .archive_guard import (
    copy_db_files as _copy_db_files,
    migrate_episode_db_location,
    record_episode_db_location,
    resolve_plugin_data_db,
)
from .diary_pipeline import DiaryPipeline
from .data_backup import DataBackupManager
from .episodic_store import EpisodicStore
from .query_planner import QueryPlanner
from .retrieval_optimizer import (
    classify_memory_intent,
    intent_target,
    optimize_fused_hits,
    parse_temporal_constraint,
)
from .scene_splitter import SceneSplitter, as_prompt_payload
from .memos_client import MemosClient
from .temporal import (
    memo_source_times,
    normalize_memory_time,
    relative_memory_age,
    temporal_anniversary_query,
    timezone_or_default,
)
from .vector_store import VectorStore
from .xinchao import XinchaoController
from .time_insight_service import IntegratedTimeInsightService

try:
    from .repetition_guard import analyze_repetition, format_mirror_alert, format_repetition_guide
    from .time_enhancer import TimeEnhancer
except Exception:
    analyze_repetition = None
    format_mirror_alert = None
    format_repetition_guide = None
    TimeEnhancer = None

try:
    from .webui import WebUIServer
except Exception:  # aiohttp 缺失等极端情况不阻塞主插件
    WebUIServer = None

_PLUGIN_VERSION = "4.6.2"
_PASSAGE_VECTOR_STRATEGY = "local_passage_v2"
_DEFAULT_CHUNK_CHARS = 120
_DEFAULT_OVERLAP_CHARS = 30
_MIN_MSGS_TO_COMPRESS = 8
_LEGACY_IMP_TIER5 = "初遇,定情,告白,承诺,誓言,约定,求婚,结婚,婚礼,戒指,家人,我家的,永远,永远在一起,不会离开,不要离开,分别,离别,失去,重逢,和好,原谅,背叛,牺牲,死亡,濒死,复活,怀孕,孩子,出生,第一次见,第一次吻,第一次抱,身份揭露,真相揭露,命运改变"
_LEGACY_IMP_TIER4 = "信任,依赖,靠近,心软,吃醋,占有欲,害怕失去,害怕分离,安心,被接住,被记住,保护,救下,受伤,哭,崩溃,道歉,认错,秘密,真相,选择,决定,觉醒,变身,解锁,失控,封印,诅咒,契约,边界,禁忌,称呼,专属称呼"
_LEGACY_IMP_TIER3 = "喜欢,在意,担心,想念,陪伴,拥抱,牵手,亲吻,脸红,沉默,犹豫,试探,确认,撒娇,嘴硬,吃醋,害羞,承认,约会,礼物,纪念,习惯,下意识,靠近一点,不安,委屈,嫉妒,温柔,照顾,生病,疼,梦,房间,住处,名字,物件,信物"
_LEGACY_IMP_LOW = "吃饭,喝水,睡觉,起床,洗澡,洗漱,换衣服,散步,逛街,买东西,做饭,收拾,天气,下雨,下雪,很热,很冷,今天,明天,昨天,早上,中午,晚上,普通聊天,闲聊,开玩笑,路过,看风景,休息,发呆,随口"
_DEFAULT_IMP_TIER5 = _LEGACY_IMP_TIER5 + ",正式交往,告白成功,接受告白,终身承诺,约定终身,交换戒指,成为家人,接纳为家人,共同的家,生死相随,彻底决裂,永久分离,失去彼此,复合,真正原谅,严重背叛,收养,第一次见面,第一次接吻,第一次拥抱,身世揭露,记忆恢复,世界线改变,不可逆选择,核心秘密曝光,关系定义改变"
_DEFAULT_IMP_TIER4 = _LEGACY_IMP_TIER4 + ",建立信任,失去信任,情感依赖,坦白脆弱,暴露软肋,被理解,获得安全感,强烈吃醋,共同面对,重病,创伤,噩梦,绝望,大哭,弥补,重要秘密,关键真相,重大选择,重大决定,能力解锁,打破契约,明确边界,越过边界,底线,原则,改口称呼,关系确认,拒绝关系,公开关系,第一次争吵,严重冲突,长期误会,解开误会,离家,归来,搬到一起,共同生活,许下愿望,重要纪念日"
_DEFAULT_IMP_TIER3 = _LEGACY_IMP_TIER3 + ",承认心意,共同习惯,疼痛,梦见,安慰,鼓励,吃醋反应,冷落,赌气,吃惊,尴尬,期待,失落,害怕,愧疚,心疼,依恋,珍藏物,昵称,口头禅,喜好,讨厌,食物偏好,气味,歌曲,故事,学校,工作,故乡,生日,节日,共同计划,未完成约定,日常仪式,睡前习惯,起床习惯,照顾方式,相处方式,重要朋友,重要家人,宠物,身体状况,长期目标"
_DEFAULT_IMP_LOW = "吃饭,喝水,睡觉,起床,洗澡,洗漱,换衣服,散步,逛街,买东西,做饭,收拾,打扫,通勤,上班,下班,刷手机,看电视,天气,下雨,下雪,很热,很冷,今天,明天,昨天,早上,中午,晚上,普通聊天,闲聊,开玩笑,路过,看风景,休息,发呆,随口,寒暄,问好,重复确认,无关闲谈"


def _migrate_default_keywords(value: Any, legacy: str, expanded: str) -> str:
    configured = str(value or "").strip()
    return expanded if not configured or configured == legacy else configured

# 剧情时间排序器(v1.2):
# "六月廿六""正月十五""除夕""五月初三""子时""十五月圆" 这种中文农历/时辰表达,
# 没有标准解析,这里实现一个够用的启发式。无法解析的统一放最后。
_CN_DAY_NUM = {
    "初一": 1, "初二": 2, "初三": 3, "初四": 4, "初五": 5, "初六": 6, "初七": 7,
    "初八": 8, "初九": 9, "初十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16,
    "十七": 17, "十八": 18, "十九": 19, "二十": 20,
    "廿一": 21, "廿二": 22, "廿三": 23, "廿四": 24, "廿五": 25,
    "廿六": 26, "廿七": 27, "廿八": 28, "廿九": 29, "三十": 30,
}
_CN_MONTHS = {"正": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10, "冬": 11, "腊": 12}
_CN_SOLAR_TERMS = {"立春": 1, "雨水": 2, "惊蛰": 3, "春分": 4, "清明": 5, "谷雨": 6,
                   "立夏": 7, "小满": 8, "芒种": 9, "夏至": 10, "小暑": 11, "大暑": 12,
                   "立秋": 13, "处暑": 14, "白露": 15, "秋分": 16, "寒露": 17, "霜降": 18,
                   "立冬": 19, "小雪": 20, "大雪": 21, "冬至": 22, "小寒": 23, "大寒": 24}

# v1.2.6:部分日期格式用的时段 / 季节字典
_PERIOD_BONUS = {
    "子时": 1, "丑时": 2, "寅时": 3, "卯时": 4, "辰时": 5, "巳时": 6,
    "午时": 7, "未时": 8, "申时": 9, "酉时": 10, "戌时": 11, "亥时": 12,
    
    "正午": 8, "中午": 8, "上午": 7, "清晨": 5, "黎明": 4, "拂晓": 4,
    "午后": 9, "下午": 10, "傍晚": 13, "黄昏": 13, "晚上": 14, "夜里": 16, "夜深": 18,
    "深夜": 19, "子夜": 1, "凌晨": 2,
    "日出": 6, "日落": 15,
}
_SEASON_BONUS = {
    "春": 1, "初春": 1, "仲春": 2, "暮春": 3,
    "夏": 4, "初夏": 4, "仲夏": 5, "盛夏": 6, "暮夏": 7,
    "秋": 8, "初秋": 8, "仲秋": 9, "深秋": 10,
    "冬": 11, "初冬": 11, "隆冬": 12, "严冬": 12,
}



def parse_story_time(date_text: str) -> int:
    """把 date_text 解析成一个整数 score,用于 story_time 倒序注入。
    分数大段分离,保证完整日期 > 公历简写 > 部分日期 > 未注明:
      - 完整农历("六月廿六夜"):     1_000_000 + m*1000 + d + 时段偏移
      - 节气("清明"):               2_000_000 + 序号*50
      - 公历完整("2024-03-05"):     3_000_000 + (y*10000+m*100+d)
      - 公历简写("3月5日"):         100_000 + m*100 + d
      - 包含可识别时段/季节词:       1_000 + _PERIOD_BONUS or _SEASON_BONUS
      - 未注明/空:                   -1
    """
    if not date_text:
        return -1
    s = str(date_text).strip()
    if s in ("未注明", "未注明时间"):
        return -1

    def _period_bonus(text: str) -> int:
        for p, b in _PERIOD_BONUS.items():
            if p in text:
                return b
        return 0

    # 完整农历: "六月廿六" "正月初一" "腊月三十"
    m = None
    d = None
    for k, v in _CN_MONTHS.items():
        if k + "月" in s:
            m = v
            break
    if m is not None:
        for k, v in _CN_DAY_NUM.items():
            if k in s:
                d = v
                break
    if m is not None:
        return 1_000_000 + m * 1000 + (d or 15) + _period_bonus(s)

    # 节气
    for k in _CN_SOLAR_TERMS:
        if k in s:
            return 2_000_000 + _CN_SOLAR_TERMS[k] * 50

    # 公历完整: "2024-01-15" / "2024年1月15日"
    import re as _re
    m2 = _re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', s)
    if m2:
        y, mo, da = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        return 3_000_000 + (y * 10000 + mo * 100 + da)

    # 公历简写: "3月5日"(无年份)
    m3 = _re.search(r'(\d{1,2})月(\d{1,2})', s)
    if m3:
        return 100_000 + int(m3.group(1)) * 100 + int(m3.group(2))

    # 包含已知时段/季节词但非完整日期:给一个比未注明高的基础分
    for k in _PERIOD_BONUS:
        if k in s:
            return 1_000 + _period_bonus(s)
    for k in _SEASON_BONUS:
        if k in s:
            return 1_000 + _SEASON_BONUS[k]

    return -1

def _resolve_provider_name(prov) -> str:
    """从 provider 对象提取一个有意义的标识,用于日志和模型变更检测。

    三级 fallback 优先级:
      1. prov.get_model()  ← AstrBot 官方 API,返回内层模型名(如 text-embedding-3-small / Qwen3-VL-Embedding-8B)
      2. prov.provider_config.get("type")  ← 供应商类型(如 openai / gemini / ollama)
      3. prov.provider_id  ← 供应商 ID(最不具体,但总是有)
      4. "unknown"

    这样日志能看到真实模型名,换模型检测更准确。
    """
    if prov is None:
        return "unknown"
    # 1. 内层模型名(最具体) — AstrBot 标准 API
    if hasattr(prov, "get_model"):
        try:
            m = prov.get_model()
            if m and str(m).strip():
                return str(m).strip()
        except Exception:
            pass
    # 2. 供应商 type
    cfg = getattr(prov, "provider_config", None)
    if isinstance(cfg, dict):
        t = cfg.get("type")
        if t and str(t).strip():
            return str(t).strip()
    # 3. 裸 provider_id
    pid = getattr(prov, "provider_id", None)
    if pid and str(pid).strip():
        return str(pid).strip()
    return "unknown"


def _parse_memo_content(content: str) -> tuple[str, str, list[str]]:
    """解析 memos 记忆的原始文本。
    返回 (ts_text, body, tags)。
    约定:首行 = ts_text;以 # 开头的行 = 标签;其余 = 正文。
    """
    if not content:
        return ("未注明", "", [])
    lines = content.split("\n")
    first = lines[0].strip() if lines else ""
    looks_temporal = bool(re.search(
        r"(?:19|20)\d{2}[年./-]\d{1,2}|\d{1,2}月\d{1,2}日|"
        r"(?:正|冬|腊|[一二三四五六七八九十])月(?:初|十|廿|三十)|未注明时间|手动记录",
        first,
    ))
    ts_text = first if looks_temporal else "未注明时间"
    body_source = lines[1:] if looks_temporal else lines
    body_lines = [
        ln for ln in body_source
        if not ln.strip().startswith("#") and not ln.strip().startswith("<!-- memos-memory:")
        and not ln.strip().startswith(("长期影响:", "长期影响：", "触发线索:", "触发线索："))
    ]
    tags: list[str] = []
    for ln in lines[1:]:
        stripped = ln.strip()
        if stripped.startswith("#"):
            tags.extend([w for w in stripped.split() if w.startswith("#")])
    body = "\n".join(l.strip() for l in body_lines if l.strip())
    body, tags = _extract_inline_tags_from_content(body, tags)
    return (ts_text, body, tags)


def _time_meta_from_memo_text(
    content: str,
    ts_text: str,
    *,
    source_created_ts: float = 0.0,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Recover persisted event time or infer it from an old diary header."""
    meta = re.search(r"<!--\s*memos-memory:([^>]*)-->", content or "")
    meta_text = meta.group(1) if meta else ""
    occurred = ""
    basis = ""
    m = re.search(r"(?:^|;)occurred_at=([^;]+)", meta_text)
    if m:
        occurred = m.group(1).strip()
    m = re.search(r"(?:^|;)time_basis=([^;]+)", meta_text)
    if m:
        basis = m.group(1).strip()
    return normalize_memory_time(
        {"occurred_at": occurred, "date_text": ts_text, "time_basis": basis},
        source_created_ts=source_created_ts,
        timezone_name=timezone_name,
    )


def _importance_from_memo_text(content: str, tags: list[str]) -> tuple[int | None, int]:
    """Recover persisted importance/manual metadata from memo text."""
    manual = 1 if any(t in ("#手动", "#重要") for t in tags) else 0
    m = re.search(r"<!--\s*memos-memory:importance=(\d+);manual=(\d+)(?:;[^>]*)?\s*-->", content or "")
    if m:
        imp = max(1, min(5, int(m.group(1))))
        manual = 1 if m.group(2) == "1" else manual
        return imp, manual
    if manual:
        return 5, manual
    return None, manual


def _memory_meta_from_memo_text(content: str, body: str, tags: list[str]) -> tuple[str, str, str]:
    """Recover v1.9 memory metadata from hidden comments or visible legacy lines."""
    meta = re.search(r"<!--\s*memos-memory:([^>]*)-->", content or "")
    meta_text = meta.group(1) if meta else ""
    mt = ""
    m_type = re.search(r"(?:^|;)type=([a-z_]+)", meta_text)
    if m_type:
        mt = m_type.group(1)
    long_effect = ""
    trigger_hint = ""
    for line in (content or body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("长期影响:") or stripped.startswith("长期影响："):
            long_effect = stripped.split(":", 1)[-1] if ":" in stripped else stripped.split("：", 1)[-1]
        elif stripped.startswith("触发线索:") or stripped.startswith("触发线索："):
            trigger_hint = stripped.split(":", 1)[-1] if ":" in stripped else stripped.split("：", 1)[-1]
    return (
        _normalize_memory_type(mt, body, tags),
        _safe_meta_text(long_effect),
        _safe_meta_text(trigger_hint),
    )


def _machine_meta_from_memo_text(content: str) -> dict[str, Any]:
    """Recover portable machine-only fields embedded in the hidden Memos comment."""
    meta = re.search(r"<!--\s*memos-memory:([^>]*)-->", content or "")
    meta_text = meta.group(1) if meta else ""
    match = re.search(r"(?:^|;)meta64=([A-Za-z0-9_-]+)", meta_text)
    if not match:
        return {}
    try:
        raw = match.group(1)
        raw += "=" * ((4 - len(raw) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _machine_meta64(data: dict[str, Any]) -> str:
    clean = {
        "scene_anchor": _safe_meta_text(data.get("scene_anchor"), 160),
        "retrieval_key": _safe_meta_text(data.get("retrieval_key"), 260),
        "state_change": _safe_meta_text(data.get("state_change"), 260),
        "entities": [str(x).strip()[:80] for x in (data.get("entities") or []) if str(x).strip()][:20],
        "episode_key": _safe_meta_text(data.get("episode_key"), 40),
        "source_batch_id": _safe_meta_text(data.get("source_batch_id"), 80),
        "evidence_quality": _safe_meta_text(data.get("evidence_quality"), 40),
    }
    if not any(clean.values()):
        return ""
    raw = json.dumps(clean, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _strip_internal_metadata(text: str) -> str:
    """Remove plugin-private metadata comments before indexing/injecting text."""
    return re.sub(r"(?m)^\s*<!--\s*memos-memory:[\s\S]*?-->\s*$", "", text or "").strip()


def _normalize_importance(value: Any, fallback: int = 3) -> int:
    """Normalize LLM importance output like 5, '5', '5分' into 1..5."""
    try:
        if value is None:
            raise ValueError("empty")
        m = re.search(r"[1-5]", str(value))
        if not m:
            raise ValueError("no digit")
        return max(1, min(5, int(m.group(0))))
    except Exception:
        return max(1, min(5, int(fallback or 3)))


def _extract_inline_tags_from_content(content: str, existing_tags: list[Any] | None = None) -> tuple[str, list[str]]:
    """Move tags accidentally written inside diary content into the structured tags field."""
    text = str(content or "").strip()
    tags: list[str] = []
    seen: set[str] = set()

    def add_tag(raw: Any) -> None:
        tag = str(raw or "").strip().strip("，,。.!！?？；;、")
        if not tag:
            return
        if not tag.startswith("#"):
            tag = "#" + tag
        if len(tag) <= 1:
            return
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    for t in existing_tags or []:
        add_tag(t)

    # Most LLM mistakes append tags after the prose, sometimes directly after punctuation.
    # Keep a prefix group so normal words containing # are left alone while "。#tag" is fixed.
    tag_pattern = re.compile(r"(^|[\s，,。.!！?？；;、])(#[\w\u4e00-\u9fff][\w\u4e00-\u9fff_-]{0,30})")
    matches = list(tag_pattern.finditer(text))
    found = [m.group(2) for m in matches]
    for t in found:
        add_tag(t)
    if found:
        text = tag_pattern.sub(lambda m: m.group(1) if m.group(1).strip() else " ", text)
        text = re.sub(r"[ \t]+([，,。.!！?？；;、])", r"\1", text)
        text = re.sub(r"\s*\n\s*", " ", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        text = text.strip("，,；;、 \t")
    return text, tags


_MEMORY_TYPES = {
    "plot_fact",
    "relationship_shift",
    "emotional_anchor",
    "behavior_bias",
    "promise_or_rule",
    "daily_texture",
}
_PERSONA_TYPES = {"relationship_shift", "emotional_anchor", "behavior_bias", "promise_or_rule"}
_PLOT_TYPES = {"plot_fact", "promise_or_rule"}
_TEXTURE_TYPES = {"daily_texture"}


def _normalize_memory_type(value: Any, content: str = "", tags: list[str] | None = None) -> str:
    """Normalize or infer v1.9 memory type for old/new diary items."""
    mt = str(value or "").strip().lower()
    if mt in _MEMORY_TYPES:
        return mt
    joined = content + " " + " ".join(tags or [])
    if any(w in joined for w in ("承诺", "约定", "答应", "永远", "不会离开", "规则", "禁忌", "边界")):
        return "promise_or_rule"
    if any(w in joined for w in ("更信任", "更亲近", "依赖", "关系", "靠近", "心软", "吃醋")):
        return "relationship_shift"
    if any(w in joined for w in ("害怕", "安心", "委屈", "难过", "疼", "哭", "分离", "重逢")):
        return "emotional_anchor"
    if any(w in joined for w in ("以后", "会先", "更容易", "习惯", "下意识", "忍不住")):
        return "behavior_bias"
    if any(w in joined for w in ("吃饭", "睡", "散步", "天气", "日常", "洗", "喝")):
        return "daily_texture"
    return "plot_fact"


def _safe_meta_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                data = json.loads(text[i:j + 1])
                return data if isinstance(data, dict) else None
            except Exception:
                return None
    return None


class ManagedMemosSidecar:
    """Manage only the memos binary explicitly placed in the configured plugin directory."""

    def __init__(self, plugin: "MemosMemoryPlugin"):
        self.plugin = plugin
        self.process: subprocess.Popen | None = None
        self.started_by_plugin = False
        self.status = "disabled"
        self.message = ""
        self.port = 0
        self.base_url = ""
        self.log_path = ""
        self.state_path = ""
        self._log_file = None

    def _resolve(self, raw: str) -> Path:
        path = Path(os.path.expanduser(str(raw or "").strip()))
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    @staticmethod
    def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _find_free_port(self, host: str, start: int) -> int:
        start = max(1, min(65535, int(start or 5230)))
        for port in range(start, min(65535, start + 200)):
            if not self._port_open(host, port):
                return port
        raise RuntimeError(f"no free port found from {start}")

    def _read_state_port(self, state_path: Path, host: str, data_dir: Path) -> int:
        try:
            if not state_path.exists():
                return 0
            data = json.loads(state_path.read_text(encoding="utf-8"))
            port = int(data.get("port") or 0)
            if port <= 0 or port > 65535:
                return 0
            if str(data.get("host") or "") != str(host):
                return 0
            old_data_dir = str(data.get("data_dir") or "")
            if old_data_dir and old_data_dir != str(data_dir):
                return 0
            return port
        except Exception as e:
            logger.debug("[memos-memory][managed-memos] read state failed: %s", e)
            return 0

    def _write_state(self, state_path: Path, host: str, port: int, data_dir: Path, status: str) -> None:
        try:
            payload = {
                "host": host,
                "port": int(port),
                "base_url": f"http://{host}:{int(port)}",
                "data_dir": str(data_dir),
                "status": status,
                "updated_ts": time.time(),
            }
            state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug("[memos-memory][managed-memos] write state failed: %s", e)

    async def start(self) -> None:
        if self.plugin.memos_mode != "managed":
            self.status = "external"
            self.message = "external mode"
            return
        host = self.plugin.managed_memos_host or "127.0.0.1"
        managed_dir = self._resolve(self.plugin.managed_memos_dir)
        exe_path = self._resolve(self.plugin.managed_memos_exe)
        data_dir = self._resolve(self.plugin.managed_memos_data_dir)
        log_dir = managed_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = str(log_dir / "memos.log")
        state_path = managed_dir / "managed_state.json"
        self.state_path = str(state_path)
        if not exe_path.exists():
            self.status = "missing_exe"
            self.message = f"memos executable not found: {exe_path}"
            logger.warning("[memos-memory][managed-memos] %s", self.message)
            return
        desired_port = int(self.plugin.managed_memos_port or 0)
        if desired_port <= 0:
            last_port = self._read_state_port(state_path, host, data_dir)
            port = last_port or self._find_free_port(host, int(self.plugin.managed_memos_port_start or 5230))
        else:
            last_port = 0
            port = desired_port
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.plugin.memos_base_url = self.base_url.rstrip("/")
        if self.process is not None and self.process.poll() is None:
            self.status = "running"
            self.message = "already started by plugin"
            self._write_state(state_path, host, port, data_dir, self.status)
            return
        if self._port_open(host, port):
            if desired_port <= 0 and last_port == port:
                self.status = "adopted_existing"
                self.message = "last managed port is already open; reusing it"
                self._write_state(state_path, host, port, data_dir, self.status)
                logger.info("[memos-memory][managed-memos] reuse existing managed url=%s", self.base_url)
                return
            self.status = "port_busy_existing"
            self.message = "configured port is already open; plugin did not start or manage that process"
            logger.warning("[memos-memory][managed-memos] %s (%s)", self.message, self.base_url)
            return
        cmd = [str(exe_path), "--data", str(data_dir), "--port", str(port)]
        env = os.environ.copy()
        env["MEMOS_DATA"] = str(data_dir)
        env["MEMOS_PORT"] = str(port)
        try:
            self._log_file = open(self.log_path, "a", encoding="utf-8", buffering=1)
            self.process = subprocess.Popen(
                cmd,
                cwd=str(managed_dir),
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.started_by_plugin = True
            self.status = "starting"
            self.message = "process started"
            logger.info("[memos-memory][managed-memos] started %s data=%s url=%s", exe_path, data_dir, self.base_url)
            for _ in range(30):
                await asyncio.sleep(0.5)
                if self.process.poll() is not None:
                    self.status = "exited"
                    self.message = f"process exited with code {self.process.returncode}; see {self.log_path}"
                    logger.warning("[memos-memory][managed-memos] %s", self.message)
                    return
                if self._port_open(host, port):
                    self.status = "running"
                    self.message = "running"
                    self._write_state(state_path, host, port, data_dir, self.status)
                    return
            self.status = "starting"
            self.message = "process started but port is not open yet"
        except Exception as e:
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
            self.status = "start_failed"
            self.message = str(e)
            logger.warning("[memos-memory][managed-memos] start failed: %s", e)

    async def stop(self) -> None:
        try:
            if self.started_by_plugin and self.process is not None and self.process.poll() is None:
                self.process.terminate()
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    if self.process.poll() is not None:
                        self.status = "stopped"
                        self.message = "stopped by plugin"
                        break
                if self.process.poll() is None:
                    self.process.kill()
                    self.status = "killed"
                    self.message = "killed by plugin"
        except Exception as e:
            logger.debug("[memos-memory][managed-memos] stop failed: %s", e)
        finally:
            if self._log_file is not None:
                try:
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None

    def as_dict(self) -> dict[str, Any]:
        exe_path = self._resolve(self.plugin.managed_memos_exe)
        data_dir = self._resolve(self.plugin.managed_memos_data_dir)
        managed_dir = self._resolve(self.plugin.managed_memos_dir)
        running = self.process is not None and self.process.poll() is None
        return {
            "mode": self.plugin.memos_mode,
            "enabled": self.plugin.memos_mode == "managed",
            "status": self.status,
            "message": self.message,
            "host": self.plugin.managed_memos_host,
            "port": self.port or self.plugin.managed_memos_port,
            "base_url": self.base_url or self.plugin.memos_base_url,
            "managed_dir": str(managed_dir),
            "exe_path": str(exe_path),
            "exe_exists": exe_path.exists(),
            "data_dir": str(data_dir),
            "data_dir_exists": data_dir.exists(),
            "log_path": self.log_path,
            "state_path": self.state_path,
            "started_by_plugin": self.started_by_plugin,
            "process_running": running,
            "pid": self.process.pid if running else None,
        }


@register(
    "astrbot_plugin_memos_memory",
    "yjcmode",
    "memos-driven long-term memory: compress RP dialogue into first-person diaries",
    _PLUGIN_VERSION,
    "",
)
class MemosMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config
        self._PLUGIN_VERSION = _PLUGIN_VERSION

        # 角色名(可分享:别人用填自己的角色)
        self.character_name: str = str(config.get("character_name", "")).strip()

        # memos 连接
        self.memos_mode: str = str(config.get("memos_mode", "external")).strip().lower() or "external"
        if self.memos_mode not in {"external", "managed"}:
            self.memos_mode = "external"
        self.memos_base_url: str = str(config.get("memos_base_url", "http://127.0.0.1:5230")).rstrip("/")
        self.memos_token: str = str(config.get("memos_token", "")).strip()
        self.memos_timeout: float = float(config.get("memos_timeout", 30))
        self.managed_memos_dir: str = str(config.get(
            "managed_memos_dir",
            "./data/astrbot_plugin_memos_memory/managed_memos",
        )).strip()
        self.managed_memos_exe: str = str(config.get(
            "managed_memos_exe",
            "./data/astrbot_plugin_memos_memory/managed_memos/memos.exe",
        )).strip()
        self.managed_memos_data_dir: str = str(config.get(
            "managed_memos_data_dir",
            "./data/astrbot_plugin_memos_memory/managed_memos/data",
        )).strip()
        self.managed_memos_host: str = str(config.get("managed_memos_host", "127.0.0.1")).strip() or "127.0.0.1"
        self.managed_memos_port: int = int(config.get("managed_memos_port", 0))
        self.managed_memos_port_start: int = int(config.get("managed_memos_port_start", 5230))

        # 向量库
        self.vec_db_path: str = str(config.get("vec_db_path", "./data/astrbot_plugin_memos_memory/memories.db"))
        self.emb_provider_id: str = str(config.get("emb_provider_id", "")).strip()

        # 压缩用的 LLM provider (留空 = 用当前对话的 LLM)
        self.compress_provider_id: str = str(config.get("compress_provider_id", "")).strip()
        self.compress_llm_timeout: float = float(config.get("compress_llm_timeout", 120))

        # v4.0: evidence-first episodic memory. Memos remains the readable source
        # of active diary truth; this isolated database owns source turns and
        # rebuildable episode cards/evidence.
        default_episode_db = str(
            Path(self.vec_db_path).expanduser().parent / "episodic_memory.db"
        )
        self.episodic_memory_enable: bool = bool(config.get("episodic_memory_enable", True))
        configured_episode_db = str(config.get("episodic_db_path", default_episode_db)).strip()
        self.episodic_db_path: str = migrate_episode_db_location(configured_episode_db)
        self.evidence_first_generation_enable: bool = bool(config.get("evidence_first_generation_enable", True))
        self.raw_evidence_archive_enable: bool = bool(config.get("raw_evidence_archive_enable", True))
        # 4.6.0-test3: 原文永远完整落库，下面两个值只控制提示视图/分块。
        self.raw_archive_full_assistant_text: bool = bool(config.get("raw_archive_full_assistant_text", True))
        self.raw_archive_assistant_max_chars: int = max(0, int(config.get("raw_archive_assistant_max_chars", 0)))
        self.raw_archive_index_chunk_chars: int = max(200, min(4000, int(config.get("raw_archive_index_chunk_chars", 800))))
        self.raw_archive_prompt_view_max_chars: int = max(0, min(12000, int(config.get("raw_archive_prompt_view_max_chars", 1200))))
        self.scene_split_enable: bool = bool(config.get("scene_split_enable", True))
        self.scene_split_gap_seconds: float = max(60.0, min(172800.0, float(config.get("scene_split_gap_seconds", 10800))))
        self.diary_count_max_cap: int = max(1, min(12, int(config.get("diary_count_max_cap", 6))))
        self.evidence_tier_enable: bool = bool(config.get("evidence_tier_enable", True))
        self.diary_literary_mode: bool = bool(config.get("diary_literary_mode", True))
        self.diary_transcript_check_enable: bool = bool(config.get("diary_transcript_check_enable", True))
        self.diary_first_person_check_enable: bool = bool(config.get("diary_first_person_check_enable", True))
        self.diary_must_coverage_threshold: float = max(0.3, min(1.0, float(config.get("diary_must_coverage_threshold", 0.9))))
        self.diary_rewrite_preview_min_chars: int = max(800, min(12000, int(config.get("diary_rewrite_preview_min_chars", 2200))))
        self.db_snapshot_keep: int = max(1, min(20, int(config.get("db_snapshot_keep", 3))))
        self.data_backup_enable: bool = bool(config.get("data_backup_enable", True))
        self.data_backup_interval_days: int = max(
            1, min(365, int(config.get("data_backup_interval_days", 14)))
        )
        self.data_backup_keep: int = max(
            1, min(52, int(config.get("data_backup_keep", 6)))
        )
        default_backup_dir = str(
            Path(self.vec_db_path).expanduser().parent / "data_backups"
        )
        self.data_backup_dir: str = str(
            config.get("data_backup_dir", default_backup_dir)
        ).strip() or default_backup_dir
        self.query_plan_enable: bool = bool(config.get("query_plan_enable", True))
        self.query_plan_context_window: int = max(1, min(20, int(config.get("query_plan_context_window", 8))))
        self.query_plan_llm_disambiguate: bool = bool(config.get("query_plan_llm_disambiguate", False))
        self.query_plan_llm_provider_id: str = str(config.get("query_plan_llm_provider_id", "")).strip()
        self.query_plan_llm_confidence_threshold: float = max(0.1, min(0.95, float(config.get("query_plan_llm_confidence_threshold", 0.5))))
        self.episode_extraction_provider_id: str = str(config.get("episode_extraction_provider_id", "")).strip()
        self.episode_extraction_timeout: float = max(20.0, float(config.get("episode_extraction_timeout", 120)))
        self.diary_render_provider_id: str = str(config.get("diary_render_provider_id", "")).strip()
        self.diary_render_timeout: float = max(20.0, float(config.get("diary_render_timeout", 150)))
        self.episodic_auto_migrate: bool = bool(config.get("episodic_auto_migrate", True))
        # v4.5.0: a first turn arriving while the legacy episode bridge is still
        # running waits briefly (shielded) instead of silently falling back to
        # the 3.x composite chain with its 14-diary injection behavior.
        self.episode_migration_wait_seconds: float = max(
            0.0, min(30.0, float(config.get("episode_migration_wait_seconds", 6.0)))
        )
        self.episodic_candidate_pool: int = max(8, min(60, int(config.get("episodic_candidate_pool", 18))))
        self.episodic_default_inject: int = max(1, min(12, int(config.get("episodic_default_inject", 5))))
        self.episodic_narrative_inject: int = max(
            self.episodic_default_inject,
            min(14, int(config.get("episodic_narrative_inject", 8))),
        )
        self.episodic_evidence_per_memory: int = max(1, min(8, int(config.get("episodic_evidence_per_memory", 3))))
        self.episodic_full_diary_limit: int = max(0, min(6, int(config.get("episodic_full_diary_limit", 1))))
        self.episodic_min_card_score: float = max(0.1, min(0.9, float(config.get("episodic_min_card_score", 0.40))))
        # v4.1: a convergent current-state document replaces repeated persona
        # diary fragments. The old profile remains readable and is used only as
        # bootstrap/fallback until the first semantic state is ready.
        self.semantic_state_enable: bool = bool(config.get("semantic_state_enable", True))
        self.semantic_state_provider_id: str = str(config.get("semantic_state_provider_id", "")).strip()
        self.semantic_state_timeout: float = max(20.0, float(config.get("semantic_state_timeout", 120)))
        self.semantic_state_target_chars: int = max(600, min(6000, int(config.get("semantic_state_target_chars", 1800))))
        self.semantic_state_update_policy: str = str(
            config.get("semantic_state_update_policy", "adaptive")
        ).strip().lower() or "adaptive"
        if self.semantic_state_update_policy not in {"adaptive", "every_batch"}:
            self.semantic_state_update_policy = "adaptive"
        self.semantic_state_batch_threshold: int = max(
            1, min(12, int(config.get("semantic_state_batch_threshold", 3)))
        )
        self.semantic_state_max_wait_hours: float = max(
            1.0, min(720.0, float(config.get("semantic_state_max_wait_hours", 72)))
        )
        self.semantic_state_significance_threshold: float = max(
            0.35,
            min(0.95, float(config.get("semantic_state_significance_threshold", 0.72))),
        )
        self.semantic_state_merge_max_batches: int = max(
            self.semantic_state_batch_threshold,
            min(20, int(config.get("semantic_state_merge_max_batches", 6))),
        )
        self.semantic_state_auto_bootstrap: bool = bool(config.get("semantic_state_auto_bootstrap", True))
        self.semantic_state_bootstrap_episode_limit: int = max(
            20, min(500, int(config.get("semantic_state_bootstrap_episode_limit", 120)))
        )
        self.semantic_state_replace_profile: bool = bool(config.get("semantic_state_replace_profile", True))

        # v4.4: one query vector searches event cards, diary passages and raw
        # source turns. The state document remains a convergent injection layer.
        # Legacy multi-route/month/cluster code stays available only for rollback
        # and the ablation lab.
        self.lean_recall_enable: bool = bool(config.get("lean_recall_enable", True))
        self.lean_recall_candidate_k: int = max(20, min(120, int(config.get("lean_recall_candidate_k", 50))))
        self.lean_event_index_enable: bool = bool(config.get("lean_event_index_enable", True))
        self.lean_source_evidence_enable: bool = bool(config.get("lean_source_evidence_enable", True))
        self.lean_coverage_selection_enable: bool = bool(config.get("lean_coverage_selection_enable", True))
        self.lean_adaptive_evidence_enable: bool = bool(config.get("lean_adaptive_evidence_enable", True))
        self.passage_vector_auto_migrate: bool = bool(config.get("passage_vector_auto_migrate", True))
        self.source_turn_vector_auto_migrate: bool = bool(config.get("source_turn_vector_auto_migrate", True))
        self.lean_temporal_enable: bool = bool(config.get("lean_temporal_enable", True))
        self.recall_cross_layer_consistency_enable: bool = bool(
            config.get("recall_cross_layer_consistency_enable", True)
        )
        self.recall_temporal_constraints_enable: bool = bool(
            config.get("recall_temporal_constraints_enable", True)
        )
        self.recall_intent_layer_weights_enable: bool = bool(
            config.get("recall_intent_layer_weights_enable", True)
        )
        self.recall_observation_enable: bool = bool(
            config.get("recall_observation_enable", True)
        )
        self.recall_auto_eval_limit: int = max(
            20, min(200, int(config.get("recall_auto_eval_limit", 80)))
        )
        self.lean_story_min_inject: int = max(0, min(6, int(config.get("lean_story_min_inject", 1))))
        self.lean_story_max_inject: int = max(
            self.lean_story_min_inject,
            min(10, int(config.get("lean_story_max_inject", 6))),
        )
        # v4.5: normal (non-narrative) turns get their own target instead of a
        # hardcoded 2, and the relative-margin cut is configurable and looser by
        # default. Missing a relevant memory is worse than injecting one more.
        self.lean_story_normal_inject: int = max(
            self.lean_story_min_inject,
            min(self.lean_story_max_inject, int(config.get("lean_story_normal_inject", 3))),
        )
        self.lean_relative_margin: float = max(
            0.08, min(0.60, float(config.get("lean_relative_margin", 0.24)))
        )
        self.lean_relative_margin_broad: float = max(
            self.lean_relative_margin,
            min(0.80, float(config.get("lean_relative_margin_broad", 0.34))),
        )
        # v4.5: when the lean main path returns a weak or empty selection, a
        # one-shot wide rescue pass (legacy multi-route + month search) runs so
        # a thin ANN window can never silently drop a memory the old composite
        # chain would have found.
        self.recall_safety_net_enable: bool = bool(config.get("recall_safety_net_enable", True))
        self.recall_safety_net_min_selected: int = max(
            1, min(4, int(config.get("recall_safety_net_min_selected", 2)))
        )
        self.recall_safety_net_min_top_score: float = max(
            0.30, min(0.90, float(config.get("recall_safety_net_min_top_score", 0.60)))
        )
        # v4.5.1: injection volume is governed by a soft character target with graceful
        # per-item degradation (full diary -> passage -> compact excerpt), never
        # by silently dropping a qualified memory. The best memory remains full,
        # so the target may be exceeded. 0 disables this compaction pass.
        self.inject_char_budget: int = max(0, min(30000, int(config.get("inject_char_budget", 10000))))
        self.inject_compact_chars: int = max(80, min(600, int(config.get("inject_compact_chars", 200))))
        self.lean_texture_enable: bool = bool(config.get("lean_texture_enable", False))

        # 压缩触发
        self.compress_every_n_turns: int = int(config.get("compress_every_n_turns", 30))
        self.diary_count: int = int(config.get("diary_count", 2))
        self.compress_batch_max_messages: int = max(
            6,
            self.compress_every_n_turns * 2,
            int(config.get("compress_batch_max_messages", 60)),
        )
        self.eod_checkpoint_enable: bool = bool(config.get("eod_checkpoint_enable", True))
        self.eod_checkpoint_min_turns: int = max(
            1, min(8, int(config.get("eod_checkpoint_min_turns", 1)))
        )
        self.eod_checkpoint_max_diaries: int = max(
            1, min(12, int(config.get("eod_checkpoint_max_diaries", 6)))
        )

        # 检索
        self.recall_top_k: int = int(config.get("recall_top_k", 6))
        self.persona_top_k: int = int(config.get("persona_top_k", 3))
        self.plot_top_k: int = int(config.get("plot_top_k", 2))
        self.texture_top_k: int = int(config.get("texture_top_k", 1))
        self.enable_layered_injection: bool = bool(config.get("enable_layered_injection", True))
        self.enable_injection_style_rules: bool = bool(config.get("enable_injection_style_rules", True))
        self.enable_time_insight_affiliate: bool = bool(config.get("enable_time_insight_affiliate", True))
        self.time_insight_max_age_days: int = int(config.get("time_insight_max_age_days", 30))
        self.enable_affiliate_profile: bool = bool(config.get("enable_affiliate_profile", True))
        self.affiliate_profile_max_age_days: int = int(config.get("affiliate_profile_max_age_days", 45))
        self.profile_provider_id: str = str(config.get("profile_provider_id", "")).strip()
        self.profile_auto_update_days: int = int(config.get("profile_auto_update_days", 14))
        self.profile_recent_persona_limit: int = int(config.get("profile_recent_persona_limit", 120))
        self.profile_anchor_limit: int = int(config.get("profile_anchor_limit", 40))
        self.profile_manual_limit: int = int(config.get("profile_manual_limit", 30))
        self.profile_feedback_limit: int = int(config.get("profile_feedback_limit", 30))
        self.profile_llm_timeout: float = float(config.get("profile_llm_timeout", 90))
        self.profile_target_chars: int = int(config.get("profile_target_chars", 2600))
        self.profile_facts_target_count: int = int(config.get("profile_facts_target_count", 40))
        # v1.7 re-rank
        self.rerank_provider_id: str = str(config.get("rerank_provider_id", "")).strip()
        self.bm25_tokenizer: str = str(config.get("bm25_tokenizer", "jieba")).strip()
        # v1.8: configurable importance tier keywords
        self.imp_tier5_keywords = _migrate_default_keywords(
            config.get("imp_tier5_keywords"), _LEGACY_IMP_TIER5, _DEFAULT_IMP_TIER5
        )
        self.imp_tier4_keywords = _migrate_default_keywords(
            config.get("imp_tier4_keywords"), _LEGACY_IMP_TIER4, _DEFAULT_IMP_TIER4
        )
        self.imp_tier3_keywords = _migrate_default_keywords(
            config.get("imp_tier3_keywords"), _LEGACY_IMP_TIER3, _DEFAULT_IMP_TIER3
        )
        self.imp_low_keywords = _migrate_default_keywords(
            config.get("imp_low_keywords"), _LEGACY_IMP_LOW, _DEFAULT_IMP_LOW
        )
        # Keep Astr/WebUI views aligned with the runtime migration. This updates
        # only missing or exact legacy defaults; user-edited keyword lists stay intact.
        for key, value in (
            ("imp_tier5_keywords", self.imp_tier5_keywords),
            ("imp_tier4_keywords", self.imp_tier4_keywords),
            ("imp_tier3_keywords", self.imp_tier3_keywords),
            ("imp_low_keywords", self.imp_low_keywords),
        ):
            if str(config.get(key, "") or "").strip() != value:
                try:
                    config[key] = value
                except Exception:
                    pass
        self.min_similarity_to_inject: float = float(config.get("min_similarity_to_inject", 0.52))

        # 加权排序:相关度仍主导,并提高重要记忆和手动钉记忆的存在感。
        self.w_relevance: float = float(config.get("w_relevance", 0.72))
        self.w_importance: float = float(config.get("w_importance", 0.23))
        self.w_recency: float = float(config.get("w_recency", 0.03))
        self.pin_boost: float = float(config.get("pin_boost", 0.18))
        self.time_boost: float = float(config.get("time_boost", 0.07))

        # 注入行为(v1.2)
        # inject_order: relevance(默认,综合分序) | story_time(剧情时间倒序) | insert_time(入库时间倒序)
        self.inject_order: str = str(config.get("inject_order", "relevance")).strip() or "relevance"
        # inject_format: diary(纯原文) | summary(前 N 字摘要) | quote(引用体)
        self.inject_format: str = str(config.get("inject_format", "diary")).strip() or "diary"
        self.summary_chars: int = int(config.get("summary_chars", 200))
        # 注入防重复:最近 N 轮已注入过的记忆不再重复注入(0=关闭去重)
        self.recall_dedup_window: int = int(config.get("recall_dedup_window", 6))
        # v2.2.6: recall post-ranker and dynamic final injection count.
        self.recall_candidate_pool: int = int(config.get("recall_candidate_pool", 24))
        self.recall_rerank_enable: bool = bool(config.get("recall_rerank_enable", True))
        self.recall_injection_min_score: float = float(config.get("recall_injection_min_score", 0.62))
        self.recall_dynamic_count_enable: bool = bool(config.get("recall_dynamic_count_enable", True))
        self.recall_min_inject: int = int(config.get("recall_min_inject", 1))
        self.recall_max_inject: int = int(config.get("recall_max_inject", 9))
        self.recall_cluster_fold_enable: bool = bool(config.get("recall_cluster_fold_enable", True))
        self.recall_cluster_fold_apply: bool = bool(config.get("recall_cluster_fold_apply", False))
        self.recall_cluster_similarity: float = float(config.get("recall_cluster_similarity", 0.88))
        self.recall_cluster_base_per_group: int = int(config.get("recall_cluster_base_per_group", 2))
        self.recall_cluster_allow_protected: bool = bool(config.get("recall_cluster_allow_protected", True))
        self.recall_context_query_messages: int = max(0, int(config.get("recall_context_query_messages", 3)))
        self.recall_context_query_max_chars: int = max(0, int(config.get("recall_context_query_max_chars", 900)))
        self.recall_multi_query_enable: bool = bool(config.get("recall_multi_query_enable", True))
        self.recall_rrf_k: int = max(10, int(config.get("recall_rrf_k", 60)))
        self.recall_month_route_enable: bool = bool(config.get("recall_month_route_enable", True))
        self.recall_month_route_count: int = max(1, min(4, int(config.get("recall_month_route_count", 2))))
        self.recall_month_route_candidate_k: int = max(2, min(30, int(config.get("recall_month_route_candidate_k", 8))))
        self.recall_month_route_inject_max: int = max(0, min(6, int(config.get("recall_month_route_inject_max", 2))))
        self.recall_month_route_timeout: float = max(1.0, min(12.0, float(config.get("recall_month_route_timeout", 6))))
        self.recall_information_gain_enable: bool = bool(config.get("recall_information_gain_enable", True))
        self.recall_necessary_can_exceed_max: bool = bool(config.get("recall_necessary_can_exceed_max", True))
        self.recall_necessary_hard_cap: int = max(1, int(config.get("recall_necessary_hard_cap", 14)))
        self.passage_index_enable: bool = bool(config.get("passage_index_enable", True))
        self.passage_max_chars: int = max(120, int(config.get("passage_max_chars", 280)))
        self.passage_overlap_chars: int = max(0, int(config.get("passage_overlap_chars", 60)))
        self.mixed_injection_enable: bool = bool(config.get("mixed_injection_enable", True))
        self.full_diary_top_n: int = max(0, int(config.get("full_diary_top_n", 2)))
        self.passage_expand_chars: int = max(0, int(config.get("passage_expand_chars", 100)))

        # emb LRU 缓存(v1.2):同一 query/chunk 命中直接返回,省重复 embed
        self.emb_cache_size: int = int(config.get("emb_cache_size", 1000))

        # 网络重试(v1.2):memos / emb / 压缩 LLM 抖动时指数退避重试
        self.retry_max: int = int(config.get("retry_max", 3))
        self.retry_base_delay: float = float(config.get("retry_base_delay", 0.5))
        self.recall_embed_timeout: float = float(config.get("recall_embed_timeout", 12))
        self.recall_search_timeout: float = float(config.get("recall_search_timeout", 18))
        self.rerank_timeout: float = float(config.get("rerank_timeout", 8))

        # 后台对账(v1.3):每 N 秒拉一次 memos,跟 vec 对比,删掉 vec 里已被 memos 删除的
        self.reconcile_interval: int = int(config.get("reconcile_interval", 86400))
        self.enable_auto_reconcile: bool = bool(config.get("enable_auto_reconcile", True))

        # WebUI(v1.3):独立 aiohttp server,默认 8088
        self.webui_enable: bool = bool(config.get("webui_enable", True))
        self.webui_host: str = str(config.get("webui_host", "127.0.0.1")).strip() or "127.0.0.1"
        self.webui_port: int = int(config.get("webui_port", 8088))

        # 开关
        self.enable: bool = bool(config.get("enable", True))
        self.enable_auto_compress: bool = bool(config.get("enable_auto_compress", True))
        self.enable_auto_recall: bool = bool(config.get("enable_auto_recall", True))

        # v2.0: AstrBot context governance. Request trimming is non-destructive;
        # archive writes a shortened history back to AstrBot only when explicitly enabled/run.
        self.context_governance_enable: bool = bool(config.get("context_governance_enable", True))
        self.context_exclude_command_turns: bool = bool(config.get("context_exclude_command_turns", True))
        self.context_keep_recent_messages: int = int(config.get("context_keep_recent_messages", 40))
        self.context_min_messages_before_trim: int = int(config.get("context_min_messages_before_trim", 80))
        self.context_preserve_system_messages: bool = bool(config.get("context_preserve_system_messages", True))
        # v4.5.0: request-level trim is persisted back by AstrBot's save path,
        # so a pre-trim JSON backup is mandatory by default; a failed backup
        # skips the trim for that turn.
        self.context_trim_backup_enable: bool = bool(config.get("context_trim_backup_enable", True))
        self.context_archive_enable: bool = bool(config.get("context_archive_enable", False))
        self.context_archive_keep_recent_messages: int = int(config.get("context_archive_keep_recent_messages", 120))
        self.context_archive_min_total_messages: int = int(config.get("context_archive_min_total_messages", 240))
        self.context_archive_interval_days: int = int(config.get("context_archive_interval_days", 7))
        self.context_archive_backup_dir: str = str(config.get(
            "context_archive_backup_dir",
            "./data/astrbot_plugin_memos_memory/context_backups",
        )).strip()

        # v2.0: built-in RP enhancer, integrated from astrbot_plugin_rp_enhancer.
        self.rp_enhancer_enable: bool = bool(config.get("rp_enhancer_enable", True))
        self.rp_time_enable: bool = bool(config.get("rp_time_enable", True))
        self.rp_repetition_enable: bool = bool(config.get("rp_repetition_enable", True))
        self.rp_mirror_enable: bool = bool(config.get("rp_mirror_enable", True))
        self.rp_time_strip_default: bool = bool(config.get("rp_time_strip_default", True))
        self.rp_time_timezone: str = str(config.get("rp_time_timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
        self.rp_time_show_lunar: bool = bool(config.get("rp_time_show_lunar", False))
        self.rp_time_show_festival: bool = bool(config.get("rp_time_show_festival", True))
        self.rp_time_show_rhythm: bool = bool(config.get("rp_time_show_rhythm", True))
        self.rp_repetition_k_recent: int = int(config.get("rp_repetition_k_recent", 20))
        self.rp_repetition_opener_threshold: int = int(config.get("rp_repetition_opener_threshold", 2))
        self.rp_repetition_phrase_threshold: int = int(config.get("rp_repetition_phrase_threshold", 3))
        self.rp_phrase_inject_max: int = int(config.get("rp_phrase_inject_max", 3))
        self.rp_opener_inject_max: int = int(config.get("rp_opener_inject_max", 3))
        self.rp_mirror_k_recent: int = int(config.get("rp_mirror_k_recent", 5))
        self.rp_mirror_count_threshold: int = int(config.get("rp_mirror_count_threshold", 2))
        self.rp_mirror_similarity: float = float(config.get("rp_mirror_similarity", 0.5))
        self.rp_mirror_cooldown_turns: int = int(config.get("rp_mirror_cooldown_turns", 3))
        self.rp_mirror_cluster_enable: bool = bool(config.get("rp_mirror_cluster_enable", True))
        self.rp_inject_max_chars: int = int(config.get("rp_inject_max_chars", 1400))
        self.rp_verbose: bool = bool(config.get("rp_verbose", False))

        self.cache_friendly_system_guard_enable: bool = bool(config.get("cache_friendly_system_guard_enable", True))
        self.cache_prefix_drift_enable: bool = bool(config.get("cache_prefix_drift_enable", True))

        # 内部状态
        self._memos: MemosClient | None = None
        self._vec: VectorStore | None = None
        self._episodes: EpisodicStore | None = None
        # 4.6.0-test3：独立职责模块；main 只负责调用顺序与生命周期。
        self._diary_pipeline = DiaryPipeline(self)
        self._query_planner = QueryPlanner(self)
        self._scene_splitter = SceneSplitter(
            gap_seconds=self.scene_split_gap_seconds,
            max_scenes=self.diary_count_max_cap,
        )
        self._emb_provider = None
        self._rerank_provider = None
        self._emb_dim: int | None = None
        self._emb_model_id: str | None = None
        self._buffer: dict[str, list[dict[str, Any]]] = {}
        self._buffer_last_turn: dict[str, int] = {}
        self._initialized: bool = False
        self._init_error: str | None = None
        # v4.5.0: serialize lazy init so a burst of first-turn requests cannot
        # run the whole init sequence (and its background tasks) twice.
        self._init_lock = asyncio.Lock()
        self._webui_start_lock = asyncio.Lock()
        self._warmup_task: asyncio.Task | None = None

        # v1.2 运行态
        from collections import OrderedDict, deque  # noqa: local import 防顶层污染
        self._emb_cache: "OrderedDict[str, list[float]]" = OrderedDict()
        # per-session 最近注入过的 memo_name 滑动窗口(防重复注入)
        self._recent_injected: dict[str, Any] = {}
        # v1.4.2: per-session enhancer 时间缓存
        self._session_time: dict[str, dict] = {}
        # v1.4.4: 行为日志环形缓存 (WebUI 控制台用)
        self._log_events: list[dict] = []
        self._log_max = 500
        # v1.4.4: 环形事件日志(WebUI /console 用)
        self._deque_factory = deque
        self._last_injection_stats: list[dict[str, Any]] = []
        self._telemetry_lock = threading.RLock()
        self._telemetry_dirty = False
        self._telemetry_last_save = 0.0
        self._compress_count: int = 0
        self._last_compress_ts: float = 0.0
        self._eod_flush_done: dict[str, dict[str, Any]] = {}
        self._eod_flush_retry: dict[str, dict[str, Any]] = {}
        self._eod_last_status: dict[str, Any] = {}
        self._eod_flush_task = None
        self._last_diary_embeddings: list = []
        self._compress_locks: dict[str, asyncio.Lock] = {}
        # v1.3 后台对账
        self._reconcile_task = None
        self._profile_task = None
        self._context_archive_task = None
        self._data_backup_task = None
        self._restore_apply_lock = asyncio.Lock()
        self._pending_restore_checked = False
        self._startup_restore_result: dict[str, Any] = {}
        self._episode_migration_task = None
        self._passage_vector_migration_task = None
        self._source_turn_vector_migration_task = None
        self._passage_vector_migration_state: dict[str, Any] = {"status": "pending"}
        self._source_turn_vector_migration_state: dict[str, Any] = {"status": "pending"}
        self._semantic_state_task = None
        self._semantic_state_lock = asyncio.Lock()
        self._semantic_state_pending_tasks: set[asyncio.Task] = set()
        self._semantic_state_last_defer_key = ""
        self._episode_migration_ready: bool = False
        self._episode_migration_state: dict[str, Any] = {"status": "pending"}
        self._last_reconcile_ts: float = 0.0
        self._reconcile_stats: dict[str, int] = {"deleted": 0, "last_run_deleted": 0}
        self._context_stats: dict[str, Any] = {}
        self._seen_context_sessions: set[str] = set()
        self._command_context_candidates: dict[str, list[tuple[float, str]]] = {}
        self._command_context_locks: dict[str, asyncio.Lock] = {}
        self._load_runtime_telemetry()
        self._last_mirror_inject: dict[str, int] = {}
        self._rp_stats: dict[str, Any] = {}
        self._provider_cache_stats: dict[str, Any] = {}
        self._system_cache_stats: dict[str, Any] = {}
        self._prefix_cache_stats: dict[str, Any] = {}
        self._prefix_last_snapshot: dict[str, Any] = {}
        self._last_request_ts: dict[str, float] = {}
        self._managed_memos = ManagedMemosSidecar(self)
        self.time_enhancer = TimeEnhancer(self.rp_time_timezone) if TimeEnhancer is not None else None
        self._xinchao = XinchaoController(
            self,
            Path(self.vec_db_path).expanduser().resolve().parent,
        )
        self._data_backup = DataBackupManager(
            backup_dir=self.data_backup_dir,
            vec_db_path=self.vec_db_path,
            episodic_db_path=self.episodic_db_path,
            plugin_version=_PLUGIN_VERSION,
            interval_days=self.data_backup_interval_days,
            keep=self.data_backup_keep,
            enabled=self.data_backup_enable,
        )
        self._time_insight = IntegratedTimeInsightService(self)
        # v1.3 WebUI
        self._webui: "WebUIServer | None" = None
        logger.info(
            "[memos-memory] Plugin v%s loaded | character='%s' | memos=%s | emb=%s | enhancer=%s time=%s context=%s",
            _PLUGIN_VERSION, self.character_name or "(unset)",
            self.memos_base_url, self.emb_provider_id or "(auto)",
            self.rp_enhancer_enable,
            self.rp_time_enable and self.time_enhancer is not None,
            self.context_governance_enable,
        )

    # ---------- 懒初始化 ----------
    async def _ensure_init(self) -> bool:
        if self._initialized:
            return True
        # v4.5.0: without this lock, concurrent first-turn requests (or a
        # request racing the activation warmup) could run init twice and
        # double-start WebUI / background migration tasks.
        async with self._init_lock:
            if self._initialized:
                return True
            return await self._ensure_init_inner()

    async def _ensure_webui_started(self) -> bool:
        """Start exactly one WebUI instance across activation and lazy warmup."""
        if WebUIServer is None or not self.webui_enable:
            return False
        async with self._webui_start_lock:
            if self._webui is not None:
                return True
            server = WebUIServer(self)
            try:
                await server.start()
            except BaseException:
                try:
                    await server.stop()
                except BaseException:
                    pass
                raise
            self._webui = server
            logger.info(
                "[memos-memory] WebUI 已启动: http://%s:%d/",
                self.webui_host,
                self.webui_port,
            )
            return True

    async def _ensure_init_inner(self) -> bool:
        await self._apply_pending_data_restore_once()
        if self._init_error:
            # v1.6: allow retry (provider may load after plugin)
            self._init_error = None
        try:
            if self.memos_mode == "managed":
                await self._managed_memos.start()
            self._memos = MemosClient(self.memos_base_url, self.memos_token, self.memos_timeout)
            ok = await self._memos.ping()
            if not ok:
                self._init_error = "memos ping failed"
                logger.error("[memos-memory] memos 服务不可达: %s", self.memos_base_url)
                return False

            # emb provider
            prov = None
            if self.emb_provider_id:
                try:
                    prov = self.context.get_provider_by_id(self.emb_provider_id)
                except Exception as e:
                    logger.warning("[memos-memory] get_provider_by_id(%s) 失败: %s", self.emb_provider_id, e)
            if prov is None:
                try:
                    all_emb = self.context.get_all_embedding_providers()
                    prov = all_emb[0] if all_emb else None
                except Exception as e:
                    logger.warning("[memos-memory] get_all_embedding_providers 失败: %s", e)
            if prov is None:
                self._init_error = "no embedding provider"
                logger.error("[memos-memory] 没有可用的 embedding provider,请在 AstrBot 配置里选一个")
                return False
            self._emb_provider = prov
            self._emb_model_id = _resolve_provider_name(prov)
            try:
                self._emb_dim = int(prov.get_dim())
            except Exception:
                self._emb_dim = None  # 首次 embed 时推断

            # 向量库
            self._vec = VectorStore(self.vec_db_path, self._emb_dim, self._emb_model_id)
            await self._vec.init()
            if self.episodic_memory_enable:
                self._episodes = EpisodicStore(
                    self.episodic_db_path,
                    self._emb_dim,
                    self._emb_model_id,
                )
                self._episodes._snapshot_keep = self.db_snapshot_keep
                self._episodes._preview_keep = 20
                await self._episodes.init()
                diagnosis = self._episodes.startup_diagnosis()
                identity = diagnosis.get("identity") or {}
                record_episode_db_location(self.episodic_db_path)
                logger.info(
                    "[memory][archive] startup uuid=%s path=%s schema=%s gen=%s issue=%s",
                    str(identity.get("database_uuid") or "")[:12],
                    identity.get("canonical_db_path") or identity.get("db_path") or "",
                    identity.get("schema_version") or "",
                    str((self._episodes.stats().get("active_generation") or ""))[:8],
                    diagnosis.get("startup_issue") or "none",
                )
                if not self.episodic_auto_migrate:
                    self._episode_migration_ready = True
                    self._episode_migration_state = {"status": "manual_mode"}

            # 重启恢复:把上一进程还没压完的缓冲读回内存
            try:
                sessions = self._vec._connect().execute(
                    "SELECT DISTINCT session_id FROM pending_messages ORDER BY session_id"
                ).fetchall()
                recovered = 0
                for r in sessions:
                    sid = r["session_id"]
                    msgs = await self._vec.buffer_take(sid, last_seq=-1)
                    if msgs:
                        self._buffer.setdefault(sid, []).extend(msgs)
                        recovered += len(msgs)
                if recovered:
                    logger.info("[memos-memory] 从 sqlite 恢复 %d 条未压缩对话 (跨 %d 个 session)",
                                recovered, len(sessions))
            except Exception as e:
                logger.debug("[memos-memory] 重启恢复失败 (不影响主流程): %s", e)

            # 换模型检测
            stored = self._vec.get_stored_meta()
            if stored and stored.get("emb_model_id") and stored["emb_model_id"] != self._emb_model_id:
                logger.warning(
                    "[memos-memory] emb 模型从 '%s' 变为 '%s',请运行 /memos-reindex 全量重灌,否则检索不准",
                    stored["emb_model_id"], self._emb_model_id,
                )

            # v1.7: init rerank provider (native AstrBot RerankProvider)
            if self.rerank_provider_id:
                try:
                    rp = self.context.get_provider_by_id(self.rerank_provider_id)
                    if hasattr(rp, "rerank"):
                        self._rerank_provider = rp
                        logger.info("[memos-memory] rerank provider: %s", self.rerank_provider_id)
                    else:
                        logger.warning("[memos-memory] provider '%s' has no rerank method", self.rerank_provider_id)
                except Exception as e:
                    logger.warning("[memos-memory] rerank provider '%s' not found: %s", self.rerank_provider_id, e)

            try:
                self._vec._bm25_use_jieba = (self.bm25_tokenizer == "jieba")
                self._vec._init_extra_tables()
            except Exception as e:
                logger.warning("[memos-memory] extra tables init failed: %s", e)
            self._initialized = True
            self._eod_flush_task = asyncio.ensure_future(self._eod_flush_loop())
            if self.data_backup_enable and self._data_backup_task is None:
                self._data_backup_task = asyncio.create_task(self._data_backup_loop())
            logger.info("[memos-memory] init ok | emb=%s dim=%s vec=%s",
                        self._emb_model_id, self._emb_dim, self.vec_db_path)

            if self._episodes is not None and self.episodic_auto_migrate:
                self._episode_migration_task = asyncio.create_task(
                    self._auto_migrate_legacy_episodes()
                )
            if self.passage_vector_auto_migrate:
                self._passage_vector_migration_task = asyncio.create_task(
                    self._auto_migrate_passage_vectors()
                )
            else:
                strategy = self._vec.get_meta_value("passage_embedding_strategy") if self._vec else ""
                self._passage_vector_migration_state = {
                    "status": "ready" if strategy == _PASSAGE_VECTOR_STRATEGY else "manual",
                    "strategy": strategy or "legacy_mixed",
                }
            if self._episodes is not None and self.source_turn_vector_auto_migrate:
                self._source_turn_vector_migration_task = asyncio.create_task(
                    self._auto_migrate_source_turn_vectors()
                )
            else:
                self._source_turn_vector_migration_state = {"status": "manual"}
            if self._episodes is not None and self.semantic_state_enable:
                self._semantic_state_task = asyncio.create_task(self._semantic_state_maintenance_loop())

            # 启动后台对账任务(v1.3)
            if self.enable_auto_reconcile and self.reconcile_interval > 0:
                self._reconcile_task = asyncio.create_task(self._background_reconcile())
                logger.info("[memos-memory] 后台对账已启动,间隔 %d 秒", self.reconcile_interval)

            if (
                self.enable_affiliate_profile
                and self.profile_auto_update_days > 0
                and self._profile_task is None
                and not (self.semantic_state_enable and self.semantic_state_replace_profile)
            ):
                self._profile_task = asyncio.create_task(self._profile_auto_loop())
                logger.info("[memos-memory] 内置画像自动更新已启动,间隔 %d 天", self.profile_auto_update_days)

            # 启动 WebUI(v1.3):独立端口,失败不阻塞插件初始化
            try:
                await self._ensure_webui_started()
            except Exception as e:
                logger.warning("[memos-memory] WebUI 启动失败 (不影响主流程): %s", e)

            return True
        except Exception as e:
            self._init_error = f"init failed: {e}"
            logger.exception("[memos-memory] %s", self._init_error)
            return False

    # ---------- 工具 ----------
    @staticmethod
    def _split_into_passages(
        text: str,
        max_chars: int = 280,
        overlap_chars: int = 60,
    ) -> list[dict[str, Any]]:
        """Split by complete sentence boundaries while retaining stable source offsets."""
        source = str(text or "")
        if not source:
            return []
        max_chars = max(80, int(max_chars or 280))
        overlap_chars = max(0, min(max_chars // 2, int(overlap_chars or 0)))
        spans: list[tuple[int, int]] = []
        for match in re.finditer(r"[^。！？!?\n]+(?:[。！？!?]+|\n+|$)", source):
            start, end = match.span()
            while start < end and source[start].isspace():
                start += 1
            while end > start and source[end - 1].isspace():
                end -= 1
            if start < end:
                spans.append((start, end))
        if not spans:
            spans = [(0, len(source))]

        passages: list[dict[str, Any]] = []
        cursor = 0
        while cursor < len(spans):
            start = spans[cursor][0]
            end_idx = cursor
            end = spans[cursor][1]
            if end - start > max_chars:
                pos = start
                while pos < end:
                    cut = min(end, pos + max_chars)
                    passages.append({"text": source[pos:cut].strip(), "char_start": pos, "char_end": cut})
                    if cut >= end:
                        break
                    pos = max(pos + 1, cut - overlap_chars)
                cursor += 1
                continue
            while end_idx + 1 < len(spans) and spans[end_idx + 1][1] - start <= max_chars:
                end_idx += 1
                end = spans[end_idx][1]
            passages.append({"text": source[start:end].strip(), "char_start": start, "char_end": end})
            if end_idx + 1 >= len(spans):
                break
            next_cursor = end_idx + 1
            if overlap_chars > 0:
                threshold = end - overlap_chars
                candidate = end_idx
                while candidate > cursor and spans[candidate][0] >= threshold:
                    candidate -= 1
                next_cursor = max(cursor + 1, candidate + 1)
            cursor = next_cursor
        for idx, passage in enumerate(passages):
            passage["passage_index"] = idx
        return [p for p in passages if p.get("text")]

    @staticmethod
    def _split_into_chunks(text: str, max_chars: int = _DEFAULT_CHUNK_CHARS, overlap_chars: int = _DEFAULT_OVERLAP_CHARS) -> list[str]:
        return [p["text"] for p in MemosMemoryPlugin._split_into_passages(text, max_chars, overlap_chars)] or [text]

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()[:24]

    @staticmethod
    def _passage_embedding_text(passage: str, machine: dict[str, Any]) -> str:
        # v4.3 keeps passage vectors local. Whole-event cues live in the episode
        # card index; repeating them on every passage made the right diary easy
        # to find but blurred which paragraph actually matched the query.
        return " ".join(str(passage or "").split()).strip()

    # ---------- 行为日志(v1.4.4) ----------
    def _runtime_telemetry_path(self) -> Path:
        db_path = Path(os.path.expanduser(self.vec_db_path or "./data/astrbot_plugin_memos_memory/memories.db"))
        return db_path.parent / "runtime_telemetry.json"

    def _load_runtime_telemetry(self) -> None:
        """Restore recent WebUI runtime samples after plugin reload/restart."""
        try:
            path = self._runtime_telemetry_path()
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            events = payload.get("events", []) if isinstance(payload, dict) else []
            stats = payload.get("injection_stats", []) if isinstance(payload, dict) else []
            if isinstance(events, list):
                self._log_events = [x for x in events if isinstance(x, dict)][-self._log_max:]
            if isinstance(stats, list):
                self._last_injection_stats = [x for x in stats if isinstance(x, dict)][-120:]
                for stat in self._last_injection_stats:
                    session = str(stat.get("session") or "").strip()
                    if session:
                        self._seen_context_sessions.add(session)
        except Exception as exc:
            logger.debug("[memos-memory] runtime telemetry restore failed: %s", exc)

    def _save_runtime_telemetry(self, force: bool = False) -> None:
        """Persist bounded runtime telemetry for WebUI and hot-reload continuity."""
        now = time.time()
        if not force and (not self._telemetry_dirty or now - self._telemetry_last_save < 0.75):
            return
        try:
            with self._telemetry_lock:
                path = self._runtime_telemetry_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "version": _PLUGIN_VERSION,
                    "saved_ts": now,
                    "events": self._log_events[-self._log_max:],
                    "injection_stats": self._last_injection_stats[-120:],
                }
                temp_path = path.with_suffix(path.suffix + ".tmp")
                temp_path.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                    encoding="utf-8",
                )
                temp_path.replace(path)
                self._telemetry_dirty = False
                self._telemetry_last_save = now
        except Exception as exc:
            logger.debug("[memos-memory] runtime telemetry persist failed: %s", exc)

    def _log_event(self, category: str, message: str, detail: dict | None = None) -> None:
        """记录一条行为日志。环形缓存,最多 _log_max 条。category: enhancer/compress/recall/inject/sync/system。"""
        import time as _t
        entry = {
            "ts": _t.time(),
            "ts_iso": _t.strftime("%H:%M:%S", _t.localtime()),
            "category": category,
            "message": message,
            "detail": detail or {},
        }
        self._log_events.append(entry)
        if len(self._log_events) > self._log_max:
            self._log_events = self._log_events[-self._log_max:]
        self._telemetry_dirty = True
        self._save_runtime_telemetry()
        # 同时输出到 AstrBot 控制台(保留原有日志级别)
        logger.info("[memos-memory][%s] %s", category, message)

    def _normalize_contexts(self, raw_ctx: Any) -> list[dict]:
        if raw_ctx is None:
            return []
        if isinstance(raw_ctx, list):
            return raw_ctx
        try:
            return list(raw_ctx)
        except Exception:
            return []

    @staticmethod
    def _ctx_role(ctx: Any) -> str:
        if isinstance(ctx, dict):
            return str(ctx.get("role") or "").lower()
        return str(getattr(ctx, "role", "") or "").lower()

    @staticmethod
    def _ctx_content(ctx: Any) -> str:
        if isinstance(ctx, dict):
            c = ctx.get("content", "")
        else:
            c = getattr(ctx, "content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            texts: list[str] = []
            for item in c:
                if isinstance(item, str):
                    texts.append(item)
                    continue
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").lower()
                    if item_type == "text" and item.get("text"):
                        texts.append(str(item.get("text")))
                    elif item.get("text"):
                        texts.append(str(item.get("text")))
                    elif item.get("content"):
                        texts.append(str(item.get("content")))
                    continue
                text = getattr(item, "text", None)
                if text is None:
                    text = getattr(item, "content", None)
                if text:
                    texts.append(str(text))
            if texts:
                return "\n".join(t for t in texts if t.strip())
        try:
            return json.dumps(c, ensure_ascii=False)
        except Exception:
            return str(c or "")

    @staticmethod
    def _command_text_key(text: Any) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if normalized.startswith("/"):
            normalized = normalized[1:].lstrip()
        return normalized[:4000]

    @staticmethod
    def _event_extra(event: AstrMessageEvent, key: str, default: Any = None) -> Any:
        try:
            return event.get_extra(key, default)
        except TypeError:
            try:
                value = event.get_extra(key)
                return default if value is None else value
            except Exception:
                return default
        except Exception:
            return default

    @classmethod
    def _event_is_command(cls, event: AstrMessageEvent) -> bool:
        if bool(cls._event_extra(event, "memos_memory_command_event", False)):
            return True
        handlers = cls._event_extra(event, "activated_handlers", []) or []
        for handler in handlers:
            for event_filter in getattr(handler, "event_filters", []) or []:
                if event_filter.__class__.__name__ in {"CommandFilter", "CommandGroupFilter"}:
                    return True
        try:
            text = str(event.get_message_str() or "").strip()
        except Exception:
            text = str(getattr(event, "message_str", "") or "").strip()
        return bool(re.match(r"^/[^\s/]{1,64}(?:\s|$)", text))

    @classmethod
    def _command_event_texts(cls, event: AstrMessageEvent, req: ProviderRequest | None = None) -> list[str]:
        texts: list[Any] = [getattr(event, "message_str", "")]
        try:
            texts.append(event.get_message_str())
        except Exception:
            pass
        if req is not None:
            texts.append(getattr(req, "prompt", ""))
        keys: list[str] = []
        for text in texts:
            key = cls._command_text_key(text)
            if key and key not in keys:
                keys.append(key)
        return keys

    def _remember_command_candidates(self, umo: str, keys: list[str]) -> None:
        if not hasattr(self, "_command_context_candidates"):
            self._command_context_candidates = {}
        now = time.time()
        current = [
            (float(ts), str(key))
            for ts, key in self._command_context_candidates.get(umo, [])
            if now - float(ts) <= 900 and key
        ]
        seen = {key for _, key in current}
        for key in keys:
            normalized = self._command_text_key(key)
            if normalized and normalized not in seen:
                current.append((now, normalized))
                seen.add(normalized)
        self._command_context_candidates[umo] = current[-32:]

    def _recent_command_candidates(self, umo: str) -> set[str]:
        if not hasattr(self, "_command_context_candidates"):
            self._command_context_candidates = {}
        now = time.time()
        current = [
            (float(ts), str(key))
            for ts, key in self._command_context_candidates.get(umo, [])
            if now - float(ts) <= 900 and key
        ]
        if current:
            self._command_context_candidates[umo] = current[-32:]
        else:
            self._command_context_candidates.pop(umo, None)
        return {key for _, key in current}

    def _strip_command_turns(
        self,
        history: list[dict],
        command_keys: set[str],
        *,
        minimum_index: int = 0,
    ) -> tuple[list[dict], int, int]:
        if not history or not command_keys:
            return history, 0, 0
        cleaned: list[dict] = []
        removed_turns = 0
        removed_messages = 0
        index = 0
        minimum_index = max(0, int(minimum_index or 0))
        while index < len(history):
            item = history[index]
            role = self._ctx_role(item)
            key = self._command_text_key(self._ctx_content(item))
            if index >= minimum_index and role == "user" and key in command_keys:
                removed_turns += 1
                removed_messages += 1
                index += 1
                while index < len(history) and self._ctx_role(history[index]) != "user":
                    following = history[index]
                    if self._ctx_role(following) in {"system", "developer"}:
                        cleaned.append(following)
                    else:
                        removed_messages += 1
                    index += 1
                continue
            cleaned.append(item)
            index += 1
        return cleaned, removed_turns, removed_messages

    def _record_command_context_cleanup(
        self,
        umo: str,
        *,
        removed_turns: int,
        removed_messages: int,
        mode: str,
    ) -> None:
        previous = dict(self._context_stats.get(umo, {}) or {})
        previous.update({
            "command_exclusion": True,
            "command_removed_turns": int(previous.get("command_removed_turns") or 0) + removed_turns,
            "command_removed_messages": int(previous.get("command_removed_messages") or 0) + removed_messages,
            "command_cleanup_mode": mode,
            "command_cleanup_ts": time.time(),
        })
        self._context_stats[umo] = previous
        self._log_event("context", f"指令回合已从上下文剔除: {removed_turns}轮/{removed_messages}条", {
            "session": umo[:24], "mode": mode,
            "removed_turns": removed_turns, "removed_messages": removed_messages,
        })

    def _sanitize_recent_command_context(self, event: AstrMessageEvent, req: ProviderRequest) -> int:
        umo = event.unified_msg_origin
        command_keys = self._recent_command_candidates(umo)
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        if not command_keys or not contexts:
            return 0
        # A stale command can only be near the tail. Restricting the scan avoids
        # deleting an unrelated old message that happens to equal a command name.
        minimum_index = max(0, len(contexts) - 16)
        cleaned, removed_turns, removed_messages = self._strip_command_turns(
            contexts, command_keys, minimum_index=minimum_index,
        )
        if removed_messages <= 0:
            return 0
        req.contexts = cleaned
        conversation = getattr(req, "conversation", None)
        if conversation is not None:
            try:
                conversation.token_usage = 0
            except Exception:
                pass
        self._record_command_context_cleanup(
            umo, removed_turns=removed_turns, removed_messages=removed_messages,
            mode="request_safety_net",
        )
        return removed_messages

    def _prepare_command_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        umo = event.unified_msg_origin
        keys = self._command_event_texts(event, req)
        self._sanitize_recent_command_context(event, req)
        self._remember_command_candidates(umo, keys)
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        try:
            event.set_extra("memos_memory_command_event", True)
            event.set_extra("memos_memory_command_texts", keys)
            event.set_extra("memos_memory_command_history_before", len(contexts))
        except Exception:
            pass
        self._log_event("context", "指令回合隔离: 不参与记忆、心潮与上下文留存", {
            "session": umo[:24], "history_before": len(contexts),
        })

    async def _remove_persisted_command_turn(self, event: AstrMessageEvent) -> int:
        before = self._event_extra(event, "memos_memory_command_history_before", None)
        if before is None:
            return 0
        try:
            minimum_index = max(0, int(before))
        except (TypeError, ValueError):
            return 0
        umo = event.unified_msg_origin
        keys = {
            self._command_text_key(text)
            for text in (self._event_extra(event, "memos_memory_command_texts", []) or [])
            if self._command_text_key(text)
        }
        if not keys:
            keys = self._recent_command_candidates(umo)
        if not keys:
            return 0
        if not hasattr(self, "_command_context_locks"):
            self._command_context_locks = {}
        lock = self._command_context_locks.setdefault(umo, asyncio.Lock())
        async with lock:
            mgr, cid, history = await self._current_conversation_history(event)
            if not cid or not history:
                return 0
            cleaned, removed_turns, removed_messages = self._strip_command_turns(
                history, keys, minimum_index=minimum_index,
            )
            if removed_messages <= 0:
                return 0
            await mgr.update_conversation(umo, cid, history=cleaned, token_usage=0)
            self._record_command_context_cleanup(
                umo, removed_turns=removed_turns, removed_messages=removed_messages,
                mode="after_message_sent",
            )
            return removed_messages

    def _trim_context_history(self, history: list[dict], keep_recent: int) -> tuple[list[dict], int]:
        keep_recent = max(0, int(keep_recent or 0))
        if keep_recent <= 0 or len(history) <= keep_recent:
            return history, 0
        if not self.context_preserve_system_messages:
            return history[-keep_recent:], max(0, len(history) - keep_recent)
        sys_items = [m for m in history if self._ctx_role(m) in {"system", "developer"}]
        body = [m for m in history if self._ctx_role(m) not in {"system", "developer"}]
        kept_body = body[-keep_recent:]
        removed = max(0, len(body) - len(kept_body))
        return sys_items + kept_body, removed

    def _govern_request_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.context_governance_enable:
            return
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        total = len(contexts)
        if total < max(1, self.context_min_messages_before_trim):
            self._context_stats[event.unified_msg_origin] = {"total": total, "trimmed": 0, "kept": total, "mode": "request"}
            return
        new_contexts, removed = self._trim_context_history(contexts, self.context_keep_recent_messages)
        if removed <= 0:
            return
        stat = {"total": total, "trimmed": removed, "kept": len(new_contexts), "mode": "request"}
        # v4.5.0 (incident root cause 3): AstrBot persists the messages the
        # agent actually used back into the conversation, so this trim is NOT
        # a per-request-only operation — it can permanently shorten AstrBot's
        # stored history. Before trimming, write a local JSON backup of the
        # pre-trim history; if the backup cannot be written, skip trimming
        # this turn rather than risk unrecoverable loss.
        if self.context_trim_backup_enable:
            try:
                conversation = getattr(req, "conversation", None)
                cid = None
                for attr in ("cid", "conversation_id", "id"):
                    value = getattr(conversation, attr, None)
                    if value:
                        cid = str(value)
                        break
                plan = {
                    "total": total, "kept": len(new_contexts), "removed": removed,
                    "keep_recent_messages": self.context_keep_recent_messages,
                    "reason": "request_trim",
                }
                stat["backup_path"] = self._write_context_backup(
                    event.unified_msg_origin, cid, contexts, plan,
                )
            except Exception as exc:
                logger.warning(
                    "[memos-memory] context trim skipped: pre-trim backup failed (%s)", exc,
                )
                self._log_event("system", "context trim skipped: backup failed", {
                    "total": total, "error": str(exc)[:200],
                })
                return
        req.contexts = new_contexts
        # v4.5.0 (incident root cause 2): AstrBot trusts a positive
        # conversation.token_usage and skips re-estimation, so the pre-trim
        # count would trigger a pointless second compression that can chew up
        # the freshly injected memory blocks. Invalidate it after a real trim.
        try:
            conversation = getattr(req, "conversation", None)
            if conversation is not None and getattr(conversation, "token_usage", None):
                conversation.token_usage = 0
                stat["token_usage_reset"] = True
        except Exception as exc:
            logger.debug("[memos-memory] token_usage reset failed open: %s", exc)
        self._context_stats[event.unified_msg_origin] = stat
        self._log_event("system", f"context request trim: {total}->{len(new_contexts)}", stat)

    def _extra_parts_text(self, req: ProviderRequest) -> str:
        parts = getattr(req, "extra_user_content_parts", None) or []
        texts = []
        for p in parts:
            text = getattr(p, "text", None)
            if text is None and isinstance(p, dict):
                text = p.get("text")
            if text:
                texts.append(str(text))
        return "\n".join(texts)

    def _request_context_chars(self, req: ProviderRequest) -> int:
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        return sum(len(self._ctx_content(c)) for c in contexts)

    def _strip_current_time_context_parts(self, parts: Any) -> tuple[list[Any], int]:
        if not parts:
            return [], 0
        kept: list[Any] = []
        removed = 0
        for part in list(parts):
            text = getattr(part, "text", None)
            if text is None and isinstance(part, dict):
                text = part.get("text")
            if isinstance(text, str) and ("<CurrentTimeContext" in text or "</CurrentTimeContext>" in text):
                removed += 1
                cleaned = re.sub(
                    r"(?is)<CurrentTimeContext\b[^>]*>.*?</CurrentTimeContext>",
                    "",
                    text,
                ).strip()
                if not cleaned:
                    continue
                if isinstance(part, dict):
                    part = dict(part)
                    part["text"] = cleaned
                else:
                    try:
                        part.text = cleaned
                    except Exception:
                        continue
            kept.append(part)
        return kept, removed

    def _cache_prefix_snapshot(self, req: ProviderRequest) -> dict[str, Any]:
        sys_text = getattr(req, "system_prompt", "") or ""
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        history_parts = []
        for msg in contexts:
            role = ""
            if isinstance(msg, dict):
                role = str(msg.get("role", "") or "")
            else:
                role = str(getattr(msg, "role", "") or "")
            history_parts.append(f"{role}:{self._ctx_content(msg)}")
        history_text = "\n".join(history_parts)
        sys_hash = self._short_hash(sys_text)
        history_hash = self._short_hash(history_text)
        combined_hash = self._short_hash(sys_hash + ":" + history_hash)
        return {
            "system_hash": sys_hash,
            "history_hash": history_hash,
            "combined_hash": combined_hash,
            "system_chars": len(sys_text),
            "history_turns": len(contexts),
            "history_chars": len(history_text),
            "dynamic_markers": self._system_dynamic_marker_hits(sys_text),
        }

    def _record_cache_prefix_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self.cache_prefix_drift_enable:
            return
        umo = event.unified_msg_origin
        snap = self._cache_prefix_snapshot(req)
        prev = self._prefix_last_snapshot.get(umo)
        changed = []
        if prev:
            if prev.get("system_hash") != snap.get("system_hash"):
                changed.append("system")
            if prev.get("history_hash") != snap.get("history_hash"):
                changed.append("history")
        stat = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "session": umo,
            "changed": changed,
            "stable": bool(prev) and not changed,
            **snap,
            "prev_system_hash": prev.get("system_hash") if prev else "",
            "prev_history_hash": prev.get("history_hash") if prev else "",
        }
        self._prefix_last_snapshot[umo] = snap
        self._prefix_cache_stats[umo] = stat
        if changed or snap.get("dynamic_markers"):
            self._log_event("system", "cache prefix drift", stat)

    @staticmethod
    def _short_hash(text: str) -> str:
        return hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()[:12]

    def _system_dynamic_marker_hits(self, text: str) -> list[str]:
        markers = []
        checks = {
            "CurrentTimeContext": "<CurrentTimeContext" in text or "</CurrentTimeContext>" in text,
            "HistoricalMemory": "<HistoricalMemory" in text or "</HistoricalMemory>" in text,
            "LongTermMemory": "[长期记忆" in text,
        }
        for name, ok in checks.items():
            if ok:
                markers.append(name)
        return markers

    def _stabilize_system_prompt_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Keep system_prompt stable by moving known dynamic plugin blocks into temp extra parts."""
        if not self.cache_friendly_system_guard_enable:
            return
        sys_prompt = getattr(req, "system_prompt", "") or ""
        if not sys_prompt:
            return
        before = sys_prompt
        moved: list[tuple[str, str]] = []
        moved_chars = 0
        dropped_time_blocks = 0
        dynamic_block_patterns = [
            ("CurrentTimeContext", r"(?is)<CurrentTimeContext\b[^>]*>.*?</CurrentTimeContext>"),
            ("HistoricalMemory", r"(?is)<HistoricalMemory\b[^>]*>.*?</HistoricalMemory>"),
        ]
        for name, pattern in dynamic_block_patterns:
            def _move(m):
                nonlocal moved_chars
                block = (m.group(0) or "").strip()
                if block:
                    moved.append((name, block))
                    moved_chars += len(block)
                return "\n"
            sys_prompt = re.sub(pattern, _move, sys_prompt)
        cleaned = "\n".join(line.rstrip() for line in sys_prompt.splitlines() if line.strip())
        if moved:
            try:
                from astrbot.core.agent.message import TextPart
                if getattr(req, "extra_user_content_parts", None) is None:
                    req.extra_user_content_parts = []
                for name, block in moved:
                    if name == "CurrentTimeContext":
                        dropped_time_blocks += 1
                        continue
                    req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
                req.system_prompt = cleaned
            except Exception as exc:
                logger.debug("[memos-memory] system cache guard move failed: %s", exc)
                moved = []
                moved_chars = 0
        after = getattr(req, "system_prompt", "") or before
        stat = {
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "session": event.unified_msg_origin,
            "enabled": True,
            "system_before": len(before),
            "system_after": len(after),
            "hash_before": self._short_hash(before),
            "hash_after": self._short_hash(after),
            "moved_blocks": len(moved),
            "moved_chars": moved_chars,
            "dropped_time_blocks": dropped_time_blocks,
            "dynamic_markers_after": self._system_dynamic_marker_hits(after),
        }
        self._system_cache_stats[event.unified_msg_origin] = stat
        if moved or stat["dynamic_markers_after"]:
            self._log_event("system", "system cache guard", stat)

    def _remember_injection_stats(self, stat: dict[str, Any]) -> None:
        request_id = str(stat.get("request_id") or "")
        replaced = False
        if request_id:
            for idx in range(len(self._last_injection_stats) - 1, -1, -1):
                if str(self._last_injection_stats[idx].get("request_id") or "") == request_id:
                    self._last_injection_stats[idx] = stat
                    replaced = True
                    break
        if not replaced:
            self._last_injection_stats.append(stat)
        if len(self._last_injection_stats) > 120:
            del self._last_injection_stats[:-120]
        self._telemetry_dirty = True
        self._save_runtime_telemetry(force=True)

    def _request_injection_stat(self, event: AstrMessageEvent, req: ProviderRequest) -> dict[str, Any]:
        """Capture what this request currently sends, even when recall yields no diary."""
        session = event.unified_msg_origin
        rp_stat = self._rp_stats.get(session, {}) if isinstance(self._rp_stats, dict) else {}
        rp_block_chars = rp_stat.get("block_chars", {}) if isinstance(rp_stat, dict) else {}
        current_time_chars = int(rp_block_chars.get("time") or 0) if isinstance(rp_block_chars, dict) else 0
        enhancer_total_chars = int(rp_stat.get("used") or 0) if isinstance(rp_stat, dict) else 0
        enhancer_chars = max(0, enhancer_total_chars - current_time_chars)
        extra_text = self._extra_parts_text(req)
        extra_chars = len(extra_text)
        xinchao_chars = sum(
            len(match.group(0))
            for match in re.finditer(r"(?s)<DynamicMindState\b.*?</DynamicMindState>", extra_text)
        )
        context_chars = self._request_context_chars(req)
        composition = {
            "current_time": current_time_chars,
            "profile": 0,
            "semantic_state": 0,
            "time_insight": 0,
            "diary": 0,
            "evidence": 0,
            "xinchao": xinchao_chars,
            "enhancer": enhancer_chars,
            "context": context_chars,
            "other_extra": max(0, extra_chars - enhancer_total_chars - xinchao_chars),
        }
        context_stat = self._context_stats.get(session, {}) if isinstance(self._context_stats, dict) else {}
        started = time.time()
        return {
            "request_id": f"{time.time_ns():x}",
            "ts": started,
            "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "session": session,
            "count": 0,
            "chars": 0,
            "memo_chars": 0,
            "order": self.inject_order,
            "format": self.inject_format,
            "composition": composition,
            "total_est_chars": sum(composition.values()),
            "context_total": context_stat.get("total"),
            "context_kept": context_stat.get("kept"),
            "context_trimmed": context_stat.get("trimmed"),
            "enhancer_blocks": rp_stat.get("injected", []) if isinstance(rp_stat, dict) else [],
            "outcome": "base_ready",
            "memos": [],
            "preview": "",
        }

    def _finish_request_injection_stat(self, stat: dict[str, Any], outcome: str) -> None:
        stat["outcome"] = outcome
        stat["duration_ms"] = round(max(0.0, time.time() - float(stat.get("ts") or time.time())) * 1000, 1)
        self._remember_injection_stats(stat)

    def _extract_cache_token_usage(self, response: LLMResponse) -> dict[str, int]:
        input_tokens = 0
        cached_tokens = 0
        output_tokens = 0
        raw_usage = None
        for attr in ("raw_completion", "usage", "token_usage"):
            obj = getattr(response, attr, None)
            if obj is None:
                continue
            inner = getattr(obj, "usage", None)
            raw_usage = inner if inner is not None else obj
            break
        if raw_usage is not None:
            input_tokens = int(
                getattr(raw_usage, "prompt_tokens", 0)
                or getattr(raw_usage, "input_tokens", 0)
                or 0
            )
            output_tokens = int(
                getattr(raw_usage, "completion_tokens", 0)
                or getattr(raw_usage, "output_tokens", 0)
                or 0
            )
            ptd = getattr(raw_usage, "prompt_tokens_details", None)
            cached_tokens = int(
                getattr(raw_usage, "prompt_cache_hit_tokens", 0)
                or getattr(raw_usage, "cache_read_input_tokens", 0)
                or getattr(raw_usage, "cached_content_token_count", 0)
                or (getattr(ptd, "cached_tokens", 0) if ptd is not None else 0)
                or 0
            )
        if input_tokens == 0:
            token_usage = None
            for attr in ("token_usage", "usage", "raw_completion"):
                obj = getattr(response, attr, None)
                if obj is None:
                    continue
                if hasattr(obj, "input_other") and hasattr(obj, "input_cached"):
                    token_usage = obj
                    break
                inner = getattr(obj, "usage", None)
                if inner is not None and hasattr(inner, "input_other") and hasattr(inner, "input_cached"):
                    token_usage = inner
                    break
            if token_usage is not None:
                other = int(getattr(token_usage, "input_other", 0) or 0)
                cached_tokens = int(getattr(token_usage, "input_cached", 0) or 0)
                input_tokens = other + cached_tokens
                output_tokens = int(getattr(token_usage, "output", 0) or 0)
        return {"input": input_tokens, "cached": cached_tokens, "output": output_tokens}

    def _rp_enhance_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        request_now: datetime | None = None,
    ) -> None:
        if not self.rp_enhancer_enable:
            return
        if not (self.rp_time_enable or self.rp_repetition_enable or self.rp_mirror_enable):
            return
        try:
            from astrbot.core.agent.message import TextPart
        except Exception:
            return
        if getattr(req, "extra_user_content_parts", None) is None:
            req.extra_user_content_parts = []
        stripped_default_time = False
        if self.rp_time_strip_default and TimeEnhancer is not None:
            try:
                before_extra = self._extra_parts_text(req)
                before_system = getattr(req, "system_prompt", "") or ""
                req.extra_user_content_parts = TimeEnhancer.strip_default_system_reminder(req.extra_user_content_parts)
                if hasattr(TimeEnhancer, "strip_default_datetime_system_prompt"):
                    req.system_prompt = TimeEnhancer.strip_default_datetime_system_prompt(before_system)
                after_extra = self._extra_parts_text(req)
                after_system = getattr(req, "system_prompt", "") or ""
                stripped_default_time = (before_extra != after_extra) or (before_system != after_system)
            except Exception as e:
                logger.debug("[memos-memory] strip default datetime failed: %s", e)
        removed_time_contexts = 0
        if self.rp_time_enable and TimeEnhancer is not None:
            try:
                req.extra_user_content_parts, removed_time_contexts = self._strip_current_time_context_parts(req.extra_user_content_parts)
            except Exception as e:
                logger.debug("[memos-memory] strip stale CurrentTimeContext failed: %s", e)
        existing = self._extra_parts_text(req) + "\n" + (getattr(req, "system_prompt", "") or "")
        budget = max(200, int(self.rp_inject_max_chars or 1400))
        used = 0
        injected: list[str] = []
        skipped: list[str] = []
        stat: dict[str, Any] = {
            "ts": time.time(),
            "session": event.unified_msg_origin,
            "budget": budget,
            "used": 0,
            "injected": injected,
            "block_chars": {},
            "skipped": skipped,
            "stripped_default_time": stripped_default_time,
            "removed_time_contexts": removed_time_contexts,
            "repetition": None,
        }

        def append_block(name: str, text: str) -> bool:
            nonlocal used, existing
            text = (text or "").strip()
            if not text:
                return False
            if used + len(text) > budget:
                skipped.append(f"{name}:budget")
                return False
            part = TextPart(text=text).mark_as_temp()
            if name == "time":
                req.extra_user_content_parts.insert(0, part)
            else:
                req.extra_user_content_parts.append(part)
            existing += "\n" + text
            used += len(text)
            injected.append(name)
            stat.setdefault("block_chars", {})[name] = len(text)
            return True
        time_markers = (
            "<CurrentTimeContext", "</CurrentTimeContext>",
            "<现实感知>", "</现实感知>",
            "当前现实时间:", "现实时间:", "时令:", "对话节奏:",
        )
        has_time_block = any(marker in existing for marker in time_markers)
        if self.rp_time_enable and self.time_enhancer is not None and not has_time_block:
            try:
                if self.rp_time_strip_default:
                    try:
                        req.extra_user_content_parts = TimeEnhancer.strip_default_system_reminder(req.extra_user_content_parts)
                    except Exception:
                        pass
                ctx = self.time_enhancer.get_time_context(
                    event.unified_msg_origin,
                    now=request_now,
                )
                if not self.rp_time_show_lunar:
                    ctx.lunar_date = ""
                if not self.rp_time_show_festival:
                    ctx.festival = None
                if not self.rp_time_show_rhythm:
                    ctx.turns_today = 0
                    ctx.time_since_last = ""
                block = self.time_enhancer.format_injection(ctx)
                if append_block("time", block):
                    detail = {
                        "datetime": getattr(ctx, "datetime_str", ""),
                        "weekday": getattr(ctx, "weekday", ""),
                        "season": getattr(ctx, "season", ""),
                        "period": getattr(ctx, "time_period", ""),
                        "lunar": getattr(ctx, "lunar_date", ""),
                        "festival": getattr(ctx, "festival", "") or "",
                        "turns_today": getattr(ctx, "turns_today", 0),
                        "time_since_last": getattr(ctx, "time_since_last", ""),
                        "chars": len(block),
                    }
                    self._log_event("enhancer", "时间增强已注入", detail)
                    logger.info(
                        "[memos-memory][time] 时间增强已注入: %s %s | %s/%s | 今日第%s轮 | 距上次%s | %d字",
                        detail["datetime"],
                        detail["weekday"],
                        detail["season"],
                        detail["period"],
                        detail["turns_today"],
                        detail["time_since_last"] or "未知",
                        detail["chars"],
                    )
                    if stripped_default_time:
                        self._log_event("enhancer", "已劫持 Astr 默认时间注入", {"mode": "replace", "with": "CurrentTimeContext"})
                else:
                    self._log_event("enhancer", "时间增强跳过", {"reason": ",".join(skipped[-2:]) or "empty", "budget": budget, "used": used})
            except Exception as e:
                self._log_event("enhancer", "时间增强失败", {"error": str(e)})
                logger.debug("[memos-memory] built-in time enhancer failed: %s", e)
        elif self.rp_time_enable and has_time_block:
            skipped.append("time:duplicate")
            self._log_event("enhancer", "时间增强跳过", {"reason": "已有时间块", "markers": [m for m in time_markers if m in existing][:4]})
        elif self.rp_time_enable and self.time_enhancer is None:
            skipped.append("time:unavailable")
            self._log_event("enhancer", "时间增强不可用", {"reason": "TimeEnhancer import failed"})
        if analyze_repetition is None or not (self.rp_repetition_enable or self.rp_mirror_enable):
            stat["used"] = used
            self._rp_stats[event.unified_msg_origin] = stat
            return
        if "<RepetitionGuide" in existing or "<MirrorAlert" in existing:
            skipped.append("guard:duplicate")
            stat["used"] = used
            self._rp_stats[event.unified_msg_origin] = stat
            return
        contexts = self._normalize_contexts(getattr(req, "contexts", None))
        if not contexts:
            skipped.append("guard:no_context")
            stat["used"] = used
            self._rp_stats[event.unified_msg_origin] = stat
            return
        report = analyze_repetition(
            contexts,
            self.rp_repetition_k_recent,
            self.rp_repetition_opener_threshold,
            self.rp_repetition_phrase_threshold,
            self.rp_mirror_k_recent,
            self.rp_mirror_count_threshold,
            self.rp_mirror_similarity,
            self.rp_mirror_cooldown_turns,
            self._last_mirror_inject.get(event.unified_msg_origin, -1),
            self.rp_mirror_cluster_enable,
        )
        stat["repetition"] = {
            "assistant_turns": getattr(report, "total_assistant_turns", 0),
            "openers": len(getattr(report, "repeated_openers", []) or []),
            "phrases": len(getattr(report, "repeated_phrases", []) or []),
            "mirror": bool(getattr(report, "should_inject_mirror", False)),
            "repetition": bool(getattr(report, "should_inject_repetition", False)),
        }
        if self.rp_repetition_enable and getattr(report, "should_inject_repetition", False) and format_repetition_guide is not None:
            try:
                import dataclasses
                inject_report = report
                if self.rp_opener_inject_max < len(report.repeated_openers) or self.rp_phrase_inject_max < len(report.repeated_phrases):
                    inject_report = dataclasses.replace(
                        report,
                        repeated_openers=report.repeated_openers[: self.rp_opener_inject_max],
                        repeated_phrases=report.repeated_phrases[: self.rp_phrase_inject_max],
                    )
                text = format_repetition_guide(inject_report)
                if append_block("repetition", text):
                    self._log_event("enhancer", "repetition guide injected", {
                        "openers": len(report.repeated_openers),
                        "phrases": len(report.repeated_phrases),
                    })
            except Exception as e:
                logger.debug("[memos-memory] repetition guide failed: %s", e)
        if self.rp_mirror_enable and getattr(report, "should_inject_mirror", False) and format_mirror_alert is not None:
            try:
                text = format_mirror_alert(report)
                if append_block("mirror", text):
                    self._last_mirror_inject[event.unified_msg_origin] = report.total_assistant_turns
                    self._log_event("enhancer", "mirror alert injected", {"turns": report.total_assistant_turns})
            except Exception as e:
                logger.debug("[memos-memory] mirror alert failed: %s", e)
        stat["used"] = used
        self._rp_stats[event.unified_msg_origin] = stat

    def _get_logs(self, limit: int = 200, category: str = "") -> list[dict]:
        """返回最近 limit 条日志,可筛选 category。"""
        logs = self._log_events
        if category:
            logs = [e for e in logs if e["category"] == category]
        return logs[-limit:]

    @staticmethod
    def _run_provider_call_blocking(coro_factory):
        result = coro_factory()
        if hasattr(result, "__await__"):
            return asyncio.run(result)
        return result

    async def _retry(self, coro_factory, what: str, timeout: float | None = None, offload_thread: bool = False):
        """带指数退避的重试。coro_factory 是一个无参函数,每次调用返回一个新的 awaitable。
        重试 self.retry_max 次,退避 base*2^i。最终仍失败则抛出最后一次异常。
        """
        last_exc: Exception | None = None
        for attempt in range(max(1, self.retry_max)):
            try:
                # Provider adapters are inconsistent: some methods are async, others
                # perform blocking HTTP before returning. Invoke the factory off-loop
                # when requested, then await any returned coroutine on AstrBot's task.
                result = await asyncio.to_thread(coro_factory) if offload_thread else coro_factory()
                if hasattr(result, "__await__"):
                    aw = result
                else:
                    return result
                if timeout and timeout > 0:
                    return await asyncio.wait_for(aw, timeout=timeout)
                return await aw
            except asyncio.TimeoutError as e:
                last_exc = e
                logger.warning("[memos-memory] %s timeout after %.1fs", what, timeout or 0)
                break
            except Exception as e:
                last_exc = e
                if attempt < self.retry_max - 1:
                    delay = self.retry_base_delay * (2 ** attempt)
                    logger.debug("[memos-memory] %s 第 %d 次失败: %s, %.1fs 后重试",
                                 what, attempt + 1, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.warning("[memos-memory] %s 重试 %d 次仍失败: %s", what, self.retry_max, e)
        if last_exc:
            raise last_exc

    # ---------- 后台对账(v1.3) ----------
    async def _background_reconcile(self):
        """定时任务:拉 memos 全量,删掉 vec 里已被 memos 删除的日记。
        只做删除方向(memos 是真相源);新增/修改交给正常压缩流程和 /memos-sync。
        """
        # 启动后先等一小会,别和初始化抢资源
        await asyncio.sleep(min(30, self.reconcile_interval))
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("[memos-memory] 后台对账异常(不影响主流程): %s", e)
            await asyncio.sleep(self.reconcile_interval)

    def _legacy_episode_payload(self, memo: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        memo_name = str(memo.get("name") or "").strip()
        raw_content = str(memo.get("content") or "")
        if not memo_name or not raw_content:
            return None
        ts_text, body, tags = _parse_memo_content(_strip_internal_metadata(raw_content))
        if not body:
            return None
        source_created_ts, source_updated_ts = memo_source_times(memo)
        time_meta = _time_meta_from_memo_text(
            raw_content,
            ts_text,
            source_created_ts=source_created_ts,
            timezone_name=self.rp_time_timezone,
        )
        importance, manual = _importance_from_memo_text(raw_content, tags)
        memory_type, long_effect, trigger_hint = _memory_meta_from_memo_text(raw_content, body, tags)
        machine = _machine_meta_from_memo_text(raw_content)
        scene_anchor = str(machine.get("scene_anchor") or "").strip()
        if not scene_anchor:
            scene_anchor = re.split(r"(?<=[。！？!?])", body, maxsplit=1)[0][:120]
        retrieval_key = str(machine.get("retrieval_key") or "").strip() or " ".join(body.split())[:360]
        episode = {
            "episode_key": str(machine.get("episode_key") or "legacy"),
            "occurred_at": time_meta.get("occurred_at") or "",
            "event_ts": float(time_meta.get("event_ts") or 0),
            "time_basis": time_meta.get("time_basis") or "unknown",
            "memory_type": memory_type,
            "importance": _normalize_importance(importance, self._auto_importance(body)),
            "manual": manual,
            "scene_anchor": scene_anchor,
            "retrieval_key": retrieval_key,
            "state_change": str(machine.get("state_change") or ""),
            "long_effect": long_effect,
            "trigger_hint": trigger_hint,
            "entities": machine.get("entities") if isinstance(machine.get("entities"), list) else [],
            "affect_before": "",
            "affect_after": "",
            "unresolved": [],
            "evidence": self._derived_diary_evidence(body),
            "content": body,
            "source_updated_ts": source_updated_ts,
        }
        return episode, body

    async def _rebuild_episodic_from_memos(
        self,
        force: bool = False,
        memos: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._episodes is None or self._memos is None:
            return {"ok": 0, "skipped": 0, "failed": 0, "reason": "episodic store unavailable"}
        if memos is None:
            memos = await self._retry(
                lambda: self._memos.list_all_memos(page_size=200),
                "episode_migrate_list",
            )
        pending: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
        skipped = 0
        for memo in memos:
            parsed = self._legacy_episode_payload(memo)
            if not parsed:
                continue
            episode, body = parsed
            memo_name = str(memo.get("name") or "")
            content_hash = self._content_hash(body)
            existing = self._episodes.get_episode(memo_name)
            if (
                not force
                and existing
                and str(existing.get("diary_content_hash") or "") == content_hash
                and bool(existing.get("embedding_ready"))
            ):
                skipped += 1
                continue
            card_text = self._episode_card_text(episode) or body[:1200]
            pending.append((memo, episode, body, card_text))
        ok = 0
        failed = 0
        for start in range(0, len(pending), 32):
            batch = pending[start:start + 32]
            try:
                embeddings = await self._embed_batch([item[3] for item in batch])
            except Exception as exc:
                logger.warning("[memos-memory][episode] 旧记忆卡片 embedding 失败: %s", exc)
                failed += len(batch)
                continue
            if len(embeddings) != len(batch):
                failed += len(batch)
                continue
            for (memo, episode, body, card_text), embedding in zip(batch, embeddings):
                try:
                    memo_name = str(memo.get("name") or "")
                    _, source_updated_ts = memo_source_times(memo)
                    existing = self._episodes.get_episode(memo_name)
                    content_hash = self._content_hash(body)
                    # Never downgrade an unchanged source-grounded episode merely
                    # because a background Memos scan sees its readable diary.
                    if (
                        existing
                        and existing.get("evidence_quality") == "source_grounded"
                        and str(existing.get("diary_content_hash") or "") == content_hash
                        and not force
                        and bool(existing.get("embedding_ready"))
                    ):
                        skipped += 1
                        continue
                    quality = "diary_derived"
                    legacy = True
                    source_batch_id = ""
                    source_kind = "legacy_memos"
                    if existing and existing.get("evidence_quality") in {"source_grounded", "mixed_user_edited"}:
                        original_evidence = self._episodes.evidence_for_memo(memo_name, "", limit=100)
                        unchanged = str(existing.get("diary_content_hash") or "") == content_hash
                        if unchanged:
                            episode.update({
                                key: existing.get(key)
                                for key in (
                                    "occurred_at", "event_ts", "time_basis", "memory_type", "importance",
                                    "scene_anchor", "retrieval_key", "state_change", "long_effect",
                                    "trigger_hint", "entities", "affect_before", "affect_after", "unresolved",
                                )
                                if existing.get(key) not in (None, "")
                            })
                            episode["evidence"] = original_evidence
                            quality = str(existing.get("evidence_quality") or "source_grounded")
                            card_text = str(existing.get("card_text") or card_text)
                        else:
                            episode["evidence"] = original_evidence + list(episode.get("evidence") or [])
                            quality = "mixed_user_edited"
                        legacy = False
                        source_batch_id = str(existing.get("source_batch_id") or "")
                        source_kind = str(existing.get("source_kind") or "source_grounded") if unchanged else "memos_user_edit"
                    self._episodes.upsert_episode(
                        memo_name=memo_name,
                        episode=episode,
                        card_text=card_text,
                        embedding=embedding,
                        source_batch_id=source_batch_id,
                        source_kind=source_kind,
                        legacy=legacy,
                        evidence_quality=quality,
                        diary_content_hash=content_hash,
                        source_updated_ts=source_updated_ts,
                    )
                    ok += 1
                except Exception as exc:
                    failed += 1
                    logger.debug("[memos-memory][episode] migrate %s failed: %s", memo.get("name"), exc)
        live_names = {str(memo.get("name") or "") for memo in memos if memo.get("name")}
        removed = 0
        for memo_name in self._episodes.memo_names() - live_names:
            if memo_name.startswith("memos/"):
                removed += self._episodes.delete_by_memo_name(memo_name)
        if removed:
            self._schedule_semantic_state_full_rebuild("memos_source_delete")
        stats = self._episodes.stats()
        result = {"ok": ok, "skipped": skipped, "failed": failed, "removed": removed, **stats}
        self._episode_migration_ready = failed == 0
        self._episode_migration_state = {
            "status": "ready" if failed == 0 else "partial",
            "updated_ts": time.time(),
            **result,
        }
        self._log_event("sync", f"情景记忆迁移: 新建/更新{ok} 跳过{skipped} 失败{failed}", result)
        return result

    async def _wait_episode_migration(self, timeout: float | None = None) -> bool:
        """Bounded wait for a running legacy-episode migration.

        v4.5.0 (from the 4.4 incident report): the first request after a
        reload used to start recall before the bridge was marked ready and
        fell back to the legacy multi-query chain. Waiting is shielded so a
        timeout never cancels the background migration; failure is open.
        Returns True when episodes are ready after the wait.
        """
        if self._episode_migration_ready:
            return True
        task = getattr(self, "_episode_migration_task", None)
        if task is None or task.done():
            return self._episode_migration_ready
        wait_seconds = (
            float(self.episode_migration_wait_seconds)
            if timeout is None else max(0.0, float(timeout))
        )
        if wait_seconds <= 0:
            return self._episode_migration_ready
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except asyncio.TimeoutError:
            self._log_event("recall", "episode migration still running, continuing without it", {
                "waited_seconds": wait_seconds,
            })
        except Exception as exc:
            logger.debug("[memos-memory][episode] migration wait failed open: %s", exc)
        return self._episode_migration_ready

    async def _auto_migrate_legacy_episodes(self) -> None:
        try:
            await asyncio.sleep(3)
            self._episode_migration_state = {"status": "running", "started_ts": time.time()}
            result = await self._rebuild_episodic_from_memos(force=False)
            logger.info(
                "[memos-memory][episode] legacy bridge ready: episodes=%s grounded=%s derived=%s updated=%s",
                result.get("episodes", 0), result.get("source_grounded", 0),
                result.get("diary_derived", 0), result.get("ok", 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._episode_migration_ready = False
            self._episode_migration_state = {
                "status": "failed", "updated_ts": time.time(), "error": str(exc)[:500],
            }
            logger.warning("[memos-memory][episode] automatic legacy migration failed open: %s", exc)

    async def _auto_migrate_passage_vectors(self) -> None:
        """Re-embed legacy chunks as local passages without touching Memos source."""
        try:
            await asyncio.sleep(4)
            episode_task = getattr(self, "_episode_migration_task", None)
            if episode_task is not None and not episode_task.done():
                try:
                    await episode_task
                except Exception:
                    pass
            if self._vec is None or self._emb_provider is None:
                self._passage_vector_migration_state = {"status": "unavailable"}
                return
            strategy = self._vec.get_meta_value("passage_embedding_strategy")
            if strategy == _PASSAGE_VECTOR_STRATEGY:
                self._passage_vector_migration_state = {
                    "status": "ready", "strategy": strategy, "updated": 0,
                }
                return
            stored = self._vec.get_stored_meta() or {}
            stored_model = str(stored.get("emb_model_id") or "")
            if stored_model and stored_model != str(self._emb_model_id or ""):
                self._passage_vector_migration_state = {
                    "status": "model_mismatch", "strategy": strategy or "legacy_mixed",
                    "stored_model": stored_model, "current_model": self._emb_model_id,
                }
                return
            self._passage_vector_migration_state = {
                "status": "running", "strategy": _PASSAGE_VECTOR_STRATEGY,
                "updated": 0, "started_ts": time.time(),
            }
            cursor = 0
            updated = 0
            while True:
                rows = self._vec.passage_embedding_rows(cursor, limit=48)
                if not rows:
                    break
                texts = [" ".join(str(row.get("chunk_text") or "").split()).strip() for row in rows]
                vectors = await self._embed_batch(texts)
                if len(vectors) != len(rows):
                    raise RuntimeError(f"passage embedding count mismatch: {len(vectors)}/{len(rows)}")
                changed = self._vec.replace_chunk_embeddings([
                    (int(row["id"]), vector) for row, vector in zip(rows, vectors)
                ])
                updated += changed
                cursor = int(rows[-1]["id"])
                self._passage_vector_migration_state.update({
                    "updated": updated, "last_id": cursor, "updated_ts": time.time(),
                })
                if updated and updated % 240 == 0:
                    self._log_event(
                        "sync", f"纯段落向量迁移: {updated} 段",
                        dict(self._passage_vector_migration_state),
                    )
                await asyncio.sleep(0)
            self._vec.set_meta_value("passage_embedding_strategy", _PASSAGE_VECTOR_STRATEGY)
            self._passage_vector_migration_state = {
                "status": "ready", "strategy": _PASSAGE_VECTOR_STRATEGY,
                "updated": updated, "updated_ts": time.time(),
            }
            self._log_event("sync", f"纯段落向量迁移完成: {updated} 段", self._passage_vector_migration_state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._passage_vector_migration_state = {
                "status": "failed", "strategy": _PASSAGE_VECTOR_STRATEGY,
                "error": str(exc)[:500], "updated_ts": time.time(),
            }
            logger.warning("[memos-memory][recall] passage vector migration failed open: %s", exc)

    @staticmethod
    def _source_turn_embedding_text(row: dict[str, Any]) -> str:
        role = str(row.get("role") or "").strip().lower()
        role_label = "用户" if role == "user" else ("角色" if role == "assistant" else role)
        content = " ".join(str(row.get("content") or "").split()).strip()
        return ((role_label + ": ") if role_label else "") + content

    @staticmethod
    def _chunk_text(text: str, max_chars: int) -> list[str]:
        """Split an embedding view while the source turn itself remains lossless."""
        source = str(text or "").strip()
        size = max(1, int(max_chars or 1))
        if not source:
            return []
        return [source[position:position + size] for position in range(0, len(source), size)]

    async def _index_source_batch_turns(self, batch_id: str) -> int:
        if self._episodes is None or not batch_id:
            return 0
        rows = self._episodes.source_turn_embedding_rows(
            batch_id=batch_id, limit=500, missing_only=True,
        )
        chunk_chars = max(200, int(getattr(self, "raw_archive_index_chunk_chars", 800) or 800))
        turn_rows: list[tuple[int, str]] = []
        for row in rows:
            row_id = int(row["id"])
            text = self._source_turn_embedding_text(row)
            # Every source turn stays in SourceArchive. Long turns additionally
            # get bounded child rows; search uses those without truncating source.
            turn_rows.append((row_id, text))
            if len(text) > chunk_chars and not self._episodes.turn_chunks_for_turn(row_id):
                self._episodes.add_turn_chunks(row_id, self._chunk_text(text, chunk_chars))

        indexed = 0
        if turn_rows:
            vectors = await self._embed_batch([text for _row_id, text in turn_rows])
            if len(vectors) != len(turn_rows):
                raise RuntimeError(
                    f"source turn embedding count mismatch: {len(vectors)}/{len(turn_rows)}"
                )
            indexed += self._episodes.replace_source_turn_embeddings([
                (row_id, vector) for (row_id, _text), vector in zip(turn_rows, vectors)
            ])

        chunk_rows = self._episodes.source_turn_chunk_rows(
            after_id=0, limit=max(500, len(rows) * 16), missing_only=True,
        )
        if chunk_rows:
            chunk_vectors = await self._embed_batch([
                str(row.get("chunk_text") or "") for row in chunk_rows
            ])
            if len(chunk_vectors) != len(chunk_rows):
                raise RuntimeError(
                    f"source chunk embedding count mismatch: {len(chunk_vectors)}/{len(chunk_rows)}"
                )
            indexed += self._episodes.replace_source_chunk_embeddings([
                (int(row["id"]), vector) for row, vector in zip(chunk_rows, chunk_vectors)
            ])
        return indexed

    async def _auto_migrate_source_turn_vectors(self) -> None:
        """Build a rebuildable first-hand evidence index without touching Memos."""
        try:
            await asyncio.sleep(6)
            passage_task = getattr(self, "_passage_vector_migration_task", None)
            if passage_task is not None and not passage_task.done():
                try:
                    await passage_task
                except Exception:
                    pass
            if self._episodes is None or self._emb_provider is None:
                self._source_turn_vector_migration_state = {"status": "unavailable"}
                return
            runtime_gen = self._episodes._gen.runtime_generation(
                self._emb_model_id or "unknown", int(self._emb_dim or 0)
            )
            self._source_turn_vector_migration_state = {
                "status": "running", "updated": 0, "generation": runtime_gen,
                "started_ts": time.time(),
            }
            updated = 0
            chunk_chars = max(200, int(self.raw_archive_index_chunk_chars or 800))

            # 1) full source turns for the runtime generation
            cursor = 0
            while True:
                rows = self._episodes.source_turn_embedding_rows(
                    after_id=cursor, limit=64, missing_only=True,
                    target_generation=runtime_gen,
                )
                if not rows:
                    break
                for row in rows:
                    text = self._source_turn_embedding_text(row)
                    row_id = int(row["id"])
                    if len(text) > chunk_chars and not self._episodes.turn_chunks_for_turn(row_id):
                        self._episodes.add_turn_chunks(row_id, self._chunk_text(text, chunk_chars))
                vectors = await self._embed_batch([
                    self._source_turn_embedding_text(row) for row in rows
                ])
                if len(vectors) != len(rows):
                    raise RuntimeError(
                        f"source turn embedding count mismatch: {len(vectors)}/{len(rows)}"
                    )
                updated += self._episodes.replace_source_turn_embeddings(
                    [(int(row["id"]), vector) for row, vector in zip(rows, vectors)],
                    runtime_gen=runtime_gen,
                )
                cursor = int(rows[-1]["id"])
                self._source_turn_vector_migration_state.update({
                    "updated": updated, "last_id": cursor, "updated_ts": time.time(),
                })
                await asyncio.sleep(0)

            # 2) child chunks, rolled up to their full turn at search time
            cursor = 0
            while True:
                rows = self._episodes.source_turn_chunk_rows(
                    after_id=cursor, limit=64, missing_only=True,
                    target_generation=runtime_gen,
                )
                if not rows:
                    break
                vectors = await self._embed_batch([str(row.get("chunk_text") or "") for row in rows])
                updated += self._episodes.replace_source_chunk_embeddings(
                    [(int(row["id"]), vector) for row, vector in zip(rows, vectors)],
                    runtime_gen=runtime_gen,
                )
                cursor = int(rows[-1]["id"])
                await asyncio.sleep(0)

            # 3) episode/card embeddings for the same runtime generation
            cursor = 0
            while True:
                rows = self._episodes.episode_card_embedding_rows(
                    after_id=cursor, limit=64, target_generation=runtime_gen,
                )
                if not rows:
                    break
                vectors = await self._embed_batch([str(row.get("card_text") or "") for row in rows])
                updated += self._episodes.replace_episode_card_embeddings(
                    [(int(row["id"]), vector) for row, vector in zip(rows, vectors)],
                    runtime_gen=runtime_gen,
                )
                cursor = int(rows[-1]["id"])
                await asyncio.sleep(0)

            stats = self._episodes.stats()
            self._source_turn_vector_migration_state = {
                "status": "ready", "updated": updated, "generation": runtime_gen,
                "indexed": int(stats.get("source_turn_vectors") or 0),
                "total": int(stats.get("source_turns") or 0),
                "updated_ts": time.time(),
            }
            self._log_event(
                "sync", f"原始轮次索引完成: {updated} 条",
                dict(self._source_turn_vector_migration_state),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._source_turn_vector_migration_state = {
                "status": "failed", "error": str(exc)[:500], "updated_ts": time.time(),
            }
            logger.warning("[memos-memory][recall] source turn migration failed open: %s", exc)

    def _semantic_state_scope_id(self) -> str:
        name = " ".join(str(self.character_name or "default").split()).strip() or "default"
        return "character:" + name

    def _semantic_state_pending_preview(self) -> dict[str, Any]:
        """Explain the next adaptive state decision without calling an LLM or mutating data."""
        if self._episodes is None:
            return {"available": False, "reason": "store_unavailable"}
        limit = int(getattr(self, "semantic_state_merge_max_batches", 6))
        pending = self._episodes.pending_state_updates(limit=limit)
        if not pending:
            return {"available": True, "pending_batches": 0, "reason": "empty"}
        episodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        batch_ids: list[str] = []
        missing_batches: list[str] = []
        for item in pending:
            batch_id = str(item.get("batch_id") or "")
            batch_ids.append(batch_id)
            batch_episodes = self._episodes.episodes_for_batch(batch_id)
            if not batch_episodes:
                missing_batches.append(batch_id)
                continue
            for episode in batch_episodes:
                key = str(episode.get("episode_id") or episode.get("memo_name") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                episodes.append(episode)
        if missing_batches:
            return {
                "available": True,
                "pending_batches": len(pending),
                "batch_ids": batch_ids,
                "missing_episode_batches": missing_batches,
                "reason": "waiting_for_episode_view",
            }
        return {
            "available": True,
            "batch_ids": batch_ids,
            "episode_count": len(episodes),
            **self._semantic_state_update_decision(pending, episodes),
        }

    def _semantic_state_status(
        self,
        include_history: bool = False,
        include_pending_preview: bool = False,
    ) -> dict[str, Any]:
        if not self.semantic_state_enable:
            return {"enabled": False, "ready": False, "reason": "disabled"}
        if self._episodes is None:
            return {"enabled": True, "ready": False, "reason": "episodic store unavailable"}
        scope_id = self._semantic_state_scope_id()
        state = self._episodes.get_semantic_state(scope_id)
        data = {
            "enabled": True,
            "ready": bool(state and str(state.get("rendered_text") or "").strip()),
            "scope_id": scope_id,
            "state": state or {},
            "target_chars": self.semantic_state_target_chars,
            "replaces_profile": self.semantic_state_replace_profile,
            "pending": int(self._episodes.stats().get("pending_state_updates") or 0),
            "update_policy": getattr(self, "semantic_state_update_policy", "adaptive"),
            "batch_threshold": getattr(self, "semantic_state_batch_threshold", 3),
            "max_wait_hours": getattr(self, "semantic_state_max_wait_hours", 72.0),
            "significance_threshold": getattr(
                self, "semantic_state_significance_threshold", 0.72
            ),
        }
        if include_history:
            data["history"] = self._episodes.semantic_state_history(scope_id, limit=60)
        if include_pending_preview:
            data["pending_preview"] = self._semantic_state_pending_preview()
        return data

    @staticmethod
    def _parse_semantic_state_response(text: str) -> dict[str, str]:
        source = str(text or "").strip()
        if source.startswith("```"):
            source = re.sub(r"^```(?:json)?\s*|\s*```$", "", source, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            raw = json.loads(source)
        except json.JSONDecodeError:
            start, end = source.find("{"), source.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                raw = json.loads(source[start:end + 1])
            except json.JSONDecodeError:
                return {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for key in (
            "relationship_position", "commitments_boundaries", "behavior_tendencies",
            "emotional_baseline", "open_loops",
        ):
            value = raw.get(key, "")
            if isinstance(value, list):
                value = "\n".join("- " + str(item).strip() for item in value if str(item).strip())
            out[key] = "\n".join(line.rstrip() for line in str(value or "").strip().splitlines()).strip()
        if not any(out.values()):
            return {}
        return out

    def _semantic_state_seed(self) -> dict[str, Any]:
        if self._episodes is not None:
            current = self._episodes.get_semantic_state(self._semantic_state_scope_id())
            if current:
                return current
        return self._semantic_state_profile_seed()

    def _semantic_state_profile_seed(self) -> dict[str, Any]:
        profile = self._affiliate_profile_status() if self.enable_affiliate_profile else {}
        if profile.get("connected"):
            seed = "\n".join(
                value for value in [
                    str(profile.get("profile") or "").strip(),
                    str(profile.get("profile_facts") or "").strip(),
                ] if value
            )
            if seed:
                return {"rendered_text": "[旧画像，仅作为首次状态融合的种子]\n" + seed}
        return {}

    @staticmethod
    def _semantic_state_change_score(
        episodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Estimate whether new episodes should rewrite the durable current state.

        This gate is deliberately deterministic: an extra LLM call just to decide
        whether another LLM call is needed would add cost and another failure mode.
        It never drops an episode; ordinary changes remain queued for batch fusion.
        """
        type_weight = {
            "promise_or_rule": 0.48,
            "relationship_shift": 0.42,
            "emotional_anchor": 0.28,
            "behavior_bias": 0.24,
            "plot_fact": 0.10,
            "daily_texture": 0.03,
        }
        hard_patterns = {
            "commitment": (
                "承诺", "答应", "约定", "誓言", "永远", "不会离开", "说定了",
                "promise", "commitment",
            ),
            "boundary": (
                "边界", "底线", "禁忌", "禁止", "不许", "拒绝", "分手", "结束关系",
                "boundary", "break up",
            ),
            "identity": (
                "真实身份", "真名", "身世", "身份揭示", "坦白身份", "认出",
                "identity", "real name",
            ),
            "relationship": (
                "告白", "确认关系", "成为恋人", "结婚", "重逢", "和好", "决裂",
                "背叛", "原谅", "彻底离开", "relationship shift",
            ),
        }
        hard_type_gate = {
            "commitment": {"promise_or_rule"},
            "boundary": {"promise_or_rule", "relationship_shift"},
            "identity": {"plot_fact", "relationship_shift"},
            "relationship": {"relationship_shift"},
        }
        best = 0.0
        reasons: set[str] = set()
        hard = False
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            memory_type = str(episode.get("memory_type") or "plot_fact").strip()
            score = float(type_weight.get(memory_type, 0.08))
            if memory_type in {"promise_or_rule", "relationship_shift"}:
                reasons.add(memory_type)
            try:
                importance = max(1, min(5, int(episode.get("importance") or 3)))
            except (TypeError, ValueError):
                importance = 3
            if importance >= 5:
                score += 0.24
                reasons.add("importance_5")
            elif importance >= 4:
                score += 0.13
                reasons.add("importance_4")
            state_change = str(episode.get("state_change") or "").strip()
            long_effect = str(episode.get("long_effect") or "").strip()
            unresolved = episode.get("unresolved") or []
            if state_change:
                score += 0.14
                reasons.add("state_change")
            if long_effect:
                score += 0.08
                reasons.add("long_effect")
            if unresolved:
                score += 0.10
                reasons.add("unresolved")
            evidence = [
                item for item in (episode.get("evidence") or [])
                if isinstance(item, dict) and str(item.get("tier") or "supporting") == "must_write"
            ]
            if evidence:
                score += min(0.12, len(evidence) * 0.03)
                reasons.add("must_write")
            decisive_text = " ".join(
                [
                    state_change,
                    long_effect,
                    str(episode.get("retrieval_key") or ""),
                    str(episode.get("scene_anchor") or ""),
                ]
                + [
                    " ".join(
                        str(item.get(key) or "")
                        for key in ("detail", "quote", "quote_text")
                    )
                    for item in evidence
                ]
            ).lower()
            for category, patterns in hard_patterns.items():
                type_matches = memory_type in hard_type_gate.get(category, set())
                # Episode classification is not infallible. A high-importance
                # emotional anchor may still contain a decisive commitment or
                # relationship turn, but low-importance daily texture must not
                # force a state rewrite merely because it says "约定" casually.
                if memory_type == "emotional_anchor" and importance >= 4:
                    type_matches = category in {"commitment", "boundary", "relationship"}
                if type_matches and any(
                    pattern.lower() in decisive_text for pattern in patterns
                ):
                    hard = True
                    score = max(score, 0.96)
                    reasons.add("hard_" + category)
            best = max(best, min(1.0, score))
        return {
            "score": round(best, 4),
            "hard": hard,
            "reasons": sorted(reasons),
            "episodes": len(episodes),
        }

    def _semantic_state_update_decision(
        self,
        pending: list[dict[str, Any]],
        episodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        policy = getattr(self, "semantic_state_update_policy", "adaptive")
        signal = self._semantic_state_change_score(episodes)
        now = time.time()
        oldest_ts = min(
            (float(item.get("created_ts") or now) for item in pending),
            default=now,
        )
        age_hours = max(0.0, (now - oldest_ts) / 3600.0)
        failed = any(str(item.get("status") or "") == "failed" for item in pending)
        state_ready = bool(
            self._episodes
            and self._episodes.get_semantic_state(self._semantic_state_scope_id())
        )
        reason = "deferred"
        should_update = False
        if policy == "every_batch":
            should_update, reason = True, "every_batch"
        elif not state_ready:
            should_update, reason = True, "initial_state"
        elif failed:
            should_update, reason = True, "failed_retry"
        elif bool(signal.get("hard")) or float(signal.get("score") or 0.0) >= float(
            getattr(self, "semantic_state_significance_threshold", 0.72)
        ):
            should_update, reason = True, "significant_change"
        elif len(pending) >= int(getattr(self, "semantic_state_batch_threshold", 3)):
            should_update, reason = True, "batch_threshold"
        elif age_hours >= float(getattr(self, "semantic_state_max_wait_hours", 72.0)):
            should_update, reason = True, "max_wait"
        return {
            "update": should_update,
            "reason": reason,
            "pending_batches": len(pending),
            "oldest_hours": round(age_hours, 2),
            "signal": signal,
            "policy": policy,
        }

    async def _update_semantic_state(
        self,
        episodes: list[dict[str, Any]],
        *,
        source_batch_id: str = "",
        reason: str = "compression",
        seed_override: dict[str, Any] | None = None,
        queue_batch_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._semantic_state_lock:
            return await self._update_semantic_state_locked(
                episodes,
                source_batch_id=source_batch_id,
                reason=reason,
                seed_override=seed_override,
                queue_batch_ids=queue_batch_ids,
            )

    async def _update_semantic_state_locked(
        self,
        episodes: list[dict[str, Any]],
        *,
        source_batch_id: str = "",
        reason: str = "compression",
        seed_override: dict[str, Any] | None = None,
        queue_batch_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.semantic_state_enable or self._episodes is None or not episodes:
            return {"updated": False, "reason": "disabled_or_empty"}
        scope_id = self._semantic_state_scope_id()
        episode_ids = [
            str(item.get("episode_id") or item.get("episode_key") or "")
            for item in episodes if isinstance(item, dict)
        ]
        episode_ids = [value for value in episode_ids if value]
        queue_batch_ids = [str(value) for value in (queue_batch_ids or []) if str(value)]
        if source_batch_id and not queue_batch_ids:
            self._episodes.enqueue_state_update(source_batch_id, scope_id, episode_ids)
        current = self._semantic_state_seed() if seed_override is None else seed_override
        prompt = build_semantic_state_update_prompt(
            self.character_name,
            current,
            episodes,
            self.semantic_state_target_chars,
        )
        try:
            response = await self._call_memory_generation_llm(
                prompt,
                provider_id=self.semantic_state_provider_id,
                timeout=self.semantic_state_timeout,
                label="semantic_state",
            )
            state = self._parse_semantic_state_response(response)
            if not state:
                raise ValueError("semantic state returned invalid JSON")
            rendered = EpisodicStore._render_semantic_state(state)
            if len(rendered) > int(self.semantic_state_target_chars * 1.35):
                retry = await self._call_memory_generation_llm(
                    prompt
                    + "\n\n【体积修正】首轮状态为 " + str(len(rendered)) + " 字，明显超过目标 "
                    + str(self.semantic_state_target_chars)
                    + " 字。请合并同义项、删除事件复述并重新输出完整 JSON；承诺、边界和未解决事项不得丢失。",
                    provider_id=self.semantic_state_provider_id,
                    timeout=self.semantic_state_timeout,
                    label="semantic_state_compact_retry",
                )
                retry_state = self._parse_semantic_state_response(retry)
                if retry_state:
                    retry_rendered = EpisodicStore._render_semantic_state(retry_state)
                    if len(retry_rendered) < len(rendered):
                        state, rendered = retry_state, retry_rendered
            if len(rendered) > int(self.semantic_state_target_chars * 1.6):
                raise ValueError(f"semantic state remains oversized: {len(rendered)} chars")
            saved = self._episodes.upsert_semantic_state(
                scope_id,
                state,
                reason=reason,
                source_batch_id=source_batch_id,
                source_episode_ids=episode_ids,
            )
            if queue_batch_ids:
                self._episodes.mark_state_updates(queue_batch_ids, "done")
            elif source_batch_id:
                self._episodes.mark_state_update(source_batch_id, "done")
            self._log_event("state", f"滚动状态更新 v{saved.get('version')}", {
                "scope": scope_id, "chars": len(saved.get("rendered_text") or ""),
                "episodes": len(episodes), "reason": reason,
                "batches": len(queue_batch_ids) if queue_batch_ids else (1 if source_batch_id else 0),
            })
            logger.info(
                "[memos-memory][state] updated v%s chars=%s episodes=%s reason=%s",
                saved.get("version"), len(saved.get("rendered_text") or ""), len(episodes), reason,
            )
            return {"updated": True, **saved}
        except Exception as exc:
            if queue_batch_ids:
                self._episodes.mark_state_updates(queue_batch_ids, "failed", str(exc))
            elif source_batch_id:
                self._episodes.mark_state_update(source_batch_id, "failed", str(exc))
            self._log_event("state", "滚动状态更新失败，已排队重试", {
                "batch_id": source_batch_id, "error": str(exc)[:300],
            })
            logger.warning("[memos-memory][state] update failed, queued for retry: %s", exc)
            return {"updated": False, "reason": str(exc)}

    def _schedule_semantic_state_update(
        self,
        episodes: list[dict[str, Any]],
        *,
        source_batch_id: str,
        reason: str,
    ) -> None:
        if not self.semantic_state_enable or self._episodes is None or not episodes:
            return
        episode_ids = [
            str(item.get("episode_id") or item.get("episode_key") or "")
            for item in episodes if isinstance(item, dict)
        ]
        if source_batch_id:
            self._episodes.enqueue_state_update(
                source_batch_id,
                self._semantic_state_scope_id(),
                [value for value in episode_ids if value],
            )
            if any(not item.done() for item in self._semantic_state_pending_tasks):
                return
            coro = self._drain_semantic_state_queue()
        else:
            coro = self._update_semantic_state(
                episodes, source_batch_id="", reason=reason,
            )
        task = asyncio.create_task(coro)
        self._semantic_state_pending_tasks.add(task)
        task.add_done_callback(self._semantic_state_pending_tasks.discard)

    async def _drain_semantic_state_queue(self, limit: int = 20) -> dict[str, Any]:
        """Coalesce queued deltas without delaying decisive relationship changes."""
        if self._episodes is None:
            return {"updated": 0, "reason": "store_unavailable"}
        merge_limit = min(
            max(1, min(100, int(limit))),
            int(getattr(self, "semantic_state_merge_max_batches", 6)),
        )
        pending = self._episodes.pending_state_updates(limit=merge_limit)
        if not pending:
            return {"updated": 0, "reason": "empty"}
        batch_ids: list[str] = []
        episodes: list[dict[str, Any]] = []
        seen_episode_ids: set[str] = set()
        for item in pending:
            batch_id = str(item.get("batch_id") or "")
            batch_episodes = self._episodes.episodes_for_batch(batch_id)
            if not batch_episodes:
                return {
                    "updated": 0,
                    "reason": "waiting_for_episode_view",
                    "batch_id": batch_id,
                }
            batch_ids.append(batch_id)
            for episode in batch_episodes:
                key = str(episode.get("episode_id") or episode.get("memo_name") or "")
                if key and key in seen_episode_ids:
                    continue
                if key:
                    seen_episode_ids.add(key)
                episodes.append(episode)
        decision = self._semantic_state_update_decision(pending, episodes)
        if not decision.get("update"):
            defer_key = ":".join(
                [batch_ids[-1], str(decision.get("pending_batches")), str(decision.get("reason"))]
            )
            if defer_key != getattr(self, "_semantic_state_last_defer_key", ""):
                self._semantic_state_last_defer_key = defer_key
                self._log_event("state", "普通变化已累计，暂不重写滚动状态", decision)
                logger.info(
                    "[memos-memory][state] deferred batches=%s signal=%.2f oldest=%.1fh",
                    decision.get("pending_batches"),
                    float((decision.get("signal") or {}).get("score") or 0.0),
                    float(decision.get("oldest_hours") or 0.0),
                )
            return {"updated": 0, **decision}
        self._semantic_state_last_defer_key = ""
        reason = "adaptive_" + str(decision.get("reason") or "queued")
        result = await self._update_semantic_state(
            episodes,
            source_batch_id=batch_ids[-1],
            reason=reason,
            queue_batch_ids=batch_ids,
        )
        if not result.get("updated"):
            return {
                "updated": 0,
                "reason": result.get("reason") or "update_failed",
                "batch_ids": batch_ids,
                "decision": decision,
            }
        return {
            "updated": 1,
            "merged_batches": len(batch_ids),
            "episodes": len(episodes),
            "decision": decision,
        }

    async def _bootstrap_semantic_state(
        self,
        force: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        if self._episodes is None:
            return {"updated": False, "reason": "store_unavailable"}
        if self._episodes.get_semantic_state(self._semantic_state_scope_id()) and not force:
            return {"updated": False, "reason": "already_ready"}
        limit = self.semantic_state_bootstrap_episode_limit
        recent = self._episodes.list_episodes(limit=min(1000, max(limit * 3, limit)))
        latest = recent[:max(10, int(limit * 0.72))]
        important = [item for item in recent if int(item.get("importance") or 3) >= 4]
        chosen: dict[str, dict[str, Any]] = {}
        for item in important + latest:
            chosen[str(item.get("episode_id") or item.get("memo_name") or "")] = item
            if len(chosen) >= limit:
                break
        episodes = sorted(chosen.values(), key=lambda item: float(item.get("event_ts") or 0))
        if not episodes:
            if force:
                async with self._semantic_state_lock:
                    superseded = self._episodes.supersede_pending_state_updates(
                        reason or "semantic_state_full_rebuild_empty"
                    )
                    cleared = self._episodes.clear_semantic_state(self._semantic_state_scope_id())
                return {
                    "updated": False, "reason": "no_episodes", "cleared": cleared,
                    "superseded_updates": superseded,
                }
            return {"updated": False, "reason": "no_episodes"}
        if force:
            async with self._semantic_state_lock:
                superseded = self._episodes.supersede_pending_state_updates(
                    reason or "semantic_state_full_rebuild"
                )
                result = await self._update_semantic_state_locked(
                    episodes,
                    reason=reason or "manual_rebuild",
                    seed_override=self._semantic_state_profile_seed(),
                )
                result["superseded_updates"] = superseded
                return result
        return await self._update_semantic_state(
            episodes,
            reason=reason or "4.0_data_bootstrap",
        )

    def _schedule_semantic_state_full_rebuild(self, reason: str) -> None:
        if not getattr(self, "semantic_state_enable", False) or self._episodes is None:
            return
        task = asyncio.create_task(self._bootstrap_semantic_state(force=True, reason=reason))
        self._semantic_state_pending_tasks.add(task)
        task.add_done_callback(self._semantic_state_pending_tasks.discard)

    async def _semantic_state_maintenance_loop(self) -> None:
        await asyncio.sleep(8)
        while True:
            try:
                if self._episodes is None:
                    return
                if self.episodic_auto_migrate and not self._episode_migration_ready:
                    await asyncio.sleep(10)
                    continue
                queue_limit = int(getattr(self, "semantic_state_merge_max_batches", 6))
                pending = self._episodes.pending_state_updates(limit=queue_limit)
                if pending:
                    await self._drain_semantic_state_queue(limit=queue_limit)
                elif self.semantic_state_auto_bootstrap:
                    await self._bootstrap_semantic_state()
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[memos-memory][state] maintenance failed: %s", exc)
                await asyncio.sleep(60)

    async def reconcile_once(self) -> int:
        """执行一次对账:vec 有、memos 没有的 memo_name → 从 vec 删掉。返回删除的日记数。"""
        if self._memos is None or self._vec is None:
            return 0
        try:
            memos_list = await self._retry(lambda: self._memos.list_all_memos(page_size=200), "reconcile_list")
        except Exception as e:
            logger.warning("[memos-memory] 对账拉取 memos 失败: %s", e)
            return 0
        live_names = {m.get("name", "") for m in memos_list if m.get("name")}
        vec_names = self._vec.all_memo_names()
        # vec 里有、但 memos 已删的(排除本地 local/ 前缀的,那些是 memos 写入失败的降级项)
        stale = {n for n in vec_names if n.startswith("memos/") and n not in live_names}
        deleted = 0
        for name in stale:
            deleted += 1 if self._vec.delete_by_memo_name(name) else 0
            if self._episodes is not None:
                self._episodes.delete_by_memo_name(name)
        self._last_reconcile_ts = time.time()
        self._reconcile_stats["last_run_deleted"] = deleted
        self._reconcile_stats["deleted"] += deleted
        if deleted:
            logger.info("[memos-memory] 对账完成:从 vec 清除 %d 篇已被 memos 删除的日记", deleted)
            self._log_event("reconcile", f"后台对账: 清理{deleted}篇过期日记", {"deleted": deleted})
            self._schedule_semantic_state_full_rebuild("background_reconcile_delete")
        else:
            logger.debug("[memos-memory] 对账完成:无需清除(vec 与 memos 一致)")
        return deleted


    def _emb_cache_get(self, key: str) -> list[float] | None:
        v = self._emb_cache.get(key)
        if v is not None:
            self._emb_cache.move_to_end(key)  # LRU: 命中挪到末尾
        return v

    def _emb_cache_put(self, key: str, vec: list[float]) -> None:
        if self.emb_cache_size <= 0:
            return
        self._emb_cache[key] = vec
        self._emb_cache.move_to_end(key)
        while len(self._emb_cache) > self.emb_cache_size:
            self._emb_cache.popitem(last=False)  # 淘汰最久未用

    async def _embed(self, text: str, timeout: float | None = None, offload_thread: bool = False) -> list[float]:
        cached = self._emb_cache_get(text)
        if cached is not None:
            return cached
        v = await self._retry(
            lambda: self._emb_provider.get_embedding(text),
            "embed",
            timeout=timeout,
            offload_thread=offload_thread,
        )
        if self._emb_dim is None:
            self._emb_dim = len(v)
            await self._vec.ensure_dim(self._emb_dim, self._emb_model_id)
            if self._episodes is not None:
                await self._episodes.ensure_dim(self._emb_dim, self._emb_model_id or "unknown")
        self._emb_cache_put(text, v)
        return v

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 先查缓存,只对未命中的调用 provider
        result: list[list[float] | None] = [self._emb_cache_get(t) for t in texts]
        miss_idx = [i for i, r in enumerate(result) if r is None]
        if miss_idx:
            miss_texts = [texts[i] for i in miss_idx]
            if hasattr(self._emb_provider, "get_embeddings"):
                vs = await self._retry(lambda: self._emb_provider.get_embeddings(miss_texts), "embed_batch")
            else:
                vs = []
                for t in miss_texts:
                    vs.append(await self._retry(lambda t=t: self._emb_provider.get_embedding(t), "embed"))
            for j, i in enumerate(miss_idx):
                result[i] = vs[j]
                self._emb_cache_put(texts[i], vs[j])
        if self._emb_dim is None and result and result[0]:
            self._emb_dim = len(result[0])
            await self._vec.ensure_dim(self._emb_dim, self._emb_model_id)
            if self._episodes is not None:
                await self._episodes.ensure_dim(self._emb_dim, self._emb_model_id or "unknown")
        return [r for r in result if r is not None]

    @staticmethod
    def _parse_diaries(llm_text: str) -> list[dict[str, Any]]:
        text = (llm_text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            i, j = text.find("["), text.rfind("]")
            if i >= 0 and j > i:
                try:
                    data = json.loads(text[i:j + 1])
                except Exception:
                    return []
            else:
                return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            content, tags = _extract_inline_tags_from_content(content, item.get("tags") or [])
            # 2.3: retain full event date and provenance; keep legacy fields for old models.
            event_date = str(item.get("event_date", "")).strip()
            md = str(item.get("month_day", "")).strip()
            tl = str(item.get("time_label", "")).strip()
            dt = str(item.get("date_text", "")).strip()
            out.append({
                "event_date": event_date,
                "time_basis": str(item.get("time_basis", "")).strip(),
                "month_day": md,
                "time_label": tl,
                # 兼容旧格式
                "date_text": dt,
                "scene_anchor": str(item.get("scene_anchor", "")).strip(),
                "content": content,
                "memory_type": _normalize_memory_type(item.get("memory_type"), content, tags),
                "long_effect": _safe_meta_text(item.get("long_effect")),
                "trigger_hint": _safe_meta_text(item.get("trigger_hint")),
                "retrieval_key": _safe_meta_text(item.get("retrieval_key"), 260),
                "state_change": _safe_meta_text(item.get("state_change"), 260),
                "entities": [str(x).strip() for x in (item.get("entities") or []) if str(x).strip()][:20]
                    if isinstance(item.get("entities") or [], list) else [],
                "tags": tags,
                "importance": int(float(str(item.get("importance", 3)))) if str(item.get("importance", "")).replace(".", "").isdigit() else 3,
            })
        return out

    @staticmethod
    def _normalize_buffer_diary_times(
        diaries: list[dict[str, Any]],
        recorded_dates: list[str],
        labels_by_date: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        """Apply deterministic fixes only where conversation_now makes them safe."""
        allowed = set(recorded_dates)
        for diary in diaries:
            if str(diary.get("time_basis") or "") != "conversation_now":
                continue
            event_date = str(diary.get("event_date") or "").strip()
            if len(recorded_dates) == 1 and event_date not in allowed:
                event_date = recorded_dates[0]
                diary["event_date"] = event_date
            labels = labels_by_date.get(event_date, [])
            if len(labels) == 1:
                diary["time_label"] = labels[0]
        return diaries

    @staticmethod
    def _buffer_diary_time_diagnostics(
        diaries: list[dict[str, Any]], recorded_dates: list[str]
    ) -> dict[str, Any]:
        allowed = set(recorded_dates)
        covered = set()
        invalid = []
        for index, diary in enumerate(diaries):
            event_date = str(diary.get("event_date") or "").strip()
            basis = str(diary.get("time_basis") or "").strip()
            if event_date in allowed:
                covered.add(event_date)
            if basis == "conversation_now" and event_date not in allowed:
                invalid.append({"index": index, "event_date": event_date or "(empty)"})
        return {
            "covered_dates": [d for d in recorded_dates if d in covered],
            "missing_dates": [d for d in recorded_dates if d not in covered],
            "invalid_conversation_now": invalid,
        }

    async def _call_llm_compress(self, prompt: str) -> str:
        return await self._call_memory_generation_llm(
            prompt,
            provider_id=self.compress_provider_id,
            timeout=max(20.0, float(self.compress_llm_timeout or 120)),
            label="compress_llm",
        )

    async def _call_memory_generation_llm(
        self,
        prompt: str,
        *,
        provider_id: str,
        timeout: float,
        label: str,
    ) -> str:
        prov = None
        if provider_id:
            try:
                prov = self.context.get_provider_by_id(provider_id)
            except Exception as e:
                logger.warning(
                    "[memos-memory] 指定记忆生成 LLM '%s' 解析失败: %s, 回退到压缩/当前对话 LLM",
                    provider_id, e,
                )
        if prov is None and provider_id != self.compress_provider_id and self.compress_provider_id:
            try:
                prov = self.context.get_provider_by_id(self.compress_provider_id)
            except Exception:
                prov = None
        if prov is None:
            prov = self.context.get_using_provider()
        if prov is None:
            raise RuntimeError("no LLM provider")
        prov_id = _resolve_provider_name(prov)
        logger.info(
            "[memos-memory] %s LLM = %s (provider_id='%s')",
            label, prov_id, provider_id or self.compress_provider_id or "(auto)",
        )
        resp = await self._retry(
            lambda: prov.text_chat(prompt=prompt, contexts=[], system_prompt=""),
            label,
            timeout=max(20.0, float(timeout or 120)),
        )
        return getattr(resp, "completion_text", "") or ""

    @staticmethod
    def _parse_json_array(llm_text: str) -> list[dict[str, Any]]:
        text = str(llm_text or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end <= start:
                return []
            try:
                value = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return []
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

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

    def _parse_episode_blueprints(
        self,
        llm_text: str,
        messages: list[dict[str, Any]],
        max_episodes: int,
    ) -> list[dict[str, Any]]:
        raw = self._parse_json_array(llm_text)
        turns = [str(message.get("content") or "") for message in messages]
        roles = [str(message.get("role") or "") for message in messages]
        out: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for position, item in enumerate(raw[:max(1, int(max_episodes or 1))]):
            key = str(item.get("episode_key") or f"e{position + 1}").strip()[:40]
            if not key or key in seen_keys:
                continue
            evidence_out = []
            for evidence in item.get("evidence") or []:
                if not isinstance(evidence, dict):
                    continue
                indexes = []
                for value in evidence.get("turn_indexes") or []:
                    try:
                        index = int(value)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= index < len(turns) and index not in indexes:
                        indexes.append(index)
                if not indexes:
                    continue
                source = "\n".join(turns[index] for index in indexes)
                quote = " ".join(str(evidence.get("quote") or "").split())
                quote_grounded = bool(quote and any(quote in " ".join(turns[index].split()) for index in indexes))
                detail = " ".join(str(evidence.get("detail") or evidence.get("fact") or "").split())[:600]
                detail_grounded = self._evidence_overlap(detail, source) >= 0.18 if detail else False
                actor = str(evidence.get("actor") or "").strip()
                if actor == "user" and not any(roles[index] == "user" for index in indexes):
                    actor = ""
                elif actor == "assistant" and not any(roles[index] == "assistant" for index in indexes):
                    actor = ""
                if not detail and not quote_grounded:
                    continue
                try:
                    confidence = float(evidence.get("confidence") or 0.5)
                except (TypeError, ValueError):
                    confidence = 0.5
                grounded = bool(quote_grounded or detail_grounded)
                if quote and not quote_grounded:
                    quote = ""
                    confidence = min(confidence, 0.55)
                if not grounded:
                    confidence = min(confidence, 0.45)
                kind = str(evidence.get("kind") or "event").strip().lower()[:40]
                tier = str(evidence.get("tier") or "supporting").strip().lower()
                if tier not in {"must_write", "supporting", "archive_only"}:
                    tier = "supporting"
                if getattr(self, "evidence_tier_enable", True):
                    fact_text = " ".join((detail, quote)).strip()
                    commitment_words = (
                        "答应", "承诺", "约定", "发誓", "一定", "不许", "边界",
                        "底线", "拒绝", "关系", "在一起", "分开", "离开", "原谅",
                    )
                    if kind in {
                        "commitment", "promise", "boundary", "relationship-decision",
                        "relationship_decision", "relationship decision",
                    } or any(word in fact_text for word in commitment_words):
                        tier = "must_write"
                    elif (not grounded or confidence < 0.46 or kind in {
                        "greeting", "backchannel", "meta", "repetition", "generic",
                    }):
                        tier = "archive_only"
                else:
                    tier = "must_write"
                evidence_out.append({
                    "kind": kind or "event",
                    "actor": actor[:20],
                    "detail": detail or quote,
                    "quote": quote,
                    "turn_indexes": indexes,
                    "confidence": round(max(0.0, min(1.0, confidence)), 3),
                    "grounded": grounded,
                    "tier": tier,
                })
            if not evidence_out:
                continue
            if not any(bool(evidence.get("grounded")) for evidence in evidence_out):
                continue
            seen_keys.add(key)
            content_stub = " ".join(evidence["detail"] for evidence in evidence_out)
            raw_tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            out.append({
                "episode_key": key,
                "event_date": str(item.get("event_date") or "").strip(),
                "time_label": str(item.get("time_label") or "").strip(),
                "time_basis": str(item.get("time_basis") or "unknown").strip(),
                "scene_anchor": _safe_meta_text(item.get("scene_anchor"), 160),
                "scene_start_turn": (
                    item.get("scene_start_turn")
                    if isinstance(item.get("scene_start_turn"), int)
                    and not isinstance(item.get("scene_start_turn"), bool) else -1
                ),
                "scene_end_turn": (
                    item.get("scene_end_turn")
                    if isinstance(item.get("scene_end_turn"), int)
                    and not isinstance(item.get("scene_end_turn"), bool) else -1
                ),
                "scene_boundary_reasons": [
                    str(value).strip()[:80]
                    for value in (item.get("scene_boundary_reasons") or item.get("reasons") or [])
                    if str(value).strip()
                ][:8],
                "memory_type": _normalize_memory_type(item.get("memory_type"), content_stub, raw_tags),
                "evidence": evidence_out,
                "affect_before": _safe_meta_text(item.get("affect_before"), 260),
                "affect_after": _safe_meta_text(item.get("affect_after"), 260),
                "state_change": _safe_meta_text(item.get("state_change"), 320),
                "long_effect": _safe_meta_text(item.get("long_effect"), 320),
                "trigger_hint": _safe_meta_text(item.get("trigger_hint"), 320),
                "retrieval_key": _safe_meta_text(item.get("retrieval_key"), 360),
                "entities": [str(value).strip()[:80] for value in (item.get("entities") or []) if str(value).strip()][:30],
                "unresolved": [str(value).strip()[:260] for value in (item.get("unresolved") or []) if str(value).strip()][:12],
                "tags": raw_tags,
                "importance": _normalize_importance(item.get("importance"), 3),
            })
        return out

    def _diary_render_coverage(self, content: str, episode: dict[str, Any]) -> tuple[float, list[str]]:
        pipeline = getattr(self, "_diary_pipeline", None) or DiaryPipeline(self)
        return pipeline.coverage(content, episode)

    def _diary_transcript_risk(
        self, content: str, episode: dict[str, Any], raw: str,
    ) -> float:
        del episode  # kept in the compatibility signature for old callers/tests
        pipeline = getattr(self, "_diary_pipeline", None) or DiaryPipeline(self)
        report = pipeline.risk_report(content, raw)
        return float(report.risk)

    def _diary_first_person_check(
        self, content: str, episode: dict[str, Any],
    ) -> tuple[bool, str]:
        if not getattr(self, "diary_first_person_check_enable", True):
            return True, ""
        pipeline = getattr(self, "_diary_pipeline", None) or DiaryPipeline(self)
        report = pipeline.first_person_check(content, episode)
        return bool(report.passed), str(report.reason or "")

    @staticmethod
    def _grounded_episode_fallback_diary(episode: dict[str, Any]) -> str:
        return DiaryPipeline.fallback_diary(episode)

    async def _generate_evidence_first_diaries(
        self,
        messages: list[dict[str, Any]],
        messages_text: str,
        diary_count: int,
        *,
        diary_cap: int = 0,
        exact_count: bool = False,
    ) -> list[dict[str, Any]]:
        splitter = getattr(self, "_scene_splitter", None) or SceneSplitter(
            gap_seconds=float(getattr(self, "scene_split_gap_seconds", 10800) or 10800),
            max_scenes=int(getattr(self, "diary_count_max_cap", 6) or 6),
        )
        scene_enabled = bool(getattr(self, "scene_split_enable", True))
        candidates = splitter.detect(messages) if scene_enabled else []
        cap = max(1, int(diary_cap or getattr(self, "diary_count_max_cap", 6) or diary_count or 1))
        target = max(1, int(diary_count or 1))
        if scene_enabled:
            target = splitter.dynamic_diary_count(
                target, messages, candidates, cap, source_kind="eod" if exact_count else "auto",
            )
        logger.info(
            "[memory][scene] candidates=%d turns=%d target=%d cap=%d boundaries=%s",
            len(candidates), len(messages), target, cap,
            ",".join(sorted({reason for item in candidates for reason in item.reasons})) or "none",
        )
        extraction_prompt = build_episode_extraction_prompt(
            self.character_name,
            messages_text,
            target,
            exact_count=exact_count,
            timezone_name=self.rp_time_timezone,
            message_time_context=recorded_time_context(messages, self.rp_time_timezone),
            scene_candidates=as_prompt_payload(candidates),
            diary_cap=cap,
        )
        extraction_text = await self._call_memory_generation_llm(
            extraction_prompt,
            provider_id=self.episode_extraction_provider_id,
            timeout=self.episode_extraction_timeout,
            label="episode_extract",
        )
        episodes = self._parse_episode_blueprints(extraction_text, messages, cap)
        scene_report = (
            splitter.validate(episodes, messages, candidates)
            if scene_enabled else {"fixed": [], "overlaps": [], "uncovered": [], "invalid": []}
        )
        logger.info(
            "[memory][scene] validated episodes=%d fixed=%d overlaps=%d uncovered=%d invalid=%d",
            len(episodes), len(scene_report.get("fixed") or []),
            len(scene_report.get("overlaps") or []), len(scene_report.get("uncovered") or []),
            len(scene_report.get("invalid") or []),
        )
        if exact_count and 0 < len(episodes) < target:
            retry_text = await self._call_memory_generation_llm(
                extraction_prompt
                + "\n\n【覆盖修正】首轮只提取了 " + str(len(episodes))
                + " 个情景，目标是 " + str(target)
                + " 个。请重新检查候选范围、不同日期、地点、目标、关系阶段和情绪转折；"
                  "补回有来源证据的独立情景，禁止重复、虚构或切碎同一情感弧。重新输出完整 JSON 数组。",
                provider_id=self.episode_extraction_provider_id,
                timeout=self.episode_extraction_timeout,
                label="episode_extract_retry",
            )
            retry_episodes = self._parse_episode_blueprints(retry_text, messages, cap)
            if len(retry_episodes) > len(episodes):
                episodes = retry_episodes
                scene_report = splitter.validate(episodes, messages, candidates)
                logger.info(
                    "[memory][scene] retry validated episodes=%d fixed=%d overlaps=%d uncovered=%d invalid=%d",
                    len(episodes), len(scene_report.get("fixed") or []),
                    len(scene_report.get("overlaps") or []), len(scene_report.get("uncovered") or []),
                    len(scene_report.get("invalid") or []),
                )
        if not episodes:
            raise ValueError("episode extraction returned no grounded episodes")

        tier_counts: dict[str, int] = {}
        for episode in episodes:
            for item in episode.get("evidence") or []:
                tier = str(item.get("tier") or "supporting")
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
        logger.info("[memory][episode] episodes=%d tiers=%s", len(episodes), tier_counts)

        render_prompt = build_diary_render_prompt(self.character_name, messages_text, episodes)
        render_text = await self._call_memory_generation_llm(
            render_prompt,
            provider_id=self.diary_render_provider_id,
            timeout=self.diary_render_timeout,
            label="diary_render",
        )
        rendered = {
            str(item.get("episode_key") or "").strip(): str(item.get("content") or "").strip()
            for item in self._parse_json_array(render_text)
            if str(item.get("episode_key") or "").strip() and str(item.get("content") or "").strip()
        }
        raw_text = "\n".join(str(message.get("content") or "") for message in messages)
        threshold = float(getattr(self, "diary_must_coverage_threshold", 0.72) or 0.72)
        pipeline = getattr(self, "_diary_pipeline", None) or DiaryPipeline(self)

        def assess(content: str, episode: dict[str, Any]) -> dict[str, Any]:
            coverage, missing = self._diary_render_coverage(content, episode)
            support = pipeline.support_coverage(content, episode)
            person_ok, person_reason = self._diary_first_person_check(content, episode)
            risk_detail = pipeline.risk_report(content, raw_text)
            risk = float(risk_detail.risk) if getattr(
                self, "diary_transcript_check_enable", True
            ) else 0.0
            reasons: list[str] = []
            if coverage < threshold:
                reasons.append("coverage_below_threshold")
            if not person_ok:
                reasons.append("first_person:" + person_reason)
            if risk >= 0.42:
                reasons.append("transcript_risk>=0.42")
            return {
                "coverage": float(coverage), "support": float(support),
                "missing": list(missing), "person_ok": bool(person_ok),
                "person_reason": person_reason, "risk": float(risk), "reasons": reasons,
                "source_overlap_ratio": float(risk_detail.longest_common_ratio),
                "direct_quote_ratio": float(risk_detail.quote_ratio),
                "compression_ratio": float(risk_detail.compression_ratio),
            }

        assessments = {
            episode["episode_key"]: assess(rendered[episode["episode_key"]], episode)
            for episode in episodes if episode["episode_key"] in rendered
        }
        retry_reasons = {
            key: ",".join(report["reasons"])
            for key, report in assessments.items() if report["reasons"]
        }
        initial_missing_keys = [
            episode["episode_key"] for episode in episodes
            if episode["episode_key"] not in rendered
        ]
        for key in initial_missing_keys:
            retry_reasons[key] = "missing_episode_key"
        if retry_reasons:
            retry_rendered: dict[str, str] = {}
            try:
                retry_text = await self._call_memory_generation_llm(
                    render_prompt
                    + "\n\n【渲染质量修正】以下 episode_key 各自只因所列精确原因需要重写：\n"
                    + json.dumps(retry_reasons, ensure_ascii=False, indent=2)
                    + "\n这是唯一一次重试。重新输出完整 JSON 数组；must_write 不得遗漏，"
                      "必须保持第一人称私密日记体并降低聊天转录密度，不得新增事实。",
                    provider_id=self.diary_render_provider_id,
                    timeout=self.diary_render_timeout,
                    label="diary_render_retry",
                )
                retry_rendered = {
                    str(item.get("episode_key") or "").strip(): str(item.get("content") or "").strip()
                    for item in self._parse_json_array(retry_text)
                    if str(item.get("episode_key") or "").strip() and str(item.get("content") or "").strip()
                }
            except Exception as exc:
                logger.warning("[memos-memory] diary render retry failed; keeping grounded episodes: %s", exc)
            by_key = {episode["episode_key"]: episode for episode in episodes}
            for key, retry_content in retry_rendered.items():
                if key not in retry_reasons or key not in by_key:
                    continue
                retry_report = assess(retry_content, by_key[key])
                current_report = assessments.get(key)
                if current_report is None:
                    rendered[key] = retry_content
                    assessments[key] = retry_report
                    continue
                retry_quality = (
                    not bool(retry_report["reasons"]), retry_report["person_ok"],
                    retry_report["coverage"], retry_report["support"], -retry_report["risk"],
                )
                current_quality = (
                    not bool(current_report["reasons"]), current_report["person_ok"],
                    current_report["coverage"], current_report["support"], -current_report["risk"],
                )
                if retry_quality > current_quality:
                    rendered[key] = retry_content
                    assessments[key] = retry_report

        fallback_keys: list[str] = []
        fallback_reasons: dict[str, str] = {}
        missing_keys = [episode["episode_key"] for episode in episodes if episode["episode_key"] not in rendered]
        for episode in episodes:
            key = episode["episode_key"]
            current_report = assessments.get(key)
            if key in rendered and current_report is not None and not current_report["reasons"]:
                continue
            if key not in rendered:
                fallback_reasons[key] = "missing_key_fallback"
            else:
                fallback_reasons[key] = "grounded_fallback_after:" + ",".join(
                    current_report["reasons"]
                )
            fallback = self._grounded_episode_fallback_diary(episode)
            if not fallback:
                raise ValueError(f"grounded episode {key} has no renderable evidence")
            rendered[key] = fallback
            assessments[key] = assess(fallback, episode)
            fallback_keys.append(key)
        if fallback_keys:
            logger.warning(
                "[memos-memory] renderer left invalid or missing keys (%s); "
                "using evidence-preserving fallback reasons=%s",
                ",".join(fallback_keys), fallback_reasons,
            )

        diaries: list[dict[str, Any]] = []
        for episode in episodes:
            key = episode["episode_key"]
            report = assessments[key]
            diary = dict(episode)
            diary["content"] = rendered[key]
            diary["_evidence_quality"] = "source_grounded"
            diary["_render_coverage"] = round(report["coverage"], 3)
            diary["_must_coverage"] = round(report["coverage"], 3)
            diary["_support_coverage"] = round(report["support"], 3)
            diary["_transcript_risk"] = round(report["risk"], 3)
            diary["_source_overlap_ratio"] = round(report["source_overlap_ratio"], 4)
            diary["_direct_quote_ratio"] = round(report["direct_quote_ratio"], 4)
            diary["_compression_ratio"] = round(report["compression_ratio"], 4)
            diary["_render_retry_reason"] = fallback_reasons.get(key, retry_reasons.get(key, ""))
            diary["_render_version"] = "4.6.2"
            diary["_diary_render_version"] = "4.6.2"
            diary["_render_missing"] = list(report["missing"])[:8]
            diary["_render_fallback"] = key in fallback_keys
            if key in missing_keys:
                diary["_render_retry_reason"] = "missing_key_fallback"
            logger.info(
                "[memory][diary] %s chars=%d must=%.3f support=%.3f risk=%.3f retry=%s fallback=%s",
                key, len(diary["content"]), diary["_must_coverage"],
                diary["_support_coverage"], diary["_transcript_risk"],
                diary["_render_retry_reason"] or "none", diary["_render_fallback"],
            )
            diaries.append(diary)
        return diaries

    # ---------- 写一条日记 ----------
    # ---------- 重要性启发式标注 (v1.6) ----------
    def _auto_importance(self, text: str) -> int:
        """启发式重要性评分 1-5, 基于可配置关键词 + 情感强度 + 文本长度。"""
        score = 3  # baseline

        # Parse configurable keywords (comma-separated)
        t5_kw = [w.strip() for w in self.imp_tier5_keywords.split(",") if w.strip()]
        t4_kw = [w.strip() for w in self.imp_tier4_keywords.split(",") if w.strip()]
        t3_kw = [w.strip() for w in self.imp_tier3_keywords.split(",") if w.strip()]
        t1_kw = [w.strip() for w in self.imp_low_keywords.split(",") if w.strip()]

        t5 = sum(1 for kw in t5_kw if kw in text) if t5_kw else 0
        t4 = sum(1 for kw in t4_kw if kw in text) if t4_kw else 0
        t3 = sum(1 for kw in t3_kw if kw in text) if t3_kw else 0
        t1 = sum(1 for kw in t1_kw if kw in text) if t1_kw else 0

        if t5 >= 1:
            score = 5
        elif t4 >= 3:
            score = 5
        elif t4 >= 2:
            score = 4
        elif t4 >= 1 or t3 >= 2:
            score = 3
        elif t3 >= 1:
            score = 3
        else:
            score = 2

        # emotional intensity
        excl = text.count("！") + text.count("!")
        ques = text.count("？") + text.count("?")
        ellip = text.count("…") + text.count("...")
        emotion = min((excl + ques) // 3, 1) + min(ellip // 2, 1)
        score = min(5, score + emotion)

        # long text bonus
        if len(text) > 500 and score < 5:
            score += 1

        # daily life penalty
        if t1 >= 3 and score > 2:
            score -= 1

        return max(1, min(5, score))

    def _keyword_candidates(self, text: str, limit: int = 8) -> list[tuple[str, float]]:
        """Extract robust proactive-recall keywords without depending on a live BM25 cache."""
        text = _strip_internal_metadata(text)
        if not text:
            return []
        stopwords = {
            "一个", "一下", "一些", "这个", "那个", "我们", "你们", "他们", "她们", "自己",
            "今天", "明天", "昨天", "时候", "感觉", "还是", "然后", "只是", "已经", "因为",
            "所以", "但是", "如果", "没有", "不是", "不会", "可以", "一起", "第一次",
        }
        try:
            import jieba
            tokens = [w.strip() for w in jieba.lcut(text) if w.strip()]
        except Exception:
            clean = re.sub(r"\s+", "", text)
            tokens = [clean[i:i + 2] for i in range(max(0, len(clean) - 1))]
            tokens.extend([clean[i:i + 3] for i in range(max(0, len(clean) - 2))])
        tf: dict[str, int] = {}
        for t in tokens:
            t = t.strip("，。！？、：；“”‘’（）()[]【】<>《》 \t\r\n")
            if len(t) < 2 or t in stopwords:
                continue
            if re.fullmatch(r"\d+", t):
                continue
            tf[t] = tf.get(t, 0) + 1
        scored = []
        for token, count in tf.items():
            # Longer named entities/phrases are usually more useful than generic bigrams.
            score = count * (1.0 + min(len(token), 6) * 0.15)
            if token.startswith("#"):
                score += 2.0
            scored.append((token, round(score, 3)))
        scored.sort(key=lambda x: (-x[1], -len(x[0]), x[0]))
        return scored[:limit]

    def _similarity_text_terms(self, text: str) -> set[str]:
        text = _strip_internal_metadata(text or "")
        if not text:
            return set()
        terms = {k for k, _ in self._keyword_candidates(text, limit=16)}
        clean = re.sub(r"\s+", "", text)
        if len(clean) >= 2:
            terms.update(clean[i:i + 2] for i in range(min(len(clean) - 1, 120)))
        return {t for t in terms if len(t) >= 2}

    @staticmethod
    def _similarity_edge_key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def _similarity_text_score(self, a_terms: set[str], b_terms: set[str]) -> float:
        if not a_terms or not b_terms:
            return 0.0
        inter = len(a_terms & b_terms)
        if inter <= 0:
            return 0.0
        smaller = min(len(a_terms), len(b_terms)) or 1
        union_terms = len(a_terms | b_terms) or 1
        # Overlap coefficient catches short related diary fragments better than plain Jaccard.
        return max(inter / union_terms, inter / smaller * 0.78)

    @staticmethod
    def _similarity_cluster_edge_score(
        *,
        threshold: float,
        embedding_similarity: float,
        text_similarity: float,
        shared_tag_count: int,
        same_type: bool,
    ) -> tuple[float, str]:
        """Score a cluster edge without letting weak metadata alone merge events."""
        threshold = max(0.0, min(0.999, float(threshold)))
        emb_sim = max(0.0, min(1.0, float(embedding_similarity or 0.0)))
        text_sim = max(0.0, min(1.0, float(text_similarity or 0.0)))
        shared_count = max(0, int(shared_tag_count or 0))
        candidates: list[tuple[float, str]] = [(emb_sim, "embedding_avg")]

        if text_sim >= 0.46 and (same_type or shared_count > 0):
            candidates.append((
                min(0.985, threshold + min(0.08, (text_sim - 0.46) * 0.22)),
                "text_overlap",
            ))
        elif text_sim >= 0.58:
            candidates.append((
                min(0.975, threshold + min(0.06, (text_sim - 0.58) * 0.18)),
                "text_overlap",
            ))

        # Tags and type are supporting evidence: require some body overlap so
        # generic labels cannot connect unrelated events.
        if same_type and shared_count >= 2 and text_sim >= 0.14:
            margin = 0.006 + min(0.022, (text_sim - 0.14) * 0.06) + min(0.012, (shared_count - 2) * 0.006)
            candidates.append((min(0.97, threshold + margin), "tag_type_overlap"))
        elif shared_count >= 3 and text_sim >= 0.20:
            margin = 0.004 + min(0.018, (text_sim - 0.20) * 0.05) + min(0.010, (shared_count - 3) * 0.005)
            candidates.append((min(0.96, threshold + margin), "tag_overlap"))

        # Near-threshold semantic pairs may cross the line only when independent
        # lexical or metadata evidence agrees with the embedding.
        if emb_sim >= max(0.68, threshold - 0.075) and (text_sim >= 0.16 or shared_count > 0):
            support = min(0.045, text_sim * 0.07)
            support += min(0.018, shared_count * 0.006)
            support += 0.010 if same_type else 0.0
            candidates.append((min(0.995, emb_sim + support), "evidence_fusion"))

        source_priority = {
            "embedding_avg": 4,
            "evidence_fusion": 3,
            "text_overlap": 2,
            "tag_type_overlap": 1,
            "tag_overlap": 0,
        }
        return max(candidates, key=lambda item: (item[0], source_priority.get(item[1], -1)))

    def _build_similarity_cluster_index(self) -> dict[str, int | float]:
        if self._vec is None:
            return {"memos": 0, "clusters": 0, "edges": 0, "threshold": float(self.recall_cluster_similarity or 0.88)}
        threshold = float(self.recall_cluster_similarity or 0.88)
        conn = self._vec._connect()
        rows = conn.execute(
            """SELECT memo_name, MAX(importance) AS imp, MAX(created_ts) AS created_ts,
                      MAX(tags) AS tags, MAX(memory_type) AS memory_type,
                      GROUP_CONCAT(chunk_text, '\n') AS text
               FROM chunks
               GROUP BY memo_name
               ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC"""
        ).fetchall()
        memos = []
        for r in rows:
            mn = r["memo_name"]
            avg_emb = None
            try:
                avg_emb = self._vec.get_memo_embedding_avg(mn) or self._vec.get_memo_embedding(mn)
            except Exception:
                avg_emb = self._vec.get_memo_embedding(mn)
            memos.append({
                "memo_name": mn,
                "embedding": avg_emb,
                "tags": self._vec.get_memo_tags(mn),
                "importance": int(r["imp"] or 3),
                "created_ts": float(r["created_ts"] or 0.0),
                "memory_type": r["memory_type"] or "",
                "terms": self._similarity_text_terms(r["text"] or ""),
            })
        parent = {m["memo_name"]: m["memo_name"] for m in memos}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        edges_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for i in range(len(memos)):
            a = memos[i]
            for j in range(i + 1, len(memos)):
                b = memos[j]
                emb_sim = 0.0
                text_sim = self._similarity_text_score(a["terms"], b["terms"])
                shared = sorted(a["tags"] & b["tags"])
                same_type = bool(a.get("memory_type") and a.get("memory_type") == b.get("memory_type"))
                if a["embedding"] and b["embedding"]:
                    emb_sim = self._cosine_sim(a["embedding"], b["embedding"])
                sim, source = self._similarity_cluster_edge_score(
                    threshold=threshold,
                    embedding_similarity=emb_sim,
                    text_similarity=text_sim,
                    shared_tag_count=len(shared),
                    same_type=same_type,
                )
                if sim < threshold:
                    continue
                ma, mb = self._similarity_edge_key(a["memo_name"], b["memo_name"])
                edges_by_key[(ma, mb)] = {
                    "memo_a": ma,
                    "memo_b": mb,
                    "similarity": round(float(sim), 4),
                    "source": source,
                    "shared_tags": ",".join(shared),
                }
                union(ma, mb)
        groups: dict[str, list[dict[str, Any]]] = {}
        for m in memos:
            groups.setdefault(find(m["memo_name"]), []).append(m)
        sorted_groups = sorted(groups.values(), key=lambda xs: (-len(xs), xs[0]["memo_name"]))
        edge_score: dict[str, float] = {}
        edge_reason: dict[str, str] = {}
        for e in edges_by_key.values():
            sim = float(e["similarity"])
            for mn in (e["memo_a"], e["memo_b"]):
                if sim > edge_score.get(mn, 0.0):
                    edge_score[mn] = sim
                    edge_reason[mn] = str(e.get("source") or "embedding")
        assignments = []
        for idx, group in enumerate(sorted_groups, start=1):
            names = [m["memo_name"] for m in group]
            rep = sorted(group, key=lambda m: (m["importance"], m["created_ts"]), reverse=True)[0]["memo_name"]
            cid = f"sim-{idx:04d}" if len(group) > 1 else f"solo:{names[0]}"
            for m in group:
                assignments.append({
                    "memo_name": m["memo_name"],
                    "cluster_id": cid,
                    "representative": rep,
                    "cluster_size": len(group),
                    "max_similarity": edge_score.get(m["memo_name"], 1.0 if len(group) == 1 else 0.0),
                    "reason": edge_reason.get(m["memo_name"], "singleton" if len(group) == 1 else "component"),
                })
        self._vec.similarity_clusters_store(assignments, list(edges_by_key.values()))
        real_clusters = sum(1 for g in sorted_groups if len(g) > 1)
        return {
            "memos": len(memos),
            "clusters": len(sorted_groups),
            "real_clusters": real_clusters,
            "edges": len(edges_by_key),
            "threshold": threshold,
        }

    def _rebuild_aux_indexes(self, rebuild_graph: bool = True) -> tuple[int, int]:
        """Rebuild BM25 cache, proactive keywords, and optionally similarity clusters."""
        if self._vec is None:
            return (0, 0)
        try:
            self._vec.invalidate_bm25()
            self._vec._ensure_bm25()
        except Exception as e:
            logger.debug("[memos-memory] BM25 rebuild failed: %s", e)
        conn = self._vec._connect()
        rows = conn.execute(
            "SELECT memo_name, chunk_text, tags FROM chunks ORDER BY memo_name, rowid"
        ).fetchall()
        memo_texts: dict[str, list[str]] = {}
        for r in rows:
            memo_texts.setdefault(r["memo_name"], []).append(r["chunk_text"] or "")
        keyword_count = 0
        for memo_name, parts in memo_texts.items():
            kws = self._keyword_candidates("\n".join(parts), limit=5)
            if kws:
                self._vec.keywords_store(memo_name, kws)
                keyword_count += 1
        edge_count = 0
        if rebuild_graph:
            try:
                stats = self._build_similarity_cluster_index()
                edge_count = int(stats.get("edges") or 0)
            except Exception as e:
                logger.debug("[memos-memory] similarity cluster rebuild failed: %s", e)
        return (keyword_count, edge_count)

    async def _store_one_diary(self, diary: dict[str, Any], source_session: str = "",
                               importance: int = 3, manual: bool = False,
                               source_kind: str = "auto") -> bool:
        content = (diary.get("content") or "").strip()
        if not content:
            return False
        raw_tags = diary.get("tags") or []
        content, raw_tags = _extract_inline_tags_from_content(content, raw_tags)
        if not content:
            return False
        flow_tags = {
            "#保底", "#保底压缩", "#夜间压缩", "#晚间压缩", "#自动压缩",
            "#eod", "#EOD", "#迁移", "#livingmemory",
        }
        tags = []
        seen_tags = set()
        for t in raw_tags:
            tag = str(t).strip()
            if not tag:
                continue
            if not tag.startswith("#"):
                tag = "#" + tag
            if tag in flow_tags or tag.lower() in {"#eod", "#livingmemory"}:
                continue
            if tag not in seen_tags:
                tags.append(tag)
                seen_tags.add(tag)
        time_meta = normalize_memory_time(
            diary,
            fallback_now_ts=time.time(),
            timezone_name=self.rp_time_timezone,
        )
        ts_text = time_meta["display"]
        imp = importance
        memory_type = _normalize_memory_type(diary.get("memory_type"), content, tags)
        long_effect = _safe_meta_text(diary.get("long_effect"))
        trigger_hint = _safe_meta_text(diary.get("trigger_hint"))
        machine = {
            "scene_anchor": _safe_meta_text(diary.get("scene_anchor"), 160),
            "retrieval_key": _safe_meta_text(diary.get("retrieval_key"), 260),
            "state_change": _safe_meta_text(diary.get("state_change"), 260),
            "entities": [str(x).strip() for x in (diary.get("entities") or []) if str(x).strip()][:20]
                if isinstance(diary.get("entities") or [], list) else [],
            "episode_key": _safe_meta_text(diary.get("episode_key"), 40),
            "source_batch_id": _safe_meta_text(diary.get("_source_batch_id"), 80),
            "evidence_quality": _safe_meta_text(diary.get("_evidence_quality"), 40),
        }

        # memos 全文:首行日期,正文,末行标签
        memos_text = ts_text + "\n" + content
        extra_lines = []
        if long_effect:
            extra_lines.append("长期影响: " + long_effect)
        if trigger_hint:
            extra_lines.append("触发线索: " + trigger_hint)
        if extra_lines:
            memos_text += "\n" + "\n".join(extra_lines)
        if tags:
            memos_text += "\n" + " ".join(tags)
        meta64 = _machine_meta64(machine)
        memos_text += (
            f"\n<!-- memos-memory:importance={max(1, min(5, int(imp or 3)))};"
            f"manual={1 if manual else 0};type={memory_type};source={source_kind or 'auto'};"
            f"occurred_at={time_meta['occurred_at']};time_basis={time_meta['time_basis']}"
            f"{';meta64=' + meta64 if meta64 else ''} -->"
        )

        memo_name = None
        memos_written = False
        vec_written = False
        source_created_ts = 0.0
        source_updated_ts = 0.0
        if self._memos is not None:
            try:
                memo = await self._memos.create_memo(content=memos_text, visibility="PRIVATE")
                memo_name = memo.get("name")
                memos_written = bool(memo_name)
                source_created_ts, source_updated_ts = memo_source_times(memo)
            except Exception as e:
                logger.warning("[memos-memory] memos create 失败: %s", e)
        if memo_name is None:
            memo_name = f"local/{time.time_ns()}"

        embeddings = []
        if self._vec is not None and self._emb_provider is not None:
            try:
                if self.passage_index_enable:
                    passages = self._split_into_passages(content, self.passage_max_chars, self.passage_overlap_chars)
                else:
                    passages = self._split_into_passages(content, _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP_CHARS)
                chunks = [p["text"] for p in passages]
                embedding_texts = [self._passage_embedding_text(text, machine) for text in chunks]
                embeddings = await self._embed_batch(embedding_texts)
                inserted = await self._vec.insert_chunks(
                    memo_name=memo_name, chunks=chunks, embeddings=embeddings,
                    ts_text=ts_text, tags=tags, importance=imp,
                    source_session=source_session, manual=1 if manual else 0,
                    memory_type=memory_type, long_effect=long_effect, trigger_hint=trigger_hint,
                    occurred_at=time_meta["occurred_at"], event_ts=time_meta["event_ts"],
                    time_basis=time_meta["time_basis"], source_created_ts=source_created_ts,
                    source_updated_ts=source_updated_ts,
                    passages=passages, content_hash=self._content_hash(content),
                    scene_anchor=machine["scene_anchor"], retrieval_key=machine["retrieval_key"],
                    state_change=machine["state_change"], entities=machine["entities"],
                )
                vec_written = inserted > 0
            except Exception as e:
                logger.warning("[memos-memory] sqlite-vec \u5199\u5165\u5931\u8d25 (memo=%s): %s", memo_name, e)

        # v1.8: extract keywords
        try:
            if self._vec is not None:
                top_kw = self._keyword_candidates(content, limit=5)
                if top_kw:
                    self._vec.keywords_store(memo_name, top_kw)
                    logger.info("[memos-memory] keywords: %s -> %s", memo_name[:20], [k for k,_ in top_kw])
        except Exception as e:
            logger.debug("[memos-memory] keyword extraction failed: %s", e)

        # v2.2.6: refresh similarity-cluster edges for this memo only.
        try:
            if self._vec is not None and embeddings:
                dim = len(embeddings[0])
                avg_emb = [sum(e[i] for e in embeddings) / len(embeddings) for i in range(dim)]
                existing = self._vec.all_memo_names()
                existing.discard(memo_name)
                diary_tags = set(t.lstrip("#") for t in tags)
                diary_terms = self._similarity_text_terms(content)
                threshold = float(self.recall_cluster_similarity or 0.88)
                edges = []
                for other in existing:
                    try:
                        other_emb = self._vec.get_memo_embedding(other)
                        try:
                            other_emb = self._vec.get_memo_embedding_avg(other) or other_emb
                        except Exception:
                            pass
                        if other_emb:
                            emb_sim = self._cosine_sim(avg_emb, other_emb)
                            other_tags = self._vec.get_memo_tags(other)
                            shared = diary_tags & other_tags
                            other_meta = self._vec.get_memo_meta(other) or {}
                            other_text = "\n".join(
                                p.get("text", "") for p in self._vec.get_memo_passages(other)
                            ) or str(other_meta.get("chunk_text") or "")
                            text_sim = self._similarity_text_score(
                                diary_terms, self._similarity_text_terms(other_text)
                            )
                            sim, source = self._similarity_cluster_edge_score(
                                threshold=threshold,
                                embedding_similarity=emb_sim,
                                text_similarity=text_sim,
                                shared_tag_count=len(shared),
                                same_type=bool(memory_type and memory_type == other_meta.get("memory_type")),
                            )
                            if sim >= threshold:
                                ma, mb = self._similarity_edge_key(memo_name, other)
                                edges.append({
                                    "memo_a": ma,
                                    "memo_b": mb,
                                    "similarity": round(sim, 4),
                                    "source": source,
                                    "shared_tags": ",".join(sorted(shared)),
                                })
                    except Exception:
                        pass
                self._vec.similarity_edges_upsert_for_memo(memo_name, edges)
                self._vec.similarity_clusters_store_from_edges(min_similarity=threshold)
                self._last_diary_embeddings.append(avg_emb)
        except Exception as e:
            logger.debug("[memos-memory] similarity cluster refresh failed: %s", e)
        diary["_stored_memo_name"] = memo_name
        diary["_stored_source_created_ts"] = source_created_ts
        diary["_stored_source_updated_ts"] = source_updated_ts
        diary["_stored_time_meta"] = time_meta
        return bool(memos_written or vec_written)

    @staticmethod
    def _episode_card_text(diary: dict[str, Any]) -> str:
        evidence = diary.get("evidence") if isinstance(diary.get("evidence"), list) else []
        # v4.5: evidence details the diary render omitted come first — the
        # literary body will never carry them, so the machine card is their
        # only lexical surface. Verbatim quotes are kept alongside details so
        # exact original phrasing stays searchable (BM25 / LIKE rescue).
        missing = {
            " ".join(str(value).split())
            for value in (diary.get("_render_missing") or [])
            if str(value).strip()
        }
        evidence_lines: list[str] = []

        def _append_item(item: dict[str, Any]) -> None:
            detail = str(item.get("detail") or "").strip()
            quote = str(item.get("quote") or "").strip()
            if detail:
                evidence_lines.append(detail)
            if quote and quote != detail:
                evidence_lines.append("「" + quote + "」")
            if not detail and not quote:
                fallback = str(item.get("quote_text") or "").strip()
                if fallback:
                    evidence_lines.append("「" + fallback + "」")

        deferred: list[dict[str, Any]] = []
        for item in evidence[:16]:
            if not isinstance(item, dict):
                continue
            normalized = " ".join(str(item.get("detail") or item.get("quote") or "").split())
            if missing and normalized in missing:
                _append_item(item)
            else:
                deferred.append(item)
        for item in deferred:
            if len(evidence_lines) >= 20:
                break
            _append_item(item)
        fields = [
            str(diary.get("occurred_at") or ""),
            str(diary.get("scene_anchor") or ""),
            str(diary.get("retrieval_key") or ""),
            " ".join(str(value) for value in (diary.get("entities") or []) if str(value).strip()),
            str(diary.get("state_change") or ""),
            str(diary.get("long_effect") or ""),
            str(diary.get("trigger_hint") or ""),
            str(diary.get("affect_before") or ""),
            str(diary.get("affect_after") or ""),
            " ".join(str(value) for value in (diary.get("unresolved") or []) if str(value).strip()),
            " ".join(evidence_lines[:20]),
        ]
        return "\n".join(" ".join(value.split()) for value in fields if value and value.strip())

    @staticmethod
    def _derived_diary_evidence(content: str) -> list[dict[str, Any]]:
        evidence = []
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[。！？!?])\s*|\n+", str(content or ""))
            if value.strip()
        ]
        for index, sentence in enumerate(sentences[:12]):
            evidence.append({
                "kind": "derived_passage",
                "actor": "",
                "detail": sentence[:600],
                "quote": "",
                "turn_indexes": [],
                "confidence": 0.55,
                "grounded": False,
            })
        return evidence

    async def _persist_episode_for_diary(
        self,
        diary: dict[str, Any],
        *,
        source_batch_id: str = "",
        source_kind: str = "auto",
    ) -> bool:
        if self._episodes is None:
            return True
        memo_name = str(diary.get("_stored_memo_name") or "").strip()
        content = str(diary.get("content") or "").strip()
        if not memo_name or not content:
            return False
        quality = str(diary.get("_evidence_quality") or "diary_derived")
        episode = dict(diary)
        if not isinstance(episode.get("evidence"), list) or not episode.get("evidence"):
            episode["evidence"] = self._derived_diary_evidence(content)
            quality = "diary_derived"
        time_meta = diary.get("_stored_time_meta") if isinstance(diary.get("_stored_time_meta"), dict) else {}
        episode.update({
            "occurred_at": str(time_meta.get("occurred_at") or diary.get("occurred_at") or ""),
            "event_ts": float(time_meta.get("event_ts") or diary.get("event_ts") or 0),
            "time_basis": str(time_meta.get("time_basis") or diary.get("time_basis") or "unknown"),
        })
        card_text = self._episode_card_text(episode)
        if not card_text:
            card_text = content[:1200]
        embedding = await self._embed(card_text)
        self._episodes.upsert_episode(
            memo_name=memo_name,
            episode=episode,
            card_text=card_text,
            embedding=embedding,
            source_batch_id=source_batch_id,
            source_kind=source_kind,
            legacy=quality != "source_grounded",
            evidence_quality=quality,
            diary_content_hash=self._content_hash(content),
            source_updated_ts=float(diary.get("_stored_source_updated_ts") or 0),
            diary_render_version=str(
                diary.get("_diary_render_version") or diary.get("_render_version") or ""
            ),
            must_coverage=float(diary.get("_must_coverage", -1.0)),
            support_coverage=float(diary.get("_support_coverage", -1.0)),
            transcript_risk=float(diary.get("_transcript_risk", -1.0)),
            render_retry_reason=str(diary.get("_render_retry_reason") or ""),
            original_memo_version=str(diary.get("_original_memo_version") or ""),
            source_overlap_ratio=float(diary.get("_source_overlap_ratio", -1.0)),
            direct_quote_ratio=float(diary.get("_direct_quote_ratio", -1.0)),
            compression_ratio=float(diary.get("_compression_ratio", -1.0)),
            render_fallback=bool(diary.get("_render_fallback", False)),
        )
        return True

    # ---------- 4.6.0-test3 persistent long-diary workflow ----------

    def _long_diaries_list(
        self, limit: int = 20, min_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        if self._episodes is None:
            return []
        threshold = max(1, int(
            self.diary_rewrite_preview_min_chars if min_chars is None else min_chars
        ))
        output: list[dict[str, Any]] = []
        for episode in self._episodes.list_episodes(limit=max(20, min(500, int(limit) * 10))):
            card_text = str(episode.get("card_text") or "")
            if len(card_text) < threshold:
                continue
            batch_id = str(episode.get("source_batch_id") or "")
            turns = self._episodes.source_turns(batch_id) if batch_id else []
            if not turns:
                continue
            output.append({
                "episode_id": episode.get("episode_id"),
                "memo_name": episode.get("memo_name"),
                "card_len": len(card_text),
                "source_batch_id": batch_id,
                "source_turns": len(turns),
                "occurred_at": episode.get("occurred_at") or "",
                "evidence_quality": episode.get("evidence_quality") or "",
                "render_version": episode.get("diary_render_version") or "",
                "must_coverage": float(episode.get("must_coverage", -1.0)),
                "support_coverage": float(episode.get("support_coverage", -1.0)),
                "transcript_risk": float(episode.get("transcript_risk", -1.0)),
            })
            if len(output) >= max(1, int(limit)):
                break
        return output

    async def _create_diary_rewrite_preview(self, episode_id: str) -> dict[str, Any]:
        if self._episodes is None:
            raise RuntimeError("原文库未就绪")
        episode = self._episodes.get_episode_by_id(str(episode_id or ""))
        if not episode:
            raise RuntimeError("情景不存在")
        batch_id = str(episode.get("source_batch_id") or "")
        turns = self._episodes.source_turns(batch_id) if batch_id else []
        if not turns:
            raise RuntimeError("来源轮次不可读，无法重构")
        messages = [{
            "role": str(turn.get("role") or ""),
            "content": str(turn.get("content") or ""),
            "event_ts": float(turn.get("event_ts") or 0),
            "event_timezone": str(turn.get("event_timezone") or self.rp_time_timezone),
        } for turn in turns]
        messages_text = format_messages_for_prompt(
            messages,
            max_turns=max(1, (len(messages) + 1) // 2),
            timezone_name=self.rp_time_timezone,
            max_chars_per_turn=self.raw_archive_prompt_view_max_chars,
        )
        candidates = self._scene_splitter.detect(messages) if self.scene_split_enable else []
        cap = max(1, int(self.diary_count_max_cap or 1))
        diaries = await self._generate_evidence_first_diaries(
            messages, messages_text, self.diary_count, diary_cap=cap, exact_count=False,
        )
        preview_id = "rw_" + hashlib.sha256(
            f"{episode_id}|{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:20]
        new_diaries = []
        for diary in diaries:
            item = dict(diary)
            item["must_coverage"] = float(diary.get("_must_coverage", -1.0))
            item["support_coverage"] = float(diary.get("_support_coverage", -1.0))
            item["transcript_risk"] = float(diary.get("_transcript_risk", -1.0))
            item["render_retry_reason"] = str(diary.get("_render_retry_reason") or "")
            new_diaries.append(item)
        payload = {
            "preview_id": preview_id,
            "episode_id": str(episode_id),
            "old_memo_name": str(episode.get("memo_name") or ""),
            "old_card_text": str(episode.get("card_text") or ""),
            "source_batch_id": batch_id,
            "source_turns": len(messages),
            "scene_candidates": len(candidates),
            "new_diaries": new_diaries,
            "created_ts": time.time(),
        }
        saved = self._episodes.save_diary_preview(payload)
        self._episodes.discard_old_previews(keep=20)
        return saved

    async def _confirm_diary_rewrite(self, preview_id: str) -> dict[str, Any]:
        if self._episodes is None:
            raise RuntimeError("原文库未就绪")
        preview = self._episodes.get_diary_preview(str(preview_id or ""))
        if not preview:
            raise RuntimeError("预览不存在或已过期")
        payload = preview.get("payload") if isinstance(preview.get("payload"), dict) else {}
        source_batch_id = str(preview.get("source_batch_id") or payload.get("source_batch_id") or "")
        old_card_text = str(preview.get("old_card_text") or payload.get("old_card_text") or "")
        loop = asyncio.get_running_loop()
        stored_payloads: dict[int, dict[str, Any]] = {}

        async def store_item(item: dict[str, Any], item_index: int) -> str:
            diary = dict(item)
            diary["_source_batch_id"] = source_batch_id
            diary["_evidence_quality"] = "source_grounded"
            diary["_original_memo_version"] = old_card_text[:60000]
            diary["_render_version"] = "4.6.2-rewrite"
            diary["_diary_render_version"] = "4.6.2-rewrite"
            diary["_must_coverage"] = float(
                diary.get("_must_coverage", diary.get("must_coverage", -1.0))
            )
            diary["_support_coverage"] = float(
                diary.get("_support_coverage", diary.get("support_coverage", -1.0))
            )
            diary["_transcript_risk"] = float(
                diary.get("_transcript_risk", diary.get("transcript_risk", -1.0))
            )
            diary["_render_retry_reason"] = str(
                diary.get("_render_retry_reason") or diary.get("render_retry_reason") or ""
            )
            stored = await self._store_one_diary(
                diary, source_session="rewrite:" + str(preview_id),
                importance=_normalize_importance(diary.get("importance"), 3),
                source_kind="rewrite",
            )
            memo_name = str(diary.get("_stored_memo_name") or "")
            if not stored or not memo_name:
                raise RuntimeError(f"第 {item_index + 1} 篇写入失败")
            # PreviewStore owns the episode DB lock while invoking this callback;
            # defer episode-card upsert until its per-item transaction completes.
            stored_payloads[item_index] = diary
            return memo_name

        def apply_item(item: dict[str, Any], item_index: int) -> str:
            future = asyncio.run_coroutine_threadsafe(store_item(item, item_index), loop)
            # _store_one_diary / MemosClient already enforce provider/network
            # timeouts. A second local timeout would leave the coroutine running
            # and could create an orphan memo after this callback returned.
            return future.result()

        def record_rollback(old_name: str, episode_id_value: str,
                            new_names: list[str], note: str) -> int:
            return self._episodes.record_diary_rollback_atomic(
                old_memo_name=old_name, episode_id=episode_id_value,
                new_memo_names=new_names, note=note,
            )

        async def compensate_item_async(new_name: str, item_index: int) -> None:
            try:
                if self._memos is not None:
                    await self._memos.delete_memo(new_name)
                if self._vec is not None:
                    self._vec.delete_memo_index(new_name)
                self._episodes.delete_by_memo_name(new_name)
                logger.warning(
                    "[memory][diary] compensated orphan preview item=%d memo=%s",
                    item_index, new_name,
                )
            except Exception as exc:
                logger.error(
                    "[memory][diary] preview compensation failed item=%d memo=%s: %s",
                    item_index, new_name, exc,
                )
                raise

        def compensate_item(new_name: str, item_index: int) -> None:
            future = asyncio.run_coroutine_threadsafe(
                compensate_item_async(new_name, item_index), loop,
            )
            future.result()

        result = await asyncio.to_thread(
            self._episodes.confirm_diary_preview,
            str(preview_id), apply_item, record_rollback, compensate_item,
        )
        for item_result in result.get("results") or []:
            if str(item_result.get("status") or "") != "succeeded":
                continue
            diary = stored_payloads.get(int(item_result.get("index") or 0))
            if diary is not None:
                try:
                    await self._persist_episode_for_diary(
                        diary, source_batch_id=source_batch_id, source_kind="rewrite",
                    )
                except Exception as exc:
                    logger.warning("[memory][diary] rewrite episode view deferred: %s", exc)
        return result

    async def _rollback_diary_rewrite(
        self, memo_name: str = "", preview_id: str = "",
    ) -> dict[str, Any]:
        if self._episodes is None or self._memos is None:
            raise RuntimeError("Memos 或原文库未就绪")
        rows: list[dict[str, Any]] = []
        if preview_id:
            rows = self._episodes.diary_preview_rollback_targets(str(preview_id))
        elif memo_name:
            rows = self._episodes.rollback_rows_for_memo(str(memo_name))
        deleted = 0
        errors: list[str] = []
        for row in rows:
            new_name = str(row.get("new_memo_name") or row.get("new_content") or "")
            if not new_name:
                continue
            try:
                if await self._memos.delete_memo(new_name):
                    deleted += 1
                    if self._vec is not None:
                        self._vec.delete_memo_index(new_name)
                    self._episodes.delete_by_memo_name(new_name)
                    rollback_id = int(row.get("rollback_id") or row.get("id") or 0)
                    if rollback_id:
                        self._episodes.mark_rollback_reverted(rollback_id)
            except Exception as exc:
                errors.append(f"{new_name}: {exc}")
        if preview_id and deleted == len([row for row in rows if row.get("new_memo_name")]):
            self._episodes.update_diary_preview_status(str(preview_id), "discarded")
        return {"deleted": deleted, "rows": len(rows), "errors": errors}

    async def _discard_diary_preview(self, preview_id: str) -> dict[str, Any]:
        if self._episodes is None:
            raise RuntimeError("原文库未就绪")
        return {
            "discarded": self._episodes.discard_diary_preview(str(preview_id or "")),
            "preview_id": preview_id,
        }

    def _list_diary_previews(self, limit: int = 30) -> list[dict[str, Any]]:
        if self._episodes is None:
            return []
        return self._episodes.list_diary_previews(limit=max(1, min(200, int(limit))))

    async def _production_generation_switch(self) -> dict[str, Any]:
        if not await self._ensure_init() or self._episodes is None:
            raise RuntimeError("原文库未就绪")
        if not self._emb_dim or not self._emb_model_id:
            raise RuntimeError("embedding 代际信息不可用")
        return self._episodes.switch_generation(self._emb_dim, self._emb_model_id)

    async def _production_generation_rollback(self) -> dict[str, Any]:
        if not await self._ensure_init() or self._episodes is None:
            raise RuntimeError("原文库未就绪")
        return self._episodes.rollback_generation()

    async def _production_restore_snapshot(self, file_name: str) -> dict[str, Any]:
        if not await self._ensure_init() or self._episodes is None:
            raise RuntimeError("原文库未就绪")
        return await asyncio.to_thread(self._episodes.restore_snapshot, str(file_name or ""))

    async def _compress_and_store(self, umo: str, msgs: list[dict[str, Any]], diary_count: int | None = None,
                                  source_kind: str = "auto", buffer_up_to_seq: int | None = None) -> int:
        if not self.character_name:
            logger.debug("[memos-memory] 跳过压缩: character_name 未配置")
            return 0
        if source_kind == "eod":
            # The 23:45 checkpoint owns a smaller floor than normal compression.
            # Even one complete turn may contain a promise or relationship change;
            # evidence extraction still decides whether it deserves a diary.
            if sum(1 for message in msgs if message.get("role") == "assistant") < max(
                1, int(getattr(self, "eod_checkpoint_min_turns", 1) or 1)
            ):
                return 0
        elif len(msgs) < _MIN_MSGS_TO_COMPRESS:
            return 0
        n_msg = len(msgs)
        recorded_dates = recorded_message_dates(msgs, self.rp_time_timezone)
        requested_dc = diary_count or self.diary_count
        candidates = self._scene_splitter.detect(msgs) if self.scene_split_enable else []
        diary_cap = max(1, int(self.diary_count_max_cap or 1))
        base_target = min(diary_cap, max(requested_dc, len(recorded_dates), 1))
        dc = (
            self._scene_splitter.dynamic_diary_count(
                base_target, msgs, candidates, diary_cap, source_kind=source_kind,
            )
            if self.scene_split_enable else base_target
        )
        logger.info(
            "[memos-memory] 开始压缩 %d 条消息 (session=%s, target=%d, cap=%d)",
            n_msg, umo[:12], dc, diary_cap,
        )
        logger.info(
            "[memory][scene] candidates=%d turns=%d target=%d cap=%d",
            len(candidates), n_msg, dc, diary_cap,
        )
        self._log_event("compress", f"开始压缩 {n_msg}条 -> 目标{dc}篇/上限{diary_cap}篇", {
            "session": umo[:12], "msg_count": n_msg, "diary_count": dc,
            "diary_cap": diary_cap, "scene_candidates": len(candidates),
            "source": source_kind, "recorded_dates": recorded_dates,
        })
        # This is only the compression task reference time. Persisted per-message
        # timestamps below remain authoritative for when the events occurred.
        time_ctx = self._compression_time_context(umo, source_kind)
        messages_text = format_messages_for_prompt(
            msgs,
            max_turns=max(1, (len(msgs) + 1) // 2),
            timezone_name=self.rp_time_timezone,
            max_chars_per_turn=self.raw_archive_prompt_view_max_chars,
        )
        prompt = build_compress_prompt(
            self.character_name,
            messages_text,
            base_target,
            enhancer_time=time_ctx,
            exact_diary_count=False,
            timezone_name=self.rp_time_timezone,
            message_time_context=recorded_time_context(msgs, self.rp_time_timezone),
        )
        source_batch_id = ""
        if self._episodes is not None and self.raw_evidence_archive_enable:
            try:
                # Archive ownership is lossless: prompt truncation above never
                # changes the original messages written to SourceArchive.
                source_batch_id = self._episodes.archive_batch(umo, msgs, source_kind)
                logger.info(
                    "[memory][archive] complete turns=%d user=%d assistant=%d batch=%s truncated=0",
                    n_msg,
                    sum(1 for message in msgs if str(message.get("role") or "") == "user"),
                    sum(1 for message in msgs if str(message.get("role") or "") == "assistant"),
                    source_batch_id,
                )
            except Exception as e:
                logger.warning("[memos-memory][episode] 原始证据归档失败，保留 buffer: %s", e)
                return 0
            if self.lean_source_evidence_enable:
                try:
                    indexed_turns = await self._index_source_batch_turns(source_batch_id)
                    if indexed_turns:
                        self._log_event(
                            "compress", f"原始轮次索引完成: {indexed_turns}条",
                            {"source_batch_id": source_batch_id, "indexed": indexed_turns},
                        )
                except Exception as e:
                    # The lossless archive is already durable. Retrieval can use
                    # episode/diary routes until the background migrator retries.
                    logger.warning("[memos-memory][episode] 原始轮次索引延后重试: %s", e)
        diaries: list[dict[str, Any]] = []
        generation_mode = "legacy"
        if self.evidence_first_generation_enable and self._episodes is not None:
            try:
                diaries = await self._generate_evidence_first_diaries(
                    msgs,
                    messages_text,
                    dc,
                    diary_cap=diary_cap,
                    exact_count=False,
                )
                generation_mode = "evidence_first"
                self._log_event("compress", f"证据优先生成完成: {len(diaries)}个情景", {
                    "session": umo[:12], "source_batch_id": source_batch_id,
                })
            except Exception as e:
                logger.warning("[memos-memory][episode] 证据优先生成失败，回退兼容日记流程: %s", e)
                self._log_event("compress", "证据优先生成降级", {
                    "session": umo[:12], "error": str(e)[:240],
                })
        llm_text = ""
        if not diaries:
            generation_mode = "legacy"
            try:
                llm_text = await self._call_llm_compress(prompt)
                diaries = self._parse_diaries(llm_text)
            except Exception as e:
                logger.warning("[memos-memory] 压缩 LLM 调用失败: %s", e)
                if source_batch_id and self._episodes is not None:
                    self._episodes.mark_batch(source_batch_id, "generation_failed")
                return 0
        if not diaries:
            logger.warning("[memos-memory] 压缩结果解析失败, 原文前200字: %s", (llm_text or "")[:200])
            if source_batch_id and self._episodes is not None:
                self._episodes.mark_batch(source_batch_id, "parse_failed")
            return 0
        for diary in diaries:
            diary["_source_batch_id"] = source_batch_id
        labels_by_date = recorded_time_labels_by_date(msgs, self.rp_time_timezone)
        diaries = self._normalize_buffer_diary_times(diaries, recorded_dates, labels_by_date)
        time_diag = self._buffer_diary_time_diagnostics(diaries, recorded_dates)
        date_mismatch = bool(time_diag["invalid_conversation_now"])
        cross_day_gap = len(recorded_dates) > 1 and bool(time_diag["missing_dates"])
        if generation_mode == "legacy" and (date_mismatch or cross_day_gap):
            try:
                corrections = []
                if date_mismatch:
                    corrections.append(
                        "conversation_now 使用了不属于缓冲记录的日期: "
                        + ", ".join(x["event_date"] for x in time_diag["invalid_conversation_now"])
                    )
                if cross_day_gap:
                    corrections.append("尚未覆盖这些有价值日期: " + ", ".join(time_diag["missing_dates"]))
                retry_prompt = (
                    prompt
                    + "\n\n【时间与覆盖修正要求】" + "；".join(corrections) + "。"
                    + f"请重新输出完整 JSON 数组，最多 {dc} 条；逐轮记录日期只能从 "
                    + (", ".join(recorded_dates) if recorded_dates else "无可用记录日期")
                    + " 中选择，除非对话正文明确给出其他剧情日期并使用 explicit_dialogue。"
                    + "按真实日期、场景、情绪转折和长期影响划分；不可把前一天归到整理执行日，"
                    + "也不可为凑数量重复或切碎同一完整情感弧。"
                    + "不要添加 #保底压缩、#夜间压缩、#自动压缩 等流程标签。"
                )
                retry_text = await self._call_llm_compress(retry_prompt)
                retry_diaries = self._parse_diaries(retry_text)
                retry_diaries = self._normalize_buffer_diary_times(
                    retry_diaries, recorded_dates, labels_by_date
                )
                retry_diag = self._buffer_diary_time_diagnostics(retry_diaries, recorded_dates)
                before_quality = (
                    -len(time_diag["invalid_conversation_now"]),
                    len(time_diag["covered_dates"]),
                    min(len(diaries), dc),
                )
                retry_quality = (
                    -len(retry_diag["invalid_conversation_now"]),
                    len(retry_diag["covered_dates"]),
                    min(len(retry_diaries), dc),
                )
                if retry_diaries and retry_quality > before_quality:
                    self._log_event("compress", f"时间校验重试: {len(diaries)}篇 -> {len(retry_diaries)}篇", {
                        "session": umo[:12], "target": dc, "before": time_diag, "after": retry_diag,
                    })
                    diaries = retry_diaries
                    time_diag = retry_diag
                else:
                    self._log_event("compress", "时间校验重试未改善，保留首轮结果", {
                        "session": umo[:12], "target": dc, "before": time_diag, "retry": retry_diag,
                    })
            except Exception as e:
                logger.warning("[memos-memory] 压缩时间校验重试失败: %s", e)
        unresolved = self._buffer_diary_time_diagnostics(diaries, recorded_dates)
        # Never persist a known-wrong date. With multiple recorded dates there is
        # no deterministic way to guess which one an invalid diary belongs to, so
        # retain the memory but mark its time unknown for later reclassification.
        if unresolved["invalid_conversation_now"]:
            for item in unresolved["invalid_conversation_now"]:
                index = int(item["index"])
                if 0 <= index < len(diaries):
                    diaries[index]["event_date"] = ""
                    diaries[index]["time_label"] = ""
                    diaries[index]["time_basis"] = "unknown"
        final_diag = self._buffer_diary_time_diagnostics(diaries, recorded_dates)
        if final_diag["invalid_conversation_now"] or (
            len(recorded_dates) > 1 and final_diag["missing_dates"]
        ):
            logger.warning("[memos-memory] 压缩时间覆盖仍不完整: %s", final_diag)
            self._log_event("compress", "时间覆盖仍不完整", {
                "session": umo[:12], **final_diag,
            })
        stored_count = 0
        for d in diaries:
            # v1.8.3: use LLM importance if provided, fallback to auto_importance.
            imp = _normalize_importance(d.get("importance"), self._auto_importance(d.get("content", "")))
            if await self._store_one_diary(d, source_session=umo, importance=imp, source_kind=source_kind):
                stored_count += 1
                try:
                    await self._persist_episode_for_diary(
                        d,
                        source_batch_id=source_batch_id,
                        source_kind=source_kind,
                    )
                except Exception as e:
                    # The Memos diary and passage index are already durable. Keep
                    # the request successful and let idempotent migration rebuild
                    # the missing machine view instead of duplicating the diary.
                    logger.warning("[memos-memory][episode] 情景视图写入失败，将由迁移补建: %s", e)
        if stored_count != len(diaries):
            logger.warning("[memos-memory] 压缩持久化不完整: %d/%d，保留原始缓冲等待重试", stored_count, len(diaries))
            self._log_event("compress", f"持久化不完整: {stored_count}/{len(diaries)}", {
                "session": umo[:12], "stored": stored_count, "parsed": len(diaries), "buffer_preserved": True,
            })
            if source_batch_id and self._episodes is not None:
                self._episodes.mark_batch(source_batch_id, "partial")
            return 0
        if source_batch_id and self._episodes is not None:
            self._episodes.mark_batch(source_batch_id, "committed")
        if self.semantic_state_enable and self._episodes is not None:
            state_episodes = self._episodes.episodes_for_batch(source_batch_id) if source_batch_id else diaries
            self._schedule_semantic_state_update(
                state_episodes,
                source_batch_id=source_batch_id,
                reason=f"{source_kind}_compression",
            )
        self._compress_count += 1
        self._last_compress_ts = time.time()
        # Delete only the snapshot that was actually compressed. Messages arriving
        # while the LLM runs have higher seq values and must remain pending.
        try:
            if buffer_up_to_seq is not None and buffer_up_to_seq >= 0:
                await self._vec.buffer_drop(umo, buffer_up_to_seq)
                self._buffer[umo] = await self._vec.buffer_take(umo, last_seq=buffer_up_to_seq)
        except Exception as e:
            logger.debug("[memos-memory] buffer cleanup failed: %s", e)
        logger.info("[memos-memory] 压缩完成: %d篇日记 (session=%s, 累计%d次)", stored_count, umo[:12], self._compress_count)
        self._log_event("compress", f"压缩完成: {stored_count}篇日记", {"session": umo[:12], "count": stored_count, "total": self._compress_count})
        return stored_count

    async def _compress_with_lock(self, umo: str, msgs: list[dict[str, Any]], diary_count: int | None = None,
                                  source_kind: str = "auto", buffer_up_to_seq: int | None = None) -> int:
        lock = self._compress_locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            self._log_event("compress", "skip concurrent compress", {"session": umo[:12], "msg_count": len(msgs)})
            return 0
        async with lock:
            return await self._compress_and_store(
                umo, msgs, diary_count=diary_count, source_kind=source_kind,
                buffer_up_to_seq=buffer_up_to_seq,
            )

    # ---------- on_llm_response: 累积 + 触发 ----------
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        if not self.enable:
            return
        try:
            usage = self._extract_cache_token_usage(response)
            if usage.get("input") or usage.get("cached"):
                cached = int(usage.get("cached") or 0)
                input_tokens = int(usage.get("input") or 0)
                rate = round(cached * 100 / input_tokens, 1) if input_tokens else 0.0
                stat = {
                    "ts": time.time(),
                    "ts_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "session": event.unified_msg_origin,
                    "input_tokens": input_tokens,
                    "cached_tokens": cached,
                    "output_tokens": int(usage.get("output") or 0),
                    "cache_hit_rate": rate,
                    "prefix": self._prefix_cache_stats.get(event.unified_msg_origin, {}),
                }
                self._provider_cache_stats[event.unified_msg_origin] = stat
                self._log_event("cache", f"provider cache tokens {cached}/{input_tokens} ({rate}%)", stat)
        except Exception as e:
            logger.debug("[memos-memory] cache token extraction failed: %s", e)
        if getattr(self, "context_exclude_command_turns", True) and self._event_is_command(event):
            self._remember_command_candidates(
                event.unified_msg_origin, self._command_event_texts(event),
            )
            return
        try:
            await self._xinchao.on_response(event, response)
        except Exception as e:
            logger.warning("[memos-memory][xinchao] response perception scheduling failed: %s", e)
        if not self.enable_auto_compress:
            return
        if not await self._ensure_init() or self._vec is None:
            return
        umo = event.unified_msg_origin
        try:
            user_text = (event.message_str or "").strip()
            asst_text = (getattr(response, "completion_text", "") or "").strip()
        except Exception:
            return
        try:
            turn_time = event.get_extra("memos_memory_request_time", {}) or {}
        except Exception:
            turn_time = getattr(event, "_memos_memory_request_time", {}) or {}
        try:
            turn_event_ts = float(turn_time.get("event_ts") or time.time())
        except (TypeError, ValueError):
            turn_event_ts = time.time()
        turn_timezone = str(turn_time.get("event_timezone") or self.rp_time_timezone)
        message_time = {"event_ts": turn_event_ts, "event_timezone": turn_timezone}
        new_msgs: list[dict[str, Any]] = []
        if user_text:
            new_msgs.append({"role": "user", "content": user_text, **message_time})
        if asst_text:
            # 4.6 source ownership is lossless. Prompt/index limits are applied
            # later as views and child chunks, never to the buffered source turn.
            new_msgs.append({"role": "assistant", "content": asst_text, **message_time})
        if not new_msgs:
            return
        # 落 sqlite(进程重启不丢),也维护内存 _buffer 给 _compress_and_store 同步读
        try:
            await self._vec.buffer_append(umo, new_msgs)
        except Exception as e:
            logger.warning("[memos-memory] buffer 落盘失败: %s", e)
            return
        buf = self._buffer.setdefault(umo, [])
        buf.extend(new_msgs)

        snapshot, snapshot_seq = await self._vec.buffer_snapshot(umo, self.compress_batch_max_messages)
        n_turns = sum(1 for m in snapshot if m["role"] == "assistant")
        if n_turns >= self.compress_every_n_turns:
            lock = self._compress_locks.setdefault(umo, asyncio.Lock())
            if lock.locked():
                self._log_event("compress", "compress already running; keep buffering", {"session": umo[:12], "turns": n_turns})
                return
            asyncio.ensure_future(self._compress_with_lock(umo, snapshot, buffer_up_to_seq=snapshot_seq))

        # v1.7.5: EOD flush moved to background task

    # ---------- enhancer 时间捕获(v1.4.2) ----------
    def _compression_time_context(self, umo: str, source_kind: str) -> dict[str, Any]:
        """Return a fresh request snapshot only for immediate auto compression."""
        if source_kind != "auto":
            return {}
        ctx = dict(self._session_time.get(umo, {}) or {})
        snapshot_ts = float(ctx.get("snapshot_ts") or ctx.get("ts") or 0.0)
        if not snapshot_ts or abs(time.time() - snapshot_ts) > 30 * 60:
            return {}
        return ctx

    def _cache_enhancer_time(self, umo: str, req, request_now: datetime | None = None) -> None:
        """从临时注入里读取本轮现实时间，供压缩和时间感知召回使用。
        同时保存完整公历日期与兼容的月日字段；绝不把历史日记日期读成现在。
        不依赖特定插件的注入格式——尽力提取。
        """
        import re as _re
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        all_text = ""
        for p in parts:
            t = getattr(p, "text", None)
            if t is None and isinstance(p, dict):
                t = p.get("text")
            if t and isinstance(t, str):
                all_text += t + "\n"

        lunar = ""
        period = ""
        # 尝试匹配 enhancer 格式。只在当前行内解析，避免空的“农历:”跨行误吞“对话节奏”。
        m = _re.search(r"(?m)^农历[：:][ \t]*([^\r\n]+)", all_text)
        if m:
            lunar = m.group(1).strip()
            if lunar in {"无", "-", "对话节奏:"} or lunar.startswith("对话节奏"):
                lunar = ""
        # 尝试匹配 时令: 夏 · 深夜
        m = _re.search(r"(?m)^时令[：:][ \t]*(?:[^\r\n·．]+\s*[·．]\s*)?([^\r\n]+)", all_text)
        if m:
            period = m.group(1).strip()
        # 只读取 CurrentTimeContext 内的公历时间，不扫描 HistoricalMemory。
        current_block = ""
        block_m = _re.search(r"(?is)<CurrentTimeContext\b[^>]*>(.*?)</CurrentTimeContext>", all_text)
        if block_m:
            current_block = block_m.group(1)
        m = _re.search(
            r"(?m)^(?:本轮唯一当前时间|当前现实时间|现实时间)[：:][ \t]*(\d{4})-(\d{2})-(\d{2})(?:[ \t]+(\d{2}):(\d{2}))?",
            current_block,
        )
        solar_md = ""
        solar_date = ""
        if m:
            solar_md = f"{int(m.group(2))}月{int(m.group(3))}日"
            solar_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if lunar or period or solar_date:
            self._log_event("enhancer", f"捕获当前时间: 公历={solar_date or '无'} 时段={period or '无'}", 
                           {"lunar": lunar, "period": period, "solar_md": solar_md, "solar_date": solar_date, "session": umo[:12]})
            snapshot_ts = (request_now or self._request_now()).timestamp()
            self._session_time[umo] = {
                "lunar": lunar,
                "period": period,
                "solar_md": solar_md,
                "solar_date": solar_date,
                "ts": snapshot_ts,
                "snapshot_ts": snapshot_ts,
            }



    # ---------- v1.7: dynamic top_k ----------
    def _dynamic_top_k(self) -> int:
        """根据记忆总数动态调整 top_k。"""
        if self._vec is None:
            return self.recall_top_k
        try:
            n = self._vec._connect().execute("SELECT COUNT(DISTINCT memo_name) AS c FROM chunks").fetchone()["c"]
        except Exception:
            return self.recall_top_k
        if n < 100:
            return max(self.recall_top_k, 3)
        elif n < 250:
            return max(self.recall_top_k, 4)
        elif n < 400:
            return max(self.recall_top_k, 5)
        else:
            return max(self.recall_top_k, 5 + (n - 400) // 150)

    @staticmethod
    def _cosine_sim(a, b):
        dot = sum(x*y for x,y in zip(a,b))
        na = sum(x*x for x in a)**0.5
        nb = sum(x*x for x in b)**0.5
        return dot/(na*nb) if na>0 and nb>0 else 0.0

    def _extract_recall_facets(self, text: str) -> dict[str, list[str]]:
        source = " ".join(str(text or "").split())
        facets: dict[str, list[str]] = {"entities": [], "emotion": [], "temporal": [], "relation": [], "key_terms": []}

        def add(kind: str, value: str) -> None:
            value = str(value or "").strip(" ，。！？!?、:：\"'“”‘’()（）[]【】")
            if len(value) < 2 or value in facets[kind]:
                return
            facets[kind].append(value[:40])

        for value in re.findall(r"[“\"《【]([^”\"》】]{2,30})[”\"》】]", source):
            add("entities", value)
        temporal_patterns = (
            r"(?:19|20)\d{2}[-年/.]\d{1,2}(?:[-月/.]\d{1,2}日?)?",
            r"\d{1,2}月\d{1,2}日",
            r"(?:今天|昨天|前天|明天|那天|当晚|第二天|后来|以前|现在|第一次|最后一次|从前|之后|之前|以后)",
            r"(?:清晨|上午|中午|下午|傍晚|晚上|深夜|凌晨)",
        )
        for pattern in temporal_patterns:
            for value in re.findall(pattern, source):
                add("temporal", value)
        emotion_words = (
            "害怕", "恐惧", "难过", "伤心", "失望", "委屈", "生气", "愤怒", "嫉妒", "吃醋",
            "安心", "信任", "依赖", "喜欢", "爱", "想念", "孤独", "后悔", "内疚", "心软", "崩溃",
            "被抛下", "被留下", "舍不得", "不安", "怀疑", "期待", "温柔", "幸福",
        )
        relation_words = (
            "承诺", "约定", "离开", "分开", "分别", "重逢", "和好", "道歉", "原谅", "背叛",
            "相信", "不相信", "陪伴", "保护", "拒绝", "接受", "告白", "称呼", "边界", "关系",
        )
        for word in emotion_words:
            if word in source:
                add("emotion", word)
        for word in relation_words:
            if word in source:
                add("relation", word)
        try:
            import jieba.posseg as pseg
            for word, flag in pseg.cut(source):
                word = word.strip()
                # Only proper names/places/organizations/specific named objects are
                # entities. Generic nouns are kept as key terms and must not create
                # an artificial "necessary diary" explosion.
                if len(word) >= 2 and flag in {"nr", "ns", "nt", "nz"}:
                    add("entities", word)
        except Exception:
            pass
        for term in sorted(self._query_terms(source), key=lambda x: (-len(x), x)):
            if len(facets["key_terms"]) >= 16:
                break
            if any(term in values for key, values in facets.items() if key != "key_terms"):
                continue
            add("key_terms", term)
        return facets

    # ---- 4.6 compatibility facade over split test2 modules -----------

    @staticmethod
    def _resolve_plugin_data_db(filename: str) -> str:
        return resolve_plugin_data_db(filename)

    def _migrate_episode_db_location(self) -> str:
        self.episodic_db_path = migrate_episode_db_location(self.episodic_db_path)
        return self.episodic_db_path

    def _build_query_plan(self, user_query: str, contexts: list[Any] | None) -> dict[str, Any] | None:
        planner = getattr(self, "_query_planner", None) or QueryPlanner(self)
        result = planner.rewrite(user_query, contexts)
        if result is None:
            return None
        return {
            "query": result.search_text,
            "standalone_query": result.search_text,
            "resolved_entities": list(result.resolved_entities),
            "entities": list(result.resolved_entities),
            "relation_cues": list(result.relation_cues),
            "emotion_cues": list(result.emotion_cues),
            "temporal_constraints": list(result.temporal_constraints),
            "intent": result.intent,
            "context_turn_indexes": list(result.context_turn_indexes),
            "confidence": result.confidence,
            "use_context": result.use_context,
            "use_context_reason": result.context_used_reason,
            "context_used_reason": result.context_used_reason,
            "rewritten": result.rewritten,
            "_plan_object": result,
        }

    async def _query_plan_llm_disambiguate(
        self, user_query: str, context_text: str, local_plan: dict[str, Any]
    ) -> dict[str, Any]:
        planner = getattr(self, "_query_planner", None) or QueryPlanner(self)
        plan_obj = local_plan.get("_plan_object")
        if plan_obj is None:
            from .query_planner import QueryPlan
            plan_obj = QueryPlan(
                intent=str(local_plan.get("intent") or "specific"),
                search_text=str(local_plan.get("query") or user_query),
                use_context=bool(local_plan.get("use_context")),
                context_used_reason=str(local_plan.get("context_used_reason") or "specific_query"),
                confidence=float(local_plan.get("confidence") or 0.0),
                resolved_entities=list(local_plan.get("resolved_entities") or []),
                raw_standalone=user_query,
            )
        async def _caller(prompt: str) -> str:
            return await self._call_memory_generation_llm(
                prompt,
                provider_id=self.query_plan_llm_provider_id,
                timeout=self.episode_extraction_timeout,
                label="query_plan",
            )
        updated = await planner.maybe_llm_disambiguate(
            plan_obj, context_text, _caller, build_query_plan_disambiguation_prompt,
        )
        out = dict(local_plan)
        out.update({
            "query": updated.search_text,
            "standalone_query": updated.search_text,
            "resolved_entities": list(updated.resolved_entities),
            "entities": list(updated.resolved_entities),
            "relation_cues": list(updated.relation_cues),
            "emotion_cues": list(updated.emotion_cues),
            "temporal_constraints": list(updated.temporal_constraints),
            "intent": updated.intent,
            "confidence": updated.confidence,
            "use_context": updated.use_context,
            "context_used_reason": updated.context_used_reason,
            "rewritten": updated.rewritten,
            "_plan_object": updated,
        })
        return out

    def _detect_scene_candidates(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        splitter = getattr(self, "_scene_splitter", None) or SceneSplitter(
            gap_seconds=float(getattr(self, "scene_split_gap_seconds", 10800) or 10800),
            max_scenes=int(getattr(self, "diary_count_max_cap", 6) or 6),
        )
        return as_prompt_payload(splitter.detect(messages))

    def _validate_episode_ranges(
        self, episodes: list[dict[str, Any]], messages: list[dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from .scene_splitter import SceneCandidate
        splitter = getattr(self, "_scene_splitter", None) or SceneSplitter(
            gap_seconds=float(getattr(self, "scene_split_gap_seconds", 10800) or 10800),
            max_scenes=int(getattr(self, "diary_count_max_cap", 6) or 6),
        )
        prepared = [SceneCandidate(
            start_turn=int(item.get("start_turn", 0)),
            end_turn=int(item.get("end_turn", 0)),
            reasons=list(item.get("reasons") or []),
            score=float(item.get("score") or 0.0),
        ) for item in (candidates or []) if isinstance(item, dict)]
        report = splitter.validate(episodes, messages, prepared or None)
        # Historical diagnostic compatibility: fixed entries were strings
        # containing the episode key. The splitter itself keeps structured
        # dicts; this facade exposes both in a compact readable form.
        fixed_items = []
        for item in report.get("fixed") or []:
            index = int(item.get("episode_index", -1)) if isinstance(item, dict) else -1
            key = str(episodes[index].get("episode_key") or f"e{index + 1}") if 0 <= index < len(episodes) else "episode"
            fixed_items.append(f"{key}: {item.get('from')} -> {item.get('to')}" if isinstance(item, dict) else str(item))
        report["fixed"] = fixed_items
        return report

    def _plan_episodic_query(self, user_query: str, context_text: str = "") -> dict[str, Any]:
        planner = getattr(self, "_query_planner", None) or QueryPlanner(self)
        plan = planner.plan_for_search(user_query, context_text)
        facets = plan.get("facets") if isinstance(plan.get("facets"), dict) else {}
        legacy_facets = self._extract_recall_facets(user_query)
        # Keep the long-standing facet shape consumed by lean/episodic ranking,
        # while letting QueryPlanner own intent and context decisions.
        merged_facets = {
            key: (list(values) if isinstance(values, (list, tuple, set)) else ([] if not values else [str(values)]))
            for key, values in legacy_facets.items()
        }
        for key, values in facets.items():
            bucket = merged_facets.setdefault(key, [])
            for value in values or []:
                if value not in bucket:
                    bucket.append(value)
        plan["facets"] = merged_facets
        if plan.get("use_context") and context_text and "相关上文" not in str(plan.get("search_text") or ""):
            plan["search_text"] = str(plan.get("search_text") or user_query) + "\n相关上文：" + context_text[-600:]
        narrative = bool(plan.get("narrative") or plan.get("intent") == "narrative")
        target = self.episodic_narrative_inject if narrative else self.episodic_default_inject
        plan["target"] = max(1, int(target))
        plan["candidate_pool"] = max(self.episodic_candidate_pool, int(target) * 3)
        plan["narrative"] = narrative
        plan["temporal"] = bool(plan.get("temporal") or plan.get("intent") == "temporal")
        return plan

    async def _episodic_recall_search(
        self,
        user_query: str,
        context_text: str,
        current_month_day: str,
    ) -> tuple[list[dict], dict[str, Any], dict[str, list[str]]]:
        if self._episodes is None or self._vec is None:
            return [], {"mode": "episodic", "ready": False}, {}
        plan = self._plan_episodic_query(user_query, context_text)
        query_vec = await asyncio.wait_for(
            self._embed(plan["search_text"], timeout=self.recall_embed_timeout),
            timeout=max(2.0, float(self.recall_embed_timeout or 12) + 1.0),
        )
        cards = await asyncio.to_thread(
            self._episodes.search_cards,
            query_vec,
            plan["search_text"],
            plan["candidate_pool"],
        )
        cards = [
            card for card in cards
            if float(card.get("score") or 0.0) >= self.episodic_min_card_score
            or float(card.get("lexical") or 0.0) >= 0.34
        ]
        if not cards:
            return [], {"mode": "episodic", "ready": True, "plan": plan, "cards": 0}, plan["facets"]
        memo_names = {str(card.get("memo_name") or "") for card in cards if card.get("memo_name")}
        passage_hits = await self._vec.search_topk(
            query_vec,
            top_k=max(len(memo_names), plan["target"] * 2),
            min_similarity=min(0.34, float(self.min_similarity_to_inject or 0.52)),
            w_relevance=self.w_relevance,
            bm25_weight=0.24,
            bm25_query=plan["search_text"],
            rerank_fn=None,
            w_importance=self.w_importance,
            w_recency=self.w_recency,
            pin_boost=self.pin_boost,
            time_boost=self.time_boost,
            current_month_day=current_month_day,
            candidate_memo_names=memo_names,
            feedback_query_vec=query_vec,
            feedback_query_text=plan["search_text"],
        )
        passages = {str(hit.get("memo_name") or ""): hit for hit in passage_hits}
        hits: list[dict] = []
        for card in cards:
            memo_name = str(card.get("memo_name") or "")
            passage = passages.get(memo_name)
            meta = self._vec.get_memo_meta(memo_name) or {}
            hit = dict(passage or {})
            if not hit:
                hit = {
                    "memo_name": memo_name,
                    "chunk_text": str(meta.get("chunk_text") or card.get("scene_anchor") or ""),
                    "ts_text": str(meta.get("ts_text") or card.get("occurred_at") or ""),
                    "tags": meta.get("tags") or [],
                    "manual": int(meta.get("manual") or 0),
                    "source_created_ts": float(meta.get("source_created_ts") or 0),
                    "source_updated_ts": float(meta.get("source_updated_ts") or 0),
                    "content_hash": str(meta.get("content_hash") or card.get("diary_content_hash") or ""),
                    "_matched_passages": [],
                }
            passage_relevance = float(hit.get("relevance") or 0.0)
            card_relevance = float(card.get("relevance") or 0.0)
            hit.update({
                "memo_name": memo_name,
                "importance": int(card.get("importance") or meta.get("importance") or 3),
                "memory_type": str(card.get("memory_type") or meta.get("memory_type") or "plot_fact"),
                "long_effect": str(card.get("long_effect") or meta.get("long_effect") or ""),
                "trigger_hint": str(card.get("trigger_hint") or meta.get("trigger_hint") or ""),
                "occurred_at": str(card.get("occurred_at") or meta.get("occurred_at") or ""),
                "event_ts": float(card.get("event_ts") or meta.get("event_ts") or 0),
                "time_basis": str(card.get("time_basis") or meta.get("time_basis") or "unknown"),
                "scene_anchor": str(card.get("scene_anchor") or meta.get("scene_anchor") or ""),
                "retrieval_key": str(card.get("retrieval_key") or meta.get("retrieval_key") or ""),
                "state_change": str(card.get("state_change") or meta.get("state_change") or ""),
                "entities": card.get("entities") or meta.get("entities") or [],
                "relevance": max(card_relevance, passage_relevance),
                "score": float(card.get("score") or 0.0) * 0.72 + float(hit.get("score") or passage_relevance) * 0.28,
                "_episodic": True,
                "_episode_id": str(card.get("episode_id") or ""),
                "_evidence_quality": str(card.get("evidence_quality") or "diary_derived"),
                "_episode_lexical": float(card.get("lexical") or 0.0),
                "_episode_card_score": float(card.get("score") or 0.0),
                "_affect_before": str(card.get("affect_before") or ""),
                "_affect_after": str(card.get("affect_after") or ""),
                "_unresolved": card.get("unresolved") or [],
                "_route_evidence": [{
                    "route": "episode_card",
                    "rank": len(hits) + 1,
                    "relevance": round(card_relevance, 4),
                }],
            })
            hit["score_parts"] = {
                **(hit.get("score_parts") if isinstance(hit.get("score_parts"), dict) else {}),
                "episode_card": round(float(card.get("score") or 0.0), 6),
                "episode_lexical": round(float(card.get("lexical") or 0.0), 6),
                "passage": round(passage_relevance, 6),
            }
            hits.append(hit)
        hits.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("relevance") or 0.0)), reverse=True)
        if self._rerank_provider is not None and hits:
            hits = await self._rerank_fn(plan["search_text"], hits[:max(12, min(20, len(hits)))])
        return hits, {
            "mode": "episodic_cascade",
            "ready": True,
            "plan": plan,
            "cards": len(cards),
            "passage_hits": len(passage_hits),
            "reranked": bool(self._rerank_provider),
            "embedding_queries": 1,
            "month_branch": False,
            "multi_query": False,
        }, plan["facets"]

    def _temporal_query_keys(self, query: str, reference_now: datetime | None = None) -> list[str]:
        source = str(query or "")
        now = reference_now or self._request_now()
        keys: list[str] = []

        def add(value: str) -> None:
            value = str(value or "").strip()
            if value and value not in keys:
                keys.append(value)

        constraint = parse_temporal_constraint(source, now)
        for start, end, _reason in constraint.ranges:
            if start == end:
                add(start.isoformat())
                add(f"{start.year}年{start.month}月{start.day}日")
                add(f"{start.month}月{start.day}日")
            else:
                add(f"{start.year:04d}-{start.month:02d}")
                add(f"{start.year}年{start.month}月")
                if (end.year, end.month) != (start.year, start.month):
                    add(f"{end.year:04d}-{end.month:02d}")
                    add(f"{end.year}年{end.month}月")

        for year, month, day in re.findall(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?", source):
            add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
            add(f"{int(year)}年{int(month)}月{int(day)}日")
            add(f"{int(month)}月{int(day)}日")
        for year, month in re.findall(r"(20\d{2})[-/.年](\d{1,2})(?:月)?", source):
            add(f"{int(year):04d}-{int(month):02d}")
            add(f"{int(year)}年{int(month)}月")
        for month, day in re.findall(r"(?<!\d)(\d{1,2})月(\d{1,2})日", source):
            add(f"{int(month)}月{int(day)}日")
            add(f"-{int(month):02d}-{int(day):02d}")
        relative_days = {"今天": 0, "昨日": -1, "昨天": -1, "前天": -2, "明天": 1}
        for marker, offset in relative_days.items():
            if marker in source:
                from datetime import timedelta
                target = now + timedelta(days=offset)
                add(target.strftime("%Y-%m-%d"))
                add(f"{target.month}月{target.day}日")
        if "去年" in source:
            for month in re.findall(r"去年\s*(\d{1,2})月", source):
                add(f"{now.year - 1:04d}-{int(month):02d}")
                add(f"{now.year - 1}年{int(month)}月")
        return keys[:12]

    async def _lean_recall_search(
        self,
        user_query: str,
        context_text: str,
        current_month_day: str,
        *,
        temporal_enabled: bool | None = None,
        event_index_enabled: bool | None = None,
        source_evidence_enabled: bool | None = None,
        bm25_weight: float = 0.30,
        optimizer_enabled: bool | None = None,
    ) -> tuple[list[dict], dict[str, Any], dict[str, list[str]]]:
        if self._vec is None:
            return [], {"mode": "lean", "ready": False}, {}
        plan = self._plan_episodic_query(user_query, context_text)
        memory_intent = classify_memory_intent(user_query, str(plan.get("intent") or ""))
        plan["memory_intent"] = memory_intent
        plan["target"] = intent_target(
            memory_intent,
            int(getattr(self, "lean_story_normal_inject", 3)),
            self.lean_story_max_inject,
            self.lean_story_min_inject,
        )
        plan["candidate_pool"] = self.lean_recall_candidate_k
        query_vec = await asyncio.wait_for(
            self._embed(plan["search_text"], timeout=self.recall_embed_timeout),
            timeout=max(2.0, float(self.recall_embed_timeout or 12) + 1.0),
        )
        direct_coro = self._vec.search_topk(
            query_vec, top_k=self.lean_recall_candidate_k,
            min_similarity=min(0.36, float(self.min_similarity_to_inject or 0.52)),
            w_relevance=self.w_relevance,
            bm25_weight=max(0.0, min(0.8, float(bm25_weight))),
            bm25_query=plan["search_text"], rerank_fn=None,
            w_importance=self.w_importance, w_recency=self.w_recency,
            pin_boost=self.pin_boost, time_boost=0.0,
            current_month_day=current_month_day,
            feedback_query_vec=query_vec, feedback_query_text=plan["search_text"],
        )
        event_enabled = bool(
            (getattr(self, "lean_event_index_enable", True) if event_index_enabled is None else event_index_enabled)
            and self._episodes is not None
        )
        source_enabled = bool(
            (getattr(self, "lean_source_evidence_enable", True)
             if source_evidence_enabled is None else source_evidence_enabled)
            and self._episodes is not None
        )
        event_limit = min(
            self.lean_recall_candidate_k,
            max(24, self.lean_story_max_inject * 10),
        )
        search_tasks: list[Any] = [direct_coro]
        if event_enabled:
            search_tasks.append(asyncio.to_thread(
                self._episodes.search_cards,
                query_vec, plan["search_text"], event_limit,
            ))
        if source_enabled:
            search_tasks.append(asyncio.to_thread(
                self._episodes.search_source_turns,
                query_vec, plan["search_text"], event_limit,
            ))
        search_results = await asyncio.gather(*search_tasks)
        direct_hits = search_results[0]
        result_index = 1
        if event_enabled:
            event_cards = search_results[result_index]
            result_index += 1
            event_cards = [
                card for card in event_cards
                if float(card.get("score") or 0.0) >= 0.28
                or float(card.get("lexical") or 0.0) >= 0.20
            ]
        else:
            event_cards = []
        if source_enabled:
            source_turn_hits = search_results[result_index]
            source_turn_hits = [
                item for item in source_turn_hits
                if float(item.get("score") or 0.0) >= 0.30
                or float(item.get("lexical") or 0.0) >= 0.24
            ]
        else:
            source_turn_hits = []

        merged: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(direct_hits, 1):
            item = dict(hit)
            item["_route_evidence"] = [{
                "route": "passage_hybrid", "rank": rank,
                "relevance": round(float(item.get("relevance") or 0.0), 4),
                "semantic": round(float(item.get("semantic_relevance") or 0.0), 4),
                "bm25": round(float(item.get("bm25_relevance") or 0.0), 4),
            }]
            item["_passage_hit"] = True
            merged[str(item.get("memo_name") or "")] = item

        # Event cards and local passages are independent recall surfaces. Reuse
        # the same query vector to locate the best paragraph for event-only
        # candidates; this adds no provider embedding or query rewrite.
        event_passages: list[dict[str, Any]] = []
        event_names = {
            str(card.get("memo_name") or "") for card in event_cards if card.get("memo_name")
        }
        if event_names:
            event_passages = await self._vec.search_topk(
                query_vec,
                top_k=min(self.lean_recall_candidate_k, max(len(event_names), self.lean_story_max_inject * 6)),
                min_similarity=0.0,
                w_relevance=self.w_relevance,
                bm25_weight=max(0.0, min(0.8, float(bm25_weight))),
                bm25_query=plan["search_text"], rerank_fn=None,
                w_importance=self.w_importance, w_recency=self.w_recency,
                pin_boost=self.pin_boost, time_boost=0.0,
                current_month_day=current_month_day,
                candidate_memo_names=event_names,
                feedback_query_vec=query_vec, feedback_query_text=plan["search_text"],
            )
        event_passage_map = {
            str(hit.get("memo_name") or ""): hit for hit in event_passages if hit.get("memo_name")
        }
        for rank, card in enumerate(event_cards, 1):
            memo_name = str(card.get("memo_name") or "")
            if not memo_name:
                continue
            existing = merged.get(memo_name)
            passage = event_passage_map.get(memo_name)
            if existing is None:
                meta = self._vec.get_memo_meta(memo_name) or {}
                existing = dict(passage or {})
                if not existing:
                    existing = {
                        "memo_name": memo_name,
                        "chunk_text": str(meta.get("chunk_text") or card.get("scene_anchor") or ""),
                        "ts_text": str(meta.get("ts_text") or card.get("occurred_at") or ""),
                        "tags": meta.get("tags") or [],
                        "manual": int(meta.get("manual") or 0),
                        "source_created_ts": float(meta.get("source_created_ts") or 0),
                        "source_updated_ts": float(meta.get("source_updated_ts") or 0),
                        "content_hash": str(meta.get("content_hash") or card.get("diary_content_hash") or ""),
                        "_matched_passages": [],
                    }
                existing["_route_evidence"] = []
                merged[memo_name] = existing
            elif passage:
                matched = list(existing.get("_matched_passages") or [])
                known = {(item.get("chunk_id"), item.get("passage_index")) for item in matched}
                for item in passage.get("_matched_passages") or []:
                    key = (item.get("chunk_id"), item.get("passage_index"))
                    if key not in known:
                        matched.append(item)
                        known.add(key)
                existing["_matched_passages"] = matched
            existing.setdefault("_route_evidence", []).append({
                "route": "event_card", "rank": rank,
                "relevance": round(float(card.get("relevance") or 0.0), 4),
                "lexical": round(float(card.get("lexical") or 0.0), 4),
            })
            had_passage = bool(existing.get("_passage_hit"))
            card_score = float(card.get("score") or 0.0)
            existing["score"] = max(float(existing.get("score") or 0.0), card_score) + (0.025 if had_passage else 0.0)
            existing["relevance"] = max(
                float(existing.get("relevance") or 0.0), float(card.get("relevance") or 0.0)
            )
            existing.update({
                "memory_type": str(card.get("memory_type") or existing.get("memory_type") or "plot_fact"),
                "importance": int(card.get("importance") or existing.get("importance") or 3),
                "occurred_at": str(card.get("occurred_at") or existing.get("occurred_at") or ""),
                "event_ts": float(card.get("event_ts") or existing.get("event_ts") or 0),
                "time_basis": str(card.get("time_basis") or existing.get("time_basis") or "unknown"),
                "scene_anchor": str(card.get("scene_anchor") or existing.get("scene_anchor") or ""),
                "retrieval_key": str(card.get("retrieval_key") or existing.get("retrieval_key") or ""),
                "state_change": str(card.get("state_change") or existing.get("state_change") or ""),
                "long_effect": str(card.get("long_effect") or existing.get("long_effect") or ""),
                "trigger_hint": str(card.get("trigger_hint") or existing.get("trigger_hint") or ""),
                "entities": card.get("entities") or existing.get("entities") or [],
                "_episodic": True,
                "_episode_id": str(card.get("episode_id") or ""),
                "_evidence_quality": str(card.get("evidence_quality") or "diary_derived"),
                "_affect_before": str(card.get("affect_before") or ""),
                "_affect_after": str(card.get("affect_after") or ""),
                "_unresolved": card.get("unresolved") or [],
                "_event_card_hit": True,
                "_dual_granularity": had_passage,
            })

        # First-hand archived turns are a third independent representation. They
        # can rescue details that neither the literary diary nor its event card
        # retained. Exact evidence links win; batch-level mapping is a fallback.
        source_episode_rescues = 0
        for rank, turn in enumerate(source_turn_hits, 1):
            mappings = self._episodes.episodes_for_source_turn(
                str(turn.get("batch_id") or ""), int(turn.get("turn_index") or 0),
            ) if self._episodes is not None else []
            for mapping in mappings:
                memo_name = str(mapping.get("memo_name") or "")
                if not memo_name:
                    continue
                existing = merged.get(memo_name)
                if existing is None:
                    meta = self._vec.get_memo_meta(memo_name) or {}
                    existing = {
                        "memo_name": memo_name,
                        "chunk_text": str(meta.get("chunk_text") or ""),
                        "ts_text": str(meta.get("ts_text") or mapping.get("occurred_at") or ""),
                        "tags": meta.get("tags") or [],
                        "manual": int(meta.get("manual") or 0),
                        "source_created_ts": float(meta.get("source_created_ts") or 0),
                        "source_updated_ts": float(meta.get("source_updated_ts") or 0),
                        "content_hash": str(meta.get("content_hash") or ""),
                        "_matched_passages": [],
                        "_route_evidence": [],
                    }
                    merged[memo_name] = existing
                    source_episode_rescues += 1
                exact_link = bool(mapping.get("exact_turn_link"))
                batch_link_score = float(mapping.get("batch_link_score") or 0.0)
                source_relevance = float(turn.get("relevance") or 0.0) * (
                    1.0 if exact_link else 0.65 + min(0.25, batch_link_score * 0.25)
                )
                existing.setdefault("_route_evidence", []).append({
                    "route": "source_turn", "rank": rank,
                    "relevance": round(source_relevance, 4),
                    "lexical": round(float(turn.get("lexical") or 0.0), 4),
                    "exact_link": exact_link,
                    "batch_link": round(batch_link_score, 4),
                })
                source_items = existing.setdefault("_source_turn_hits", [])
                signature = (str(turn.get("batch_id") or ""), int(turn.get("turn_index") or 0))
                known = {
                    (str(item.get("batch_id") or ""), int(item.get("turn_index") or 0))
                    for item in source_items
                }
                if signature not in known:
                    source_items.append({
                        "batch_id": signature[0], "turn_index": signature[1],
                        "role": str(turn.get("role") or ""),
                        "content": str(turn.get("content") or ""),
                        "event_ts": float(turn.get("event_ts") or 0),
                        "relevance": round(source_relevance, 6),
                        "lexical": round(float(turn.get("lexical") or 0.0), 4),
                        "exact_link": exact_link,
                    })
                existing["score"] = max(
                    float(existing.get("score") or 0.0), source_relevance + (0.025 if exact_link else 0.0)
                )
                existing["relevance"] = max(
                    float(existing.get("relevance") or 0.0), source_relevance,
                )
                existing["_source_evidence_hit"] = True

        temporal_keys = self._temporal_query_keys(user_query)
        temporal_hits = []
        use_temporal = self.lean_temporal_enable if temporal_enabled is None else bool(temporal_enabled)
        temporal_triggered = bool(use_temporal and plan.get("temporal") and temporal_keys)
        if temporal_triggered:
            temporal_hits = self._vec.search_temporal_keys(
                temporal_keys,
                limit=max(12, self.lean_story_max_inject * 5),
            )
            for rank, hit in enumerate(temporal_hits, 1):
                memo_name = str(hit.get("memo_name") or "")
                existing = merged.get(memo_name)
                route = {
                    "route": "temporal", "rank": rank,
                    "relevance": round(float(hit.get("relevance") or 0.0), 4),
                    "keys": hit.get("_temporal_keys") or [],
                }
                if existing is None:
                    item = dict(hit)
                    item["_route_evidence"] = [route]
                    merged[memo_name] = item
                else:
                    existing.setdefault("_route_evidence", []).append(route)
                    if float(hit.get("relevance") or 0.0) > float(existing.get("relevance") or 0.0):
                        for key in (
                            "chunk_text", "ts_text", "occurred_at", "event_ts", "time_basis",
                            "chunk_id", "passage_index", "char_start", "char_end", "_matched_passages",
                        ):
                            existing[key] = hit.get(key, existing.get(key))
                    existing["relevance"] = max(
                        float(existing.get("relevance") or 0.0), float(hit.get("relevance") or 0.0)
                    )
                    existing["score"] = max(
                        float(existing.get("score") or 0.0), float(hit.get("score") or 0.0)
                    )
                    existing["_temporal_rescue"] = True
        hits = [item for key, item in merged.items() if key]
        for hit in hits:
            if hit.get("_episodic"):
                continue
            memo_name = str(hit.get("memo_name") or "")
            episode = self._episodes.get_episode(memo_name) if self._episodes is not None else None
            if not episode:
                continue
            hit.update({
                "memory_type": str(episode.get("memory_type") or hit.get("memory_type") or "plot_fact"),
                "importance": int(episode.get("importance") or hit.get("importance") or 3),
                "occurred_at": str(episode.get("occurred_at") or hit.get("occurred_at") or ""),
                "event_ts": float(episode.get("event_ts") or hit.get("event_ts") or 0),
                "time_basis": str(episode.get("time_basis") or hit.get("time_basis") or "unknown"),
                "scene_anchor": str(episode.get("scene_anchor") or hit.get("scene_anchor") or ""),
                "retrieval_key": str(episode.get("retrieval_key") or hit.get("retrieval_key") or ""),
                "state_change": str(episode.get("state_change") or hit.get("state_change") or ""),
                "long_effect": str(episode.get("long_effect") or hit.get("long_effect") or ""),
                "trigger_hint": str(episode.get("trigger_hint") or hit.get("trigger_hint") or ""),
                "entities": episode.get("entities") or hit.get("entities") or [],
                "_episodic": True,
                "_episode_id": str(episode.get("episode_id") or ""),
                "_evidence_quality": str(episode.get("evidence_quality") or "diary_derived"),
                "_affect_before": str(episode.get("affect_before") or ""),
                "_affect_after": str(episode.get("affect_after") or ""),
                "_unresolved": episode.get("unresolved") or [],
            })
        hits.sort(
            key=lambda item: (float(item.get("score") or 0.0), float(item.get("relevance") or 0.0)),
            reverse=True,
        )
        if self._rerank_provider is not None and hits:
            hits = await self._rerank_fn(plan["search_text"], hits, pool_limit=self.lean_recall_candidate_k)
        optimizer_on = True if optimizer_enabled is None else bool(optimizer_enabled)
        optimization_diag = optimize_fused_hits(
            user_query,
            hits,
            plan,
            reference_now=self._request_now(),
            cross_layer=optimizer_on
            and getattr(self, "recall_cross_layer_consistency_enable", True),
            temporal=optimizer_on
            and getattr(self, "recall_temporal_constraints_enable", True),
            intent_weights=optimizer_on
            and getattr(self, "recall_intent_layer_weights_enable", True),
        )
        hits.sort(
            key=lambda item: (
                float(item.get("_rerank_score") or item.get("score") or item.get("relevance") or 0.0)
                + float(item.get("_retrieval_bonus") or 0.0),
                float(item.get("relevance") or 0.0),
            ),
            reverse=True,
        )
        return hits, {
            "mode": "lean_full_memory_fusion",
            "ready": True,
            "plan": plan,
            "direct_hits": len(direct_hits),
            "event_cards": len(event_cards),
            "event_passage_hits": len(event_passages),
            "event_only_rescues": len([
                hit for hit in hits if hit.get("_event_card_hit") and not hit.get("_passage_hit")
            ]),
            "source_turn_hits": len(source_turn_hits),
            "source_episode_rescues": source_episode_rescues,
            "source_evidence_candidates": len([
                hit for hit in hits if hit.get("_source_evidence_hit")
            ]),
            "dual_granularity_hits": len([hit for hit in hits if hit.get("_dual_granularity")]),
            "passage_vector_migration": dict(getattr(self, "_passage_vector_migration_state", {})),
            "source_turn_vector_migration": dict(
                getattr(self, "_source_turn_vector_migration_state", {})
            ),
            "semantic_hits": len([hit for hit in direct_hits if float(hit.get("semantic_relevance") or 0) > 0]),
            "lexical_rescues": len([hit for hit in direct_hits if hit.get("lexical_rescue")]),
            "temporal_triggered": temporal_triggered,
            "temporal_keys": temporal_keys,
            "temporal_hits": len(temporal_hits),
            "reranked": bool(self._rerank_provider),
            "rerank_pool": min(len(hits), self.lean_recall_candidate_k) if self._rerank_provider else 0,
            "embedding_queries": 1,
            "multi_query": False,
            "month_branch": False,
            "episode_card_route": event_enabled,
            "source_evidence_route": source_enabled,
            "retrieval_optimizer": optimization_diag,
        }, plan["facets"]

    def _lean_coverage_features(self, hit: dict[str, Any], query_terms: set[str]) -> dict[str, Any]:
        raw_entities = hit.get("entities") or []
        if isinstance(raw_entities, str):
            raw_entities = re.split(r"[,，、|\s]+", raw_entities)
        entities = {str(value).strip().lower() for value in raw_entities if str(value).strip()}
        occurred = str(hit.get("occurred_at") or hit.get("ts_text") or "").strip()
        date_match = re.search(r"(20\d{2})\D+(\d{1,2})(?:\D+(\d{1,2}))?", occurred)
        if date_match:
            year, month, day = date_match.groups()
            date_key = f"{int(year):04d}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")
        else:
            date_key = occurred[:16]
        evidence_text = " ".join(str(hit.get(key) or "") for key in (
            "scene_anchor", "retrieval_key", "state_change", "trigger_hint", "chunk_text",
        ))
        terms = self._query_terms(evidence_text)
        unresolved = {str(value).strip() for value in (hit.get("_unresolved") or []) if str(value).strip()}
        memory_type = str(hit.get("memory_type") or "plot_fact")
        return {
            "date": date_key,
            "entities": entities,
            "terms": terms,
            "query_terms": terms & query_terms,
            "memory_type": memory_type,
            "state_change": bool(str(hit.get("state_change") or "").strip()),
            "promise": memory_type == "promise_or_rule",
            "unresolved": unresolved,
        }

    @staticmethod
    def _lean_fact_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
        left_terms = set(left.get("terms") or set())
        right_terms = set(right.get("terms") or set())
        if not left_terms or not right_terms:
            return 0.0
        return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))

    def _select_lean_coverage(
        self,
        query: str,
        qualified: list[dict[str, Any]],
        plan: dict[str, Any],
        minimum: int,
        maximum: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not qualified or maximum <= 0:
            return [], {"enabled": True, "decisions": []}
        if not getattr(self, "lean_coverage_selection_enable", True):
            selected = qualified[:minimum]
            top_score = float(qualified[0].get("_injection_score") or 0.0)
            relative_floor = max(float(self.recall_injection_min_score or 0.0), top_score - 0.10)
            for hit in qualified[len(selected):maximum]:
                if (
                    float(hit.get("_injection_score") or 0.0) >= relative_floor
                    or hit.get("_temporal_rescue")
                    or plan.get("narrative") and len(selected) < maximum
                ):
                    selected.append(hit)
            return selected, {"enabled": False, "relative_floor": relative_floor, "decisions": []}

        query_terms = self._query_terms(query)
        feature_map = {
            str(hit.get("memo_name") or ""): self._lean_coverage_features(hit, query_terms)
            for hit in qualified
        }
        selected = list(qualified[:minimum])
        top_score = float(qualified[0].get("_injection_score") or 0.0)
        broad = bool(plan.get("narrative") or plan.get("broad") or plan.get("temporal"))
        margin_broad = float(getattr(self, "lean_relative_margin_broad", 0.34))
        margin_normal = float(getattr(self, "lean_relative_margin", 0.24))
        relative_floor = max(
            float(self.recall_injection_min_score or 0.0),
            top_score - (margin_broad if broad else margin_normal),
        )
        decisions: list[dict[str, Any]] = []
        while len(selected) < maximum:
            selected_names = {str(hit.get("memo_name") or "") for hit in selected}
            covered = [feature_map[name] for name in selected_names if name in feature_map]
            covered_dates = {item.get("date") for item in covered if item.get("date")}
            covered_entities = set().union(*(item.get("entities") or set() for item in covered)) if covered else set()
            covered_query_terms = set().union(*(item.get("query_terms") or set() for item in covered)) if covered else set()
            covered_types = {item.get("memory_type") for item in covered if item.get("memory_type")}
            covered_unresolved = set().union(*(item.get("unresolved") or set() for item in covered)) if covered else set()
            ranked: list[tuple[float, bool, dict[str, Any], list[str]]] = []
            for hit in qualified:
                name = str(hit.get("memo_name") or "")
                if name in selected_names:
                    continue
                features = feature_map.get(name) or {}
                base = float(hit.get("_injection_score") or 0.0)
                bonus = 0.0
                reasons: list[str] = []
                new_date = bool(features.get("date") and features.get("date") not in covered_dates)
                if new_date:
                    bonus += 0.08 if broad else 0.035
                    reasons.append("new_date")
                new_entities = set(features.get("entities") or set()) - covered_entities
                if new_entities:
                    bonus += min(0.06, 0.025 + len(new_entities) * 0.01)
                    reasons.append("new_entity")
                new_query_terms = set(features.get("query_terms") or set()) - covered_query_terms
                if new_query_terms:
                    bonus += min(0.07, 0.025 + len(new_query_terms) * 0.012)
                    reasons.append("new_query_fact")
                new_type = features.get("memory_type") not in covered_types
                if new_type:
                    bonus += 0.025
                    reasons.append("new_memory_type")
                if features.get("state_change"):
                    bonus += 0.04
                    reasons.append("state_change")
                if features.get("promise"):
                    bonus += 0.055
                    reasons.append("promise_or_boundary")
                new_unresolved = set(features.get("unresolved") or set()) - covered_unresolved
                if new_unresolved:
                    bonus += 0.045
                    reasons.append("unresolved")
                duplicate_penalty = 0.0
                for old in covered:
                    same_date = bool(features.get("date") and features.get("date") == old.get("date"))
                    overlap = self._lean_fact_overlap(features, old)
                    if same_date and overlap >= 0.72:
                        duplicate_penalty = max(duplicate_penalty, 0.16)
                    elif same_date and overlap >= 0.55:
                        duplicate_penalty = max(duplicate_penalty, 0.08)
                if duplicate_penalty:
                    reasons.append("same_date_same_fact")
                novel_evidence = bool(
                    new_date or new_entities or new_query_terms or new_type
                    or features.get("state_change") or new_unresolved
                )
                protected = bool(
                    hit.get("_temporal_rescue")
                    or hit.get("_source_exact_rescue")
                    or hit.get("_source_direct_rescue")
                    # v4.5: safety-net rescues exist because the main selection
                    # was weak/empty; the relative floor must not re-drop them.
                    or hit.get("_safety_net_rescue")
                    or (features.get("promise") and novel_evidence)
                    or new_unresolved
                    or (features.get("state_change") and novel_evidence)
                    or (broad and new_date)
                )
                utility = base + bonus - duplicate_penalty
                hit["_coverage_hard_duplicate"] = bool(duplicate_penalty >= 0.16 and not novel_evidence)
                ranked.append((utility, protected, hit, reasons))
            if not ranked:
                break
            ranked.sort(
                key=lambda item: (item[0], float(item[2].get("_injection_score") or 0.0)),
                reverse=True,
            )
            utility, protected, candidate, reasons = ranked[0]
            base = float(candidate.get("_injection_score") or 0.0)
            accepted = bool(
                not candidate.get("_coverage_hard_duplicate")
                and (utility >= relative_floor or protected)
            )
            decisions.append({
                "memo": str(candidate.get("memo_name") or ""),
                "base": round(base, 4), "utility": round(utility, 4),
                "protected": protected, "accepted": accepted, "reasons": reasons,
            })
            if not accepted:
                break
            candidate["_coverage_reasons"] = reasons
            candidate["_coverage_protected"] = protected
            selected.append(candidate)
        return selected, {
            "enabled": True,
            "relative_floor": round(relative_floor, 4),
            "covered_dates": len({
                feature_map.get(str(hit.get("memo_name") or ""), {}).get("date")
                for hit in selected
                if feature_map.get(str(hit.get("memo_name") or ""), {}).get("date")
            }),
            "decisions": decisions,
        }

    def _postprocess_lean_hits(
        self,
        query: str,
        hits: list[dict[str, Any]],
        plan: dict[str, Any],
        recent_seen: set[str] | None = None,
    ) -> tuple[list[dict], dict[str, Any], list[dict]]:
        recent_seen = recent_seen or set()
        working = [dict(hit) for hit in hits if hit.get("memo_name")]
        query_terms = self._query_terms(query)
        for hit in working:
            rank_score = float(hit.get("_rerank_score") or 0.0)
            coarse_score = max(
                float(hit.get("relevance") or 0.0),
                float(hit.get("score") or 0.0),
            )
            base = (
                rank_score * 0.78 + coarse_score * 0.22
                if rank_score > 0 and coarse_score > 0
                else (rank_score if rank_score > 0 else coarse_score)
            )
            base += float(hit.get("_retrieval_bonus") or 0.0)
            representation_count = sum(bool(value) for value in (
                hit.get("_passage_hit"), hit.get("_event_card_hit"), hit.get("_source_evidence_hit"),
            ))
            if hit.get("_dual_granularity"):
                representation_count = max(2, representation_count)
            hit["_memory_representation_count"] = representation_count
            consensus_rescue = bool(
                representation_count >= 2
                and base >= max(0.48, float(self.recall_injection_min_score or 0.0) - 0.14)
            )
            source_exact_rescue = bool(
                hit.get("_source_evidence_hit")
                and any(
                    item.get("exact_link") and float(item.get("relevance") or 0.0) >= 0.52
                    for item in (hit.get("_source_turn_hits") or [])
                    if isinstance(item, dict)
                )
            )
            source_direct_rescue = bool(
                hit.get("_source_evidence_hit")
                and any(
                    (
                        float(item.get("relevance") or 0.0) >= 0.58
                        and float(item.get("lexical") or 0.0) >= 0.20
                    )
                    or float(item.get("relevance") or 0.0) >= 0.70
                    for item in (hit.get("_source_turn_hits") or [])
                    if isinstance(item, dict)
                )
            )
            hit["_consensus_rescue"] = consensus_rescue
            hit["_source_exact_rescue"] = source_exact_rescue
            hit["_source_direct_rescue"] = source_direct_rescue
            recently_used = hit.get("memo_name") in recent_seen
            hit["_recent_reuse"] = recently_used
            hit["_injection_score"] = max(0.0, base - (0.08 if recently_used else 0.0))
            hit["_qualified"] = bool(
                (
                    base >= float(self.recall_injection_min_score or 0.0)
                    or consensus_rescue
                    or source_exact_rescue
                    or source_direct_rescue
                    or hit.get("_temporal_rescue")
                    or hit.get("lexical_rescue") and float(hit.get("bm25_relevance") or 0.0) >= 0.58
                    or (
                        hit.get("_safety_net_rescue")
                        and base >= max(
                            float(getattr(self, "min_similarity_to_inject", 0.52) or 0.52),
                            float(self.recall_injection_min_score or 0.0) - 0.08,
                        )
                    )
                )
            )
            if not self.lean_texture_enable and self._memory_layer(hit) == "texture":
                hit_terms = self._query_terms(self._hit_evidence_text(hit))
                direct_texture = bool(
                    hit.get("_temporal_rescue")
                    or hit.get("lexical_rescue")
                    or base >= float(self.recall_injection_min_score or 0.0) + 0.10
                    or len(query_terms & hit_terms) >= 2
                )
                if direct_texture:
                    hit["_texture_direct_rescue"] = True
                else:
                    hit["_qualified"] = False
                    hit["_reject_reason"] = "日常 texture 与本轮没有足够直接证据"
            elif not hit["_qualified"]:
                hit["_reject_reason"] = "低于精简主路资格线"
        qualified = sorted(
            (hit for hit in working if hit.get("_qualified")),
            key=lambda item: float(item.get("_injection_score") or 0.0),
            reverse=True,
        )
        maximum = max(self.lean_story_min_inject, min(self.lean_story_max_inject, int(plan.get("target") or 2)))
        minimum = min(maximum, self.lean_story_min_inject)
        selected, coverage_diag = self._select_lean_coverage(
            query, qualified, plan, minimum, maximum,
        )
        selected_names = {str(hit.get("memo_name") or "") for hit in selected}
        for hit in working:
            hit["_selected"] = str(hit.get("memo_name") or "") in selected_names
            if not hit["_selected"] and hit.get("_qualified") and not hit.get("_reject_reason"):
                hit["_reject_reason"] = "相关但超出少量剧情注入范围"
        return selected, {
            "mode": "lean_relative_margin",
            "candidate_pool": len(working), "qualified": len(qualified), "selected": len(selected),
            "min_inject": minimum, "max_inject": maximum,
            "recent_soft_penalty": 0.08,
            "recent_reused": len([hit for hit in selected if hit.get("_recent_reuse")]),
            "texture_direct_rescued": len([hit for hit in selected if hit.get("_texture_direct_rescue")]),
            "dual_consensus_rescued": len([hit for hit in selected if hit.get("_consensus_rescue")]),
            "source_exact_rescued": len([hit for hit in selected if hit.get("_source_exact_rescue")]),
            "source_direct_rescued": len([hit for hit in selected if hit.get("_source_direct_rescue")]),
            "relative_margin": (
                float(getattr(self, "lean_relative_margin_broad", 0.34))
                if plan.get("narrative") or plan.get("temporal")
                else float(getattr(self, "lean_relative_margin", 0.24))
            ),
            "clusters": False, "information_gain": False,
            "evidence_coverage": coverage_diag,
        }, working

    @staticmethod
    def _merge_safety_net_candidate(existing: dict[str, Any], rescue: dict[str, Any]) -> bool:
        """Merge a wider-route result into the same lean candidate without losing evidence."""
        changed = False
        numeric_max_fields = (
            "score", "relevance", "semantic_relevance", "bm25_relevance",
            "_rerank_score", "_rrf_score", "_rrf_normalized", "_episode_card_score",
        )
        def _number(value: Any) -> float:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        for key in numeric_max_fields:
            old_value = _number(existing.get(key))
            new_value = _number(rescue.get(key))
            if new_value > old_value:
                existing[key] = new_value
                changed = True

        boolean_fields = (
            "lexical_rescue", "_rerank_boost", "_dual_granularity",
            "_passage_hit", "_event_card_hit", "_source_evidence_hit",
            "_temporal_rescue", "_keyword_boost", "_episodic",
        )
        for key in boolean_fields:
            if rescue.get(key) and not existing.get(key):
                existing[key] = True
                changed = True

        list_fields = (
            "_route_evidence", "_matched_passages", "_source_turn_hits",
            "_unresolved",
        )
        for key in list_fields:
            incoming = rescue.get(key)
            if not isinstance(incoming, list) or not incoming:
                continue
            current = existing.get(key)
            merged = list(current) if isinstance(current, list) else []
            signatures = {
                json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                for item in merged
            }
            for item in incoming:
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if signature not in signatures:
                    merged.append(item)
                    signatures.add(signature)
                    changed = True
            existing[key] = merged

        # Keep the richer lean/Episode representation, but fill fields that only
        # the wider route recovered (for example a lexical passage or exact date).
        for key, value in rescue.items():
            if key in numeric_max_fields or key in boolean_fields or key in list_fields:
                continue
            if key.startswith("_injection_") or key in {"_qualified", "_selected", "_reject_reason"}:
                continue
            if value not in (None, "", [], {}) and existing.get(key) in (None, "", [], {}):
                existing[key] = value
                changed = True

        if not existing.get("_safety_net_rescue"):
            existing["_safety_net_rescue"] = True
            changed = True
        for key in ("_injection_score", "_qualified", "_selected", "_reject_reason"):
            existing.pop(key, None)
        return changed

    def _build_recall_routes(self, user_query: str, context_text: str = "") -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        combined = "\n".join(x for x in [user_query.strip(), context_text.strip()] if x)
        facets = self._extract_recall_facets(combined)
        routes: list[dict[str, Any]] = []

        def route(name: str, text: str, weight: float) -> None:
            text = " ".join(str(text or "").split()).strip()
            if not text or any(x["text"] == text for x in routes):
                return
            routes.append({"name": name, "text": text, "weight": weight})

        route("direct", user_query, 1.0)
        if context_text:
            route("context", user_query + " " + context_text, 0.88)
        if facets["entities"] or facets["key_terms"]:
            route("entity", " ".join((facets["entities"] + facets["key_terms"])[:18]), 0.92)
        if facets["emotion"] or facets["relation"]:
            route("relationship", " ".join(facets["emotion"] + facets["relation"] + facets["entities"][:5]), 0.96)
        if facets["temporal"]:
            route("temporal", " ".join(facets["temporal"] + facets["entities"][:6] + facets["relation"][:5]), 1.0)
        if not self.recall_multi_query_enable:
            routes = routes[:1]
        return routes, facets

    def _rrf_merge_hits(self, route_hits: list[tuple[dict[str, Any], list[dict]]]) -> tuple[list[dict], dict[str, Any]]:
        merged: dict[str, dict] = {}
        rrf_k = max(10, int(self.recall_rrf_k or 60))
        for route, hits in route_hits:
            for rank, hit in enumerate(hits, 1):
                memo_name = str(hit.get("memo_name") or "")
                if not memo_name:
                    continue
                contribution = float(route.get("weight") or 1.0) / (rrf_k + rank)
                item = merged.get(memo_name)
                if item is None:
                    item = dict(hit)
                    item["_rrf_score"] = 0.0
                    item["_route_evidence"] = []
                    item["_matched_passages"] = []
                    merged[memo_name] = item
                if float(hit.get("relevance") or 0.0) > float(item.get("relevance") or 0.0):
                    preserved = {k: item[k] for k in ("_rrf_score", "_route_evidence", "_matched_passages")}
                    item.update(hit)
                    item.update(preserved)
                item["_rrf_score"] += contribution
                item["_route_evidence"].append({
                    "route": route.get("name"), "rank": rank,
                    "relevance": round(float(hit.get("relevance") or 0.0), 4),
                })
                passage_key = int(hit.get("chunk_id") or 0) or f"{hit.get('passage_index', 0)}:{hit.get('chunk_text', '')[:40]}"
                if not any(p.get("key") == passage_key for p in item["_matched_passages"]):
                    item["_matched_passages"].append({
                        "key": passage_key,
                        "chunk_id": int(hit.get("chunk_id") or 0),
                        "passage_index": int(hit.get("passage_index") or 0),
                        "char_start": int(hit.get("char_start") or 0),
                        "char_end": int(hit.get("char_end") or 0),
                        "text": hit.get("chunk_text") or "",
                        "route": route.get("name"),
                        "relevance": round(float(hit.get("relevance") or 0.0), 4),
                    })
        if not merged:
            return [], {"routes": [], "merged": 0}
        max_rrf = max(float(x.get("_rrf_score") or 0.0) for x in merged.values()) or 1.0
        out = list(merged.values())
        for item in out:
            normalized = float(item.get("_rrf_score") or 0.0) / max_rrf
            item["_rrf_normalized"] = normalized
            item["score"] = float(item.get("score") or 0.0) + normalized * 0.12
            item["_matched_passages"].sort(key=lambda p: p.get("relevance", 0), reverse=True)
        out.sort(key=lambda x: (float(x.get("_rrf_score") or 0.0), float(x.get("score") or 0.0)), reverse=True)
        return out, {
            "routes": [{"name": r.get("name"), "count": len(h)} for r, h in route_hits],
            "merged": len(out),
            "rrf_k": rrf_k,
        }

    async def _multi_route_search(
        self,
        user_query: str,
        context_text: str,
        candidate_k: int,
        current_month_day: str,
    ) -> tuple[list[dict], dict[str, Any], dict[str, list[str]]]:
        if self._vec is None:
            return [], {"routes": [], "merged": 0}, {}
        routes, facets = self._build_recall_routes(user_query, context_text)
        texts = [r["text"] for r in routes]
        vectors = await asyncio.wait_for(
            self._embed_batch(texts),
            timeout=max(2.0, float(self.recall_embed_timeout or 12) * 1.5),
        )
        route_hits: list[tuple[dict[str, Any], list[dict]]] = []
        direct_query_vec = vectors[0] if vectors else None
        final_similarity = float(self.min_similarity_to_inject or 0.0)
        collection_similarity = (
            max(0.35, final_similarity - 0.12) if final_similarity > 0 else 0.0
        )
        for route, vector in zip(routes, vectors):
            hits = await self._vec.search_topk(
                vector, candidate_k, collection_similarity,
                bm25_query=route["text"], rerank_fn=None,
                w_relevance=self.w_relevance, w_importance=self.w_importance,
                w_recency=self.w_recency, pin_boost=self.pin_boost,
                time_boost=max(self.time_boost or 0, 0)
                    if route["name"] == "temporal" and temporal_anniversary_query(route["text"]) else 0.0,
                current_month_day=current_month_day,
                feedback_query_vec=direct_query_vec,
                feedback_query_text=user_query,
            )
            route_hits.append((route, hits))
        merged, diagnostics = self._rrf_merge_hits(route_hits)
        if self._rerank_provider is not None and merged:
            merged = await self._rerank_fn(user_query + ("\n" + context_text if context_text else ""), merged)
        rescue_gate = max(0.55, final_similarity)
        for hit in merged:
            hit["_rerank_boost"] = float(hit.get("_rerank_score") or 0.0) >= rescue_gate
        before_final_gate = len(merged)
        if final_similarity > 0:
            merged = [
                hit for hit in merged
                if float(hit.get("relevance") or 0.0) >= final_similarity or hit.get("_rerank_boost")
            ]
        diagnostics["query_count"] = len(routes)
        diagnostics["facets"] = facets
        diagnostics["collection_similarity"] = round(collection_similarity, 4)
        diagnostics["final_similarity"] = round(final_similarity, 4)
        diagnostics["before_final_gate"] = before_final_gate
        diagnostics["after_final_gate"] = len(merged)
        return merged, diagnostics, facets

    async def _month_route_search(
        self,
        user_query: str,
        candidate_k: int,
        current_month_day: str,
    ) -> tuple[list[dict], dict[str, Any]]:
        """Independent month branch. It can add candidates but never filters the direct branch."""
        if not self.recall_month_route_enable or self._vec is None:
            return [], {"enabled": False, "months": [], "added": 0}
        query_vec = await asyncio.wait_for(
            self._embed(user_query),
            timeout=max(2.0, float(self.recall_embed_timeout or 12)),
        )
        routes = self._vec.month_route_search(
            query_vec, user_query, limit=self.recall_month_route_count,
        )
        if not routes:
            return [], {"enabled": True, "months": [], "added": 0, "reason": "no_month_route"}
        month_names = [str(route.get("year_month") or "") for route in routes]
        memo_names = self._vec.month_memo_names(month_names)
        if not memo_names:
            return [], {"enabled": True, "months": routes, "added": 0, "reason": "empty_months"}
        final_similarity = float(self.min_similarity_to_inject or 0.0)
        collection_similarity = max(0.35, final_similarity - 0.12) if final_similarity > 0 else 0.0
        hits = await self._vec.search_topk(
            query_vec,
            max(candidate_k, self.recall_month_route_candidate_k),
            collection_similarity,
            bm25_query=user_query,
            rerank_fn=None,
            w_relevance=self.w_relevance,
            w_importance=self.w_importance,
            w_recency=self.w_recency,
            pin_boost=self.pin_boost,
            time_boost=max(self.time_boost or 0, 0) if temporal_anniversary_query(user_query) else 0.0,
            current_month_day=current_month_day,
            candidate_memo_names=memo_names,
            feedback_query_vec=query_vec,
            feedback_query_text=user_query,
        )
        memo_month: dict[str, str] = {}
        for route in routes:
            for memo_name in route.get("source_memos") or []:
                memo_month.setdefault(str(memo_name), str(route.get("year_month") or ""))
        filtered: list[dict] = []
        for rank, hit in enumerate(hits, 1):
            if final_similarity > 0 and float(hit.get("relevance") or 0.0) < final_similarity:
                continue
            month = memo_month.get(str(hit.get("memo_name") or ""), "")
            hit.setdefault("_route_evidence", []).append({
                "route": f"month:{month}" if month else "month",
                "rank": rank,
                "relevance": round(float(hit.get("relevance") or 0.0), 4),
            })
            hit.setdefault("_matched_passages", []).append({
                "key": int(hit.get("chunk_id") or 0),
                "chunk_id": int(hit.get("chunk_id") or 0),
                "passage_index": int(hit.get("passage_index") or 0),
                "char_start": int(hit.get("char_start") or 0),
                "char_end": int(hit.get("char_end") or 0),
                "text": hit.get("chunk_text") or "",
                "route": f"month:{month}" if month else "month",
                "relevance": round(float(hit.get("relevance") or 0.0), 4),
            })
            hit["_month_route"] = month
            filtered.append(hit)
        return filtered, {
            "enabled": True,
            "months": [{
                "year_month": route.get("year_month"),
                "score": round(float(route.get("score") or 0.0), 4),
                "semantic": round(float(route.get("semantic") or 0.0), 4),
                "lexical": round(float(route.get("lexical") or 0.0), 4),
                "exact": bool(route.get("exact")),
                "memo_count": int(route.get("memo_count") or 0),
            } for route in routes],
            "candidate_memos": len(memo_names),
            "branch_hits": len(filtered),
            "collection_similarity": round(collection_similarity, 4),
        }

    @staticmethod
    def _merge_parallel_recall_hits(
        direct_hits: list[dict],
        month_hits: list[dict],
    ) -> tuple[list[dict], dict[str, int]]:
        """Union the branches by memo_name without double scores or duplicate injection."""
        merged = list(direct_hits)
        by_name = {str(hit.get("memo_name") or ""): hit for hit in merged if hit.get("memo_name")}
        duplicates = 0
        added = 0
        for hit in month_hits:
            memo_name = str(hit.get("memo_name") or "")
            if not memo_name:
                continue
            existing = by_name.get(memo_name)
            if existing is not None:
                duplicates += 1
                existing_routes = existing.setdefault("_route_evidence", [])
                for evidence in hit.get("_route_evidence") or []:
                    signature = (evidence.get("route"), evidence.get("rank"))
                    if not any((old.get("route"), old.get("rank")) == signature for old in existing_routes):
                        existing_routes.append(evidence)
                continue
            merged.append(hit)
            hit["_month_route_only"] = True
            by_name[memo_name] = hit
            added += 1
        return merged, {"direct": len(direct_hits), "month": len(month_hits), "added": added, "duplicates": duplicates}

    def _postprocess_parallel_recall_hits(
        self,
        query: str,
        hits: list[dict],
        recent_seen: set[str] | None = None,
    ) -> tuple[list[dict], dict[str, Any], list[dict]]:
        """Select direct hits first, then append month-only hits from a separate quota."""
        direct_hits = [hit for hit in hits if not hit.get("_month_route_only")]
        month_hits = [hit for hit in hits if hit.get("_month_route_only")]
        if not direct_hits and month_hits:
            selected, diagnostics, annotated = self._postprocess_recall_hits(
                query, month_hits, recent_seen,
            )
            diagnostics["direct_selected"] = 0
            diagnostics["month_supplement"] = {
                "candidates": len(month_hits),
                "qualified": int(diagnostics.get("qualified") or 0),
                "selected": len(selected),
                "max": int(self.recall_month_route_inject_max or 0),
                "separate_quota": True,
                "fallback": True,
            }
            return selected, diagnostics, annotated
        direct_selected, direct_diag, direct_annotated = self._postprocess_recall_hits(
            query, direct_hits, recent_seen,
        )
        supplement_max = max(0, int(self.recall_month_route_inject_max or 0))
        if not month_hits or supplement_max <= 0:
            diagnostics = dict(direct_diag)
            diagnostics["month_supplement"] = {
                "candidates": len(month_hits), "qualified": 0, "selected": 0,
                "max": supplement_max, "separate_quota": True,
            }
            return direct_selected, diagnostics, direct_annotated + month_hits

        month_selected_all, month_diag, month_annotated = self._postprocess_recall_hits(
            query, month_hits, recent_seen,
        )
        month_selected = month_selected_all[:supplement_max]
        selected = direct_selected + month_selected
        diagnostics = dict(direct_diag)
        diagnostics.update({
            "candidate_pool": len(hits),
            "qualified": int(direct_diag.get("qualified") or 0) + int(month_diag.get("qualified") or 0),
            "selected": len(selected),
            "folded": int(direct_diag.get("folded") or 0) + int(month_diag.get("folded") or 0),
            "direct_selected": len(direct_selected),
            "month_supplement": {
                "candidates": len(month_hits),
                "qualified": int(month_diag.get("qualified") or 0),
                "eligible": len(month_selected_all),
                "selected": len(month_selected),
                "max": supplement_max,
                "separate_quota": True,
            },
        })
        selected_month_names = {str(hit.get("memo_name") or "") for hit in month_selected}
        for hit in month_annotated:
            if str(hit.get("memo_name") or "") not in selected_month_names:
                hit["_selected"] = False
                if hit.get("_qualified"):
                    hit["_reject_reason"] = "超出月份补充独立名额"
        return selected, diagnostics, direct_annotated + month_annotated

    async def _parallel_recall_search(
        self,
        user_query: str,
        context_text: str,
        candidate_k: int,
        current_month_day: str,
    ) -> tuple[list[dict], dict[str, Any], dict[str, list[str]]]:
        direct_task = asyncio.create_task(
            self._multi_route_search(user_query, context_text, candidate_k, current_month_day)
        )
        month_task = asyncio.create_task(asyncio.wait_for(
            self._month_route_search(user_query, candidate_k, current_month_day),
            timeout=self.recall_month_route_timeout,
        )) if self.recall_month_route_enable else None
        tasks = [direct_task] + ([month_task] if month_task is not None else [])
        results = await asyncio.gather(*tasks, return_exceptions=True)
        direct_result = results[0]
        month_result = results[1] if len(results) > 1 else ([], {"enabled": False, "months": []})
        if isinstance(direct_result, BaseException):
            if isinstance(month_result, BaseException):
                raise direct_result
            month_hits, month_diag = month_result
            month_diag["direct_error"] = str(direct_result)
            return month_hits, {"routes": [], "merged": 0, "month_route": month_diag}, {}
        direct_hits, direct_diag, facets = direct_result
        if isinstance(month_result, BaseException):
            month_hits, month_diag = [], {
                "enabled": True, "months": [], "error": str(month_result), "branch_hits": 0,
            }
        else:
            month_hits, month_diag = month_result
        merged, merge_diag = self._merge_parallel_recall_hits(direct_hits, month_hits)
        direct_diag["month_route"] = month_diag
        direct_diag["parallel_merge"] = merge_diag
        return merged, direct_diag, facets

    async def _eval_month_route(self, query: str) -> dict[str, Any]:
        if not await self._ensure_init() or self._vec is None:
            return {"query": query, "months": [], "error": "index not ready"}
        query_vec = await self._embed(query)
        routes = self._vec.month_route_search(query_vec, query, limit=6)
        return {
            "query": query,
            "months": [{
                "year_month": route.get("year_month"),
                "score": round(float(route.get("score") or 0.0), 4),
                "semantic": round(float(route.get("semantic") or 0.0), 4),
                "lexical": round(float(route.get("lexical") or 0.0), 4),
                "exact": bool(route.get("exact")),
                "memo_count": int(route.get("memo_count") or 0),
                "day_count": int(route.get("day_count") or 0),
            } for route in routes],
        }

    # ---------- v1.7: re-rank ----------
    async def _rerank_fn(self, query: str, candidates: list, pool_limit: int = 20) -> list:
        """Re-rank candidates using AstrBot's native RerankProvider API."""
        if self._rerank_provider is None or not candidates:
            return candidates
        try:
            pool = candidates[:max(1, min(100, int(pool_limit or 20)))]
            docs = [self._hit_evidence_text(c)[:700] for c in pool]
            results = await self._retry(
                lambda: self._rerank_provider.rerank(query=query, documents=docs, top_n=len(pool)),
                "rerank",
                timeout=self.rerank_timeout,
                offload_thread=True,
            )
            if not results:
                return candidates
            # Re-sort pool by rerank score (descending)
            scored = [(r.index, r.relevance_score) for r in results]
            scored.sort(key=lambda x: -x[1])
            reranked = []
            for idx, rerank_score in scored:
                if idx >= len(pool):
                    continue
                candidate = pool[idx]
                candidate["_rerank_score"] = max(0.0, min(1.0, float(rerank_score or 0.0)))
                reranked.append(candidate)
            # Append remaining candidates not in pool
            seen = set(idx for idx, _ in scored if idx < len(pool))
            rest = [c for i, c in enumerate(candidates) if i not in seen]
            self._log_event("recall", f"re-rank: {len(reranked)} candidates re-scored", {
                "top_score": scored[0][1] if scored else 0,
                "pool_size": len(pool),
            })
            return reranked + rest
        except Exception as e:
            logger.debug("[memos-memory] rerank failed: %s", e)
            return candidates

    # ---------- on_llm_request: 检索 + 注入 ----------
    @staticmethod
    def _request_now() -> datetime:
        return datetime.now(timezone.utc)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable:
            return
        request_now = self._request_now()
        request_time_meta = {
            "event_ts": request_now.timestamp(),
            "event_timezone": self.rp_time_timezone,
        }
        try:
            event.set_extra("memos_memory_request_time", request_time_meta)
        except Exception:
            try:
                setattr(event, "_memos_memory_request_time", request_time_meta)
            except Exception:
                pass
        if getattr(self, "context_exclude_command_turns", True):
            if self._event_is_command(event):
                self._prepare_command_request(event, req)
                return
            try:
                self._sanitize_recent_command_context(event, req)
            except Exception as e:
                logger.warning("[memos-memory][context] 指令上下文兜底清理失败，已放行请求: %s", e)
        self._seen_context_sessions.add(event.unified_msg_origin)
        try:
            self._stabilize_system_prompt_request(event, req)
        except Exception as e:
            logger.debug("[memos-memory] system cache guard failed: %s", e)
        try:
            self._govern_request_context(event, req)
        except Exception as e:
            logger.debug("[memos-memory] context governance failed: %s", e)
        try:
            self._rp_enhance_request(event, req, request_now=request_now)
        except Exception as e:
            logger.debug("[memos-memory] built-in rp enhancer failed: %s", e)
        try:
            self._cache_enhancer_time(event.unified_msg_origin, req, request_now=request_now)
        except Exception as e:
            logger.debug("[memos-memory] current time capture failed: %s", e)
        try:
            await self._xinchao.on_request(event, req, now=request_now)
        except Exception as e:
            logger.warning("[memos-memory][xinchao] request injection failed open: %s", e)
        try:
            self._record_cache_prefix_request(event, req)
        except Exception as e:
            logger.debug("[memos-memory] cache prefix snapshot failed: %s", e)
        request_stat = self._request_injection_stat(event, req)
        self._remember_injection_stats(request_stat)
        state_chars = self._inject_semantic_state_for_request(req, request_stat)
        time_insight_result: dict[str, Any] = {"chars": 0, "selected": 0, "mode": "disabled"}
        try:
            time_insight_result = await self._time_insight.on_request(
                event,
                req,
                request_now=request_now,
            )
            time_insight_chars = int(time_insight_result.get("chars") or 0)
            request_stat["time_insight"] = time_insight_result
            request_stat.setdefault("composition", {})["time_insight"] = time_insight_chars
            request_stat["total_est_chars"] = int(request_stat.get("total_est_chars") or 0) + time_insight_chars
            self._xinchao.move_injection_last(req)
        except Exception as e:
            logger.warning("[memos-memory][time-insight] request failed open: %s", e)
            time_insight_chars = 0
        if not self.enable_auto_recall:
            self._finish_request_injection_stat(request_stat, "recall_disabled")
            return
        fast_count = self._local_index_memo_count_fast()
        if fast_count == 0:
            self._log_event("system", "memory recall skipped: local memory index empty", {
                "stage": "pre_init",
                "reason": "skip memory init only; time/context governance already applied",
            })
            self._finish_request_injection_stat(request_stat, "empty_index")
            return
        if not await self._ensure_init():
            self._finish_request_injection_stat(request_stat, "init_failed")
            return
        if state_chars <= 0:
            state_chars = self._inject_semantic_state_for_request(req, request_stat)
        try:
            if self._vec is None or self._vec.distinct_memo_count() <= 0:
                self._log_event("system", "memory recall skipped: local memory index empty", {
                    "stage": "pre_recall",
                    "reason": "avoid memory retrieval before first sync/reindex",
                })
                self._finish_request_injection_stat(request_stat, "empty_index")
                return
        except Exception as e:
            logger.debug("[memos-memory] early memory count check failed: %s", e)
        try:
            query = (event.message_str or "").strip()
        except Exception:
            query = ""
        if not query:
            self._finish_request_injection_stat(request_stat, "empty_query")
            return
        session_key = getattr(req, "session_id", None) or event.unified_msg_origin
        context_query_parts = 0
        contexts_total = 0
        user_query = query
        context_query_text = ""
        query_plan = None
        context_ready = False
        context_used = False
        context_used_reason = "no_candidate"
        try:
            ctxs = self._normalize_contexts(getattr(req, "contexts", None))
            contexts_total = len(ctxs)
            context_ready = bool(ctxs)
            query_plan = self._query_planner.rewrite(user_query, ctxs)
            if query_plan is not None:
                window_texts = [
                    self._ctx_content(item) for item in ctxs[-self.query_plan_context_window:]
                    if self._ctx_role(item) in {"user", "assistant"} and self._ctx_content(item)
                ]
                disambiguation_context = "\n".join(window_texts)[-self.recall_context_query_max_chars:]
                if self.query_plan_llm_disambiguate:
                    async def _query_llm(prompt_text: str) -> str:
                        return await self._call_memory_generation_llm(
                            prompt_text,
                            provider_id=self.query_plan_llm_provider_id or self.episode_extraction_provider_id,
                            timeout=30.0,
                            label="query_plan_disambiguate",
                        )
                    query_plan = await self._query_planner.maybe_llm_disambiguate(
                        query_plan, disambiguation_context, _query_llm,
                        build_query_plan_disambiguation_prompt,
                    )
                query = query_plan.search_text or user_query
                context_used = bool(query_plan.use_context)
                context_used_reason = str(query_plan.context_used_reason or "planned")
                if context_used:
                    context_query_text = disambiguation_context
                    context_query_parts = len(window_texts)
            elif ctxs and self.recall_context_query_messages > 0 and self.recall_context_query_max_chars > 0:
                # Compatibility fallback only when QueryPlanner did not produce a
                # decision. A plan that says false never gets blind full context.
                recent = []
                base_norm = " ".join(query.split())
                budget = self.recall_context_query_max_chars
                for item in reversed(ctxs):
                    if len(recent) >= self.recall_context_query_messages or budget <= 0:
                        break
                    if self._ctx_role(item) not in {"user", "assistant"}:
                        continue
                    clean = " ".join(str(self._ctx_content(item) or "").split())
                    if not clean or clean == base_norm:
                        continue
                    clean = clean[-min(len(clean), budget, 360):]
                    recent.append(clean)
                    budget -= len(clean)
                if recent:
                    recent.reverse()
                    context_query_parts = len(recent)
                    context_query_text = "\n".join(recent)
                    query = user_query + "\n[最近对话线索]\n" + context_query_text
                    context_used = True
                    context_used_reason = "legacy_fallback"
        except Exception as exc:
            logger.debug("[memory][query] planner failed open: %s", exc)
            query = user_query
            context_used = False
            context_used_reason = "planner_failure_local_fallback"
        intent = str(getattr(query_plan, "intent", "specific"))
        confidence = float(getattr(query_plan, "confidence", 0.0) or 0.0)
        resolved_entities = list(getattr(query_plan, "resolved_entities", []) or [])
        logger.info(
            "[memory][query] intent=%s use_context=%s use_context_reason=%s confidence=%.3f",
            intent, context_used, context_used_reason, confidence,
        )
        query_detail = {
            "intent": intent, "confidence": confidence,
            "resolved_entities": resolved_entities,
            "context_ready": context_ready, "context_used": context_used,
            "context_used_reason": context_used_reason,
        }
        request_stat.update(query_detail)
        request_stat["query"] = user_query[:1200]
        request_stat["query_with_context"] = query[:1800]
        self._log_event("recall", f"query: {query[:80]}", {
            "query_len": len(query), "user_input": user_query[:60],
            "context_query_parts": context_query_parts, "contexts_total": contexts_total,
            **query_detail,
        })

        # QueryPlanner's standalone text is authoritative for retrieval. Context
        # is passed separately only when the planner explicitly approved it.
        retrieval_user_query = query
        retrieval_context_text = context_query_text if context_used else ""

        episodic_ready = False
        try:
            if (
                self.episodic_memory_enable
                and self._episodes is not None
                and not self._episode_migration_ready
            ):
                # v4.5.0: give a running bridge a bounded chance to finish so
                # the first turn after reload uses the lean main path instead
                # of the legacy 14-diary fallback.
                await self._wait_episode_migration()
            episodic_ready = bool(
                self.episodic_memory_enable
                and self._episodes is not None
                and self._episode_migration_ready
                and int(self._episodes.stats().get("episodes") or 0) > 0
            )
        except Exception:
            episodic_ready = False
        lean_ready = bool(episodic_ready and self.lean_recall_enable)

        # v1.8: proactive keyword recall
        keyword_hits = []
        try:
            if not episodic_ready and self._vec is not None:
                keyword_hits.extend(
                    self._vec.keywords_find_in_text(
                        query, limit=max(20, self.recall_top_k * 4), min_weight=1.2, min_hits=2
                    )
                )
                words = set(query.split())
                try:
                    if self._vec._bm25 is not None:
                        words.update(self._vec._bm25._tokenize(query, getattr(self._vec._bm25, "_use_jieba", False)))
                    else:
                        import jieba
                        words.update(w.strip() for w in jieba.lcut(query) if w.strip())
                except Exception:
                    for i in range(max(0, len(query) - 1)):
                        words.add(query[i:i + 2])
                seen_keyword_memos = {h["memo_name"] for h in keyword_hits}
                for word in words:
                    if len(word) < 2:
                        continue
                    if word in {"今天", "明天", "昨天", "第一次", "不会", "一起", "感觉", "然后"}:
                        continue
                    matches = self._vec.keywords_find_by_word(word)
                    for kw_m in matches[:2]:
                        if float(kw_m.get("weight") or 0.0) < 1.5:
                            continue
                        if kw_m["memo_name"] not in seen_keyword_memos:
                            keyword_hits.append({"memo_name": kw_m["memo_name"], "keyword": word, "weight": kw_m["weight"]})
                            seen_keyword_memos.add(kw_m["memo_name"])
                if keyword_hits:
                    self._log_event("recall", f"keyword hit: {len(keyword_hits)} memos", {"keywords": [h["keyword"] for h in keyword_hits[:3]]})
        except Exception as e:
            logger.debug("[memos-memory] keyword recall failed: %s", e)

        dynamic_k = self._dynamic_top_k()
        layered_k = max(
            dynamic_k,
            int(self.recall_candidate_pool or 24),
            self.persona_top_k + self.plot_top_k + self.texture_top_k + 3,
        )
        recall_routes_diag: dict[str, Any] = {}
        recall_facets: dict[str, list[str]] = {}
        try:
            time_ctx = self._session_time.get(event.unified_msg_origin, {})
            current_md = str(time_ctx.get("solar_md", "") or "").strip()
            if not current_md:
                now = time.localtime()
                current_md = f"{now.tm_mon}月{now.tm_mday}日"
            if lean_ready:
                self._log_event("recall", "full memory fusion recall start", {
                    "timeout": self.recall_search_timeout,
                    "candidate_pool": self.lean_recall_candidate_k,
                })
                hits, recall_routes_diag, recall_facets = await asyncio.wait_for(
                    self._lean_recall_search(retrieval_user_query, retrieval_context_text, current_md),
                    timeout=max(2.0, float(self.recall_search_timeout or 18)),
                )
                self._log_event("recall", f"lean recall: {len(hits)} candidates", recall_routes_diag)
            elif episodic_ready:
                self._log_event("recall", "4.0 episodic cascade fallback start", {
                    "timeout": self.recall_search_timeout,
                    "candidate_pool": self.episodic_candidate_pool,
                })
                hits, recall_routes_diag, recall_facets = await asyncio.wait_for(
                    self._episodic_recall_search(retrieval_user_query, retrieval_context_text, current_md),
                    timeout=max(2.0, float(self.recall_search_timeout or 18)),
                )
            else:
                self._log_event("recall", "multi-query search start", {
                    "timeout": self.recall_search_timeout, "k": layered_k,
                    "context_parts": context_query_parts,
                })
                hits, recall_routes_diag, recall_facets = await asyncio.wait_for(
                    self._parallel_recall_search(retrieval_user_query, retrieval_context_text, layered_k, current_md),
                    timeout=max(2.0, float(self.recall_search_timeout or 18)),
                )
                self._log_event("recall", f"multi-query merged: {len(hits)} candidates", recall_routes_diag)
        except asyncio.TimeoutError:
            search_mode = "lean_full_memory_fusion" if lean_ready else ("episodic_cascade" if episodic_ready else "multi_query")
            logger.warning("[memos-memory] %s search timeout after %.1fs", search_mode, self.recall_search_timeout)
            self._log_event("recall", f"search timeout: {self.recall_search_timeout}s", {"stage": search_mode})
            if not keyword_hits:
                self._finish_request_injection_stat(request_stat, "search_timeout")
                return
            hits = []
        except Exception as e:
            search_mode = "lean_full_memory_fusion" if lean_ready else ("episodic_cascade" if episodic_ready else "multi_query")
            logger.debug("[memos-memory] %s search failed: %s", search_mode, e)
            self._log_event("recall", f"search failed: {e}", {"stage": search_mode})
            if not keyword_hits:
                self._finish_request_injection_stat(request_stat, "search_failed")
                return
            hits = []
        # v1.8: merge keyword hits
        if keyword_hits and self._vec is not None:
            existing_mn = set(h.get("memo_name") for h in hits)
            for kh in keyword_hits:
                if kh["memo_name"] in existing_mn:
                    keyword_relevance = self._keyword_hit_relevance(kh)
                    existing_hit = next((h for h in hits if h.get("memo_name") == kh["memo_name"]), None)
                    if existing_hit is not None:
                        existing_hit["score"] = float(existing_hit.get("score") or 0.0) + keyword_relevance * 0.05
                        existing_hit.setdefault("_route_evidence", []).append({
                            "route": "keyword", "rank": 1, "relevance": keyword_relevance,
                        })
                        existing_hit.setdefault("diagnostics", {})["keyword_hits"] = int(kh.get("hits") or 1)
                    continue
                if kh["memo_name"] not in existing_mn:
                    try:
                        row = self._vec._connect().execute(
                            """SELECT id AS chunk_id, chunk_text, ts_text, importance, tags, memory_type,
                                      long_effect, trigger_hint, created_ts, occurred_at, event_ts,
                                      time_basis, source_created_ts, source_updated_ts, passage_index,
                                      char_start, char_end, content_hash, scene_anchor, retrieval_key,
                                      state_change, entities
                               FROM chunks WHERE memo_name=? ORDER BY rowid LIMIT 1""",
                            (kh["memo_name"],)).fetchone()
                        if row:
                            keyword_relevance = self._keyword_hit_relevance(kh)
                            hits.append({
                                "memo_name": kh["memo_name"],
                                "chunk_text": row["chunk_text"] or "",
                                "ts_text": row["ts_text"] or "",
                                "tags": row["tags"] or "",
                                "importance": int(row["importance"] or 3),
                                "memory_type": row["memory_type"] or "plot_fact",
                                "long_effect": row["long_effect"] or "",
                                "trigger_hint": row["trigger_hint"] or "",
                                "score": keyword_relevance, "relevance": keyword_relevance,
                                "created_ts": float(row["created_ts"] or 0),
                                "occurred_at": row["occurred_at"] or "",
                                "event_ts": float(row["event_ts"] or 0),
                                "time_basis": row["time_basis"] or "unknown",
                                "source_created_ts": float(row["source_created_ts"] or 0),
                                "source_updated_ts": float(row["source_updated_ts"] or 0),
                                "chunk_id": int(row["chunk_id"] or 0),
                                "passage_index": int(row["passage_index"] or 0),
                                "char_start": int(row["char_start"] or 0),
                                "char_end": int(row["char_end"] or 0),
                                "content_hash": row["content_hash"] or "",
                                "scene_anchor": row["scene_anchor"] or "",
                                "retrieval_key": row["retrieval_key"] or "",
                                "state_change": row["state_change"] or "",
                                "entities": row["entities"] or "",
                                "_matched_passages": [{
                                    "key": int(row["chunk_id"] or 0),
                                    "chunk_id": int(row["chunk_id"] or 0),
                                    "passage_index": int(row["passage_index"] or 0),
                                    "char_start": int(row["char_start"] or 0),
                                    "char_end": int(row["char_end"] or 0),
                                    "text": row["chunk_text"] or "",
                                    "route": "keyword",
                                    "relevance": keyword_relevance,
                                }],
                                "_route_evidence": [{"route": "keyword", "rank": 1, "relevance": keyword_relevance}],
                                "_keyword_boost": True,
                                "score_parts": {"keyword": keyword_relevance},
                                "diagnostics": {
                                    "source": "keyword_index",
                                    "keyword_hits": int(kh.get("hits") or 1),
                                    "keyword_weight": float(kh.get("weight") or 0.0),
                                },
                            })
                    except Exception:
                        pass

        # v4.5 safety net (stage 0): a completely empty lean recall must not
        # short-circuit past the wide rescue below. Run the legacy composite
        # chain once before giving up on this turn.
        safety_net_ran = False
        if not hits and lean_ready and self.recall_safety_net_enable:
            safety_net_ran = True
            try:
                hits, _rescue_routes, _rescue_facets = await asyncio.wait_for(
                    self._parallel_recall_search(retrieval_user_query, retrieval_context_text, layered_k, current_md),
                    timeout=max(2.0, float(self.recall_search_timeout or 18)),
                )
                for item in hits:
                    item["_safety_net_rescue"] = True
                self._log_event("recall", f"safety net: empty lean recall, wide search got {len(hits)}", {
                    "stage": "empty_lean_recall",
                })
            except Exception as exc:
                logger.debug("[memos-memory] safety net wide search failed: %s", exc)

        if not hits:
            self._log_event("recall", "no hits", {"min_sim": self.min_similarity_to_inject, "top_k": dynamic_k})
            if (
                lean_ready
                and getattr(self, "recall_observation_enable", True)
                and self._episodes is not None
            ):
                try:
                    empty_plan = recall_routes_diag.get("plan") if isinstance(recall_routes_diag, dict) else {}
                    self._episodes.record_recall_observation({
                        "request_id": str(request_stat.get("request_id") or ""),
                        "query": retrieval_user_query,
                        "intent": str((empty_plan or {}).get("memory_intent") or (empty_plan or {}).get("intent") or ""),
                        "safety_triggered": bool(safety_net_ran),
                        "safety_reason": "empty_lean_recall" if safety_net_ran else "",
                        "selected_before": [], "selected_after": [], "rescue_selected": [],
                        "outcome": "no_selection",
                    })
                except Exception as exc:
                    logger.debug("[memos-memory][recall] empty observation write failed open: %s", exc)
            self._finish_request_injection_stat(request_stat, "no_hits")
            return

        target_label = (
            f"story={self.lean_story_min_inject}-{self.lean_story_max_inject}"
            if lean_ready else f"top_k={dynamic_k}"
        )
        self._log_event("recall", f"search: {len(hits)} hits ({target_label})", {
            "count": len(hits),
            "results": [
                {"memo": h["memo_name"][:20], "score": round(h["score"], 4), "rel": round(h["relevance"], 4), "imp": h.get("importance", 3)}
                for h in hits
            ],
        })

        # item 4: 滑动窗口去重
        dedup_window = self._deque_factory(maxlen=max(0, self.recall_dedup_window))
        if self.recall_dedup_window > 0:
            prev = self._recent_injected.get(session_key)
            if prev:
                dedup_window.extend(prev)
        seen_in_this_call = set()

        # item 3: 按剧情时间(date_text)倒序重排(v1.2.6 支持 v1.2.5 占位格式)
        # 排序键:(剧情时间 score, relevance, created_ts)全部降序
        # - 剧情时间 score 主导: 完整农历(1M+) > 公历(3M+) > 节气(2M+) > 部分日期+时段(10-20k) > 纯占位(1k) > 未注明(-1)
        # - relevance 次之:同剧情时间组内,更相关的排前
        # - created_ts 兜底:同剧情时间+同相关度时,新入库略前,避免随机
        if self.inject_order == "story_time":
            for h in hits:
                h["_sort_key"] = float(h.get("event_ts") or 0) or parse_story_time(h.get("ts_text", ""))
            hits.sort(
                key=lambda x: (
                    x.get("_sort_key", -1),
                    x.get("relevance", 0),
                    x.get("created_ts", 0),
                ),
                reverse=True,
            )
        elif self.inject_order == "insert_time":
            for h in hits:
                h["_sort_key"] = h.get("source_created_ts") or h.get("created_ts", 0)
            hits.sort(key=lambda x: x.get("_sort_key", 0), reverse=True)

        # Log dedup result
        dedup_selected = [h.get("memo_name", "")[:20] for h in hits if h.get("memo_name", "") not in (dedup_window if self.recall_dedup_window > 0 else set())]
        self._log_event("recall", f"after dedup: {len(dedup_selected)} candidates", {"selected": dedup_selected[:5]})

        recent_seen = set(dedup_window) if self.recall_dedup_window > 0 else set()
        if lean_ready:
            plan = recall_routes_diag.get("plan") if isinstance(recall_routes_diag.get("plan"), dict) else {}
            selection_query = str(plan.get("search_text") or user_query)
            recall_routes_diag["retrieval_optimizer"] = optimize_fused_hits(
                retrieval_user_query,
                hits,
                plan,
                reference_now=request_now,
                cross_layer=getattr(
                    self, "recall_cross_layer_consistency_enable", True
                ),
                temporal=getattr(self, "recall_temporal_constraints_enable", True),
                intent_weights=getattr(
                    self, "recall_intent_layer_weights_enable", True
                ),
            )
            selected_hits, recall_diag, _annotated_hits = self._postprocess_lean_hits(
                selection_query,
                hits,
                plan,
                recent_seen,
            )
            selected_before_safety = [] if safety_net_ran else [
                str(hit.get("memo_name") or "") for hit in selected_hits
                if str(hit.get("memo_name") or "")
            ]
            recall_diag["lean"] = recall_routes_diag
            recall_diag["facets"] = recall_facets
            # v4.5 safety net: if the lean path selected nothing (or a lone weak
            # hit), give the legacy composite chain one shot at rescuing what a
            # thin ANN window may have missed. Costs extra embeddings only on
            # turns that would otherwise inject little or nothing.
            safety_diag: dict[str, Any] = {
                "enabled": bool(self.recall_safety_net_enable), "triggered": safety_net_ran,
            }
            if safety_net_ran:
                safety_diag["reason"] = "empty_lean_recall"
            if self.recall_safety_net_enable and not safety_net_ran:
                top_selected = max(
                    (float(h.get("_injection_score") or 0.0) for h in selected_hits),
                    default=0.0,
                )
                weak = bool(
                    not selected_hits
                    or (
                        len(selected_hits) < self.recall_safety_net_min_selected
                        and top_selected < self.recall_safety_net_min_top_score
                    )
                )
                if weak:
                    safety_diag["triggered"] = True
                    safety_diag["reason"] = "empty_selection" if not selected_hits else "weak_selection"
                    safety_diag["top_selected"] = round(top_selected, 4)
                    self._log_event("recall", "safety net: wide rescue search", safety_diag)
                    rescue_hits: list[dict[str, Any]] = []
                    try:
                        rescue_hits, _rescue_routes, _rescue_facets = await asyncio.wait_for(
                            self._parallel_recall_search(retrieval_user_query, retrieval_context_text, layered_k, current_md),
                            timeout=max(2.0, float(self.recall_search_timeout or 18)),
                        )
                    except Exception as exc:
                        safety_diag["error"] = str(exc)[:160]
                    existing_by_name = {
                        str(h.get("memo_name") or ""): h
                        for h in hits
                        if h.get("memo_name")
                    }
                    added = 0
                    merged = 0
                    for rescue in rescue_hits:
                        memo_name = str(rescue.get("memo_name") or "")
                        if not memo_name:
                            continue
                        existing = existing_by_name.get(memo_name)
                        if existing is not None:
                            if self._merge_safety_net_candidate(existing, rescue):
                                merged += 1
                            continue
                        item = dict(rescue)
                        item["_safety_net_rescue"] = True
                        episode = self._episodes.get_episode(memo_name) if self._episodes is not None else None
                        if episode:
                            item.update({
                                "memory_type": str(episode.get("memory_type") or item.get("memory_type") or "plot_fact"),
                                "importance": int(episode.get("importance") or item.get("importance") or 3),
                                "occurred_at": str(episode.get("occurred_at") or item.get("occurred_at") or ""),
                                "event_ts": float(episode.get("event_ts") or item.get("event_ts") or 0),
                                "time_basis": str(episode.get("time_basis") or item.get("time_basis") or "unknown"),
                                "scene_anchor": str(episode.get("scene_anchor") or item.get("scene_anchor") or ""),
                                "retrieval_key": str(episode.get("retrieval_key") or item.get("retrieval_key") or ""),
                                "state_change": str(episode.get("state_change") or item.get("state_change") or ""),
                                "long_effect": str(episode.get("long_effect") or item.get("long_effect") or ""),
                                "trigger_hint": str(episode.get("trigger_hint") or item.get("trigger_hint") or ""),
                                "entities": episode.get("entities") or item.get("entities") or [],
                                "_episodic": True,
                                "_episode_id": str(episode.get("episode_id") or ""),
                                "_evidence_quality": str(episode.get("evidence_quality") or "diary_derived"),
                                "_affect_before": str(episode.get("affect_before") or ""),
                                "_affect_after": str(episode.get("affect_after") or ""),
                                "_unresolved": episode.get("unresolved") or [],
                            })
                        hits.append(item)
                        existing_by_name[memo_name] = item
                        added += 1
                    safety_diag["rescue_candidates"] = len(rescue_hits)
                    safety_diag["added"] = added
                    safety_diag["merged"] = merged
                    if added or merged:
                        recall_routes_diag["retrieval_optimizer"] = optimize_fused_hits(
                            retrieval_user_query,
                            hits,
                            plan,
                            reference_now=request_now,
                            cross_layer=getattr(
                                self, "recall_cross_layer_consistency_enable", True
                            ),
                            temporal=getattr(
                                self, "recall_temporal_constraints_enable", True
                            ),
                            intent_weights=getattr(
                                self, "recall_intent_layer_weights_enable", True
                            ),
                        )
                        selected_hits, recall_diag, _annotated_hits = self._postprocess_lean_hits(
                            selection_query,
                            hits,
                            plan,
                            recent_seen,
                        )
                        recall_diag["lean"] = recall_routes_diag
                        recall_diag["facets"] = recall_facets
                    safety_diag["selected_after"] = len(selected_hits)
                    self._log_event(
                        "recall",
                        f"safety net: +{added} new / {merged} strengthened, final={len(selected_hits)}",
                        safety_diag,
                    )
            selected_after_safety = [
                str(hit.get("memo_name") or "") for hit in selected_hits
                if str(hit.get("memo_name") or "")
            ]
            rescue_selected = [
                str(hit.get("memo_name") or "") for hit in selected_hits
                if hit.get("_safety_net_rescue") and str(hit.get("memo_name") or "")
            ]
            if safety_diag.get("triggered"):
                if not selected_after_safety:
                    observation_outcome = "no_selection"
                elif rescue_selected or len(selected_after_safety) > len(selected_before_safety):
                    observation_outcome = "helped"
                else:
                    observation_outcome = "no_change"
            else:
                observation_outcome = "not_triggered"
            safety_diag.update({
                "selected_before": len(selected_before_safety),
                "selected_after": len(selected_after_safety),
                "rescue_selected": len(rescue_selected),
                "outcome": observation_outcome,
            })
            if (
                getattr(self, "recall_observation_enable", True)
                and self._episodes is not None
            ):
                try:
                    self._episodes.record_recall_observation({
                        "request_id": str(request_stat.get("request_id") or ""),
                        "query": retrieval_user_query,
                        "intent": str(plan.get("memory_intent") or plan.get("intent") or ""),
                        "safety_triggered": bool(safety_diag.get("triggered")),
                        "safety_reason": str(safety_diag.get("reason") or ""),
                        "rescue_candidates": int(safety_diag.get("rescue_candidates") or 0),
                        "rescue_added": int(safety_diag.get("added") or 0),
                        "rescue_merged": int(safety_diag.get("merged") or 0),
                        "selected_before": selected_before_safety,
                        "selected_after": selected_after_safety,
                        "rescue_selected": rescue_selected,
                        "outcome": observation_outcome,
                    })
                except Exception as exc:
                    logger.debug("[memos-memory][recall] observation write failed open: %s", exc)
            recall_diag["safety_net"] = safety_diag
        elif episodic_ready:
            target = int((recall_routes_diag.get("plan") or {}).get("target") or self.episodic_default_inject)
            selection_query = str((recall_routes_diag.get("plan") or {}).get("search_text") or user_query)
            selected_hits, recall_diag, _annotated_hits = self._postprocess_recall_hits(
                selection_query,
                hits,
                recent_seen,
                limit_override=target,
            )
            recall_diag["episodic"] = recall_routes_diag
            recall_diag["facets"] = recall_facets
        elif self.enable_layered_injection:
            selected_hits, recall_diag, _annotated_hits = self._postprocess_parallel_recall_hits(query, hits, recent_seen)
            recall_diag["multi_query"] = recall_routes_diag
            recall_diag["facets"] = recall_facets
        else:
            direct_hits = [hit for hit in hits if not hit.get("_month_route_only")]
            month_hits = [hit for hit in hits if hit.get("_month_route_only")]
            direct_selected = direct_hits[:max(1, int(self.recall_top_k or 6))]
            month_limit = (
                max(1, int(self.recall_top_k or 6))
                if not direct_hits
                else max(0, int(self.recall_month_route_inject_max or 0))
            )
            month_selected = month_hits[:month_limit]
            selected_hits = direct_selected + month_selected
            recall_diag = {
                "candidate_pool": len(hits), "qualified": len(hits),
                "selected": len(selected_hits), "folded": 0,
                "direct_selected": len(direct_selected),
                "month_supplement": {
                    "candidates": len(month_hits), "qualified": len(month_hits),
                    "selected": len(month_selected),
                    "max": month_limit,
                    "separate_quota": True,
                    "fallback": not bool(direct_hits),
                },
            }
        route_context_used = context_used
        route_context_reason = context_used_reason
        route_plan = recall_routes_diag.get("plan") if isinstance(recall_routes_diag, dict) else None
        if isinstance(route_plan, dict) and "use_context" in route_plan:
            route_context_used = bool(route_plan.get("use_context"))
            route_context_reason = str(route_plan.get("context_used_reason") or "route_plan")
        elif isinstance(recall_routes_diag, dict) and recall_routes_diag.get("multi_query"):
            route_context_used = bool(context_query_text)
            route_context_reason = "multi_query_context" if route_context_used else "multi_query_current_only"
        request_stat["context_used"] = route_context_used
        request_stat["context_used_reason"] = route_context_reason
        recall_diag["context_ready"] = context_ready
        recall_diag["context_used"] = route_context_used
        recall_diag["context_used_reason"] = route_context_reason
        self._log_event(
            "recall",
            f"postprocess: pool={recall_diag.get('candidate_pool')} qualified={recall_diag.get('qualified')} "
            f"folded={recall_diag.get('folded')} final={recall_diag.get('selected')}",
            recall_diag,
        )
        evidence_expansion_names: set[str] = set()
        evidence_expansion_mode = "inactive"
        if lean_ready:
            evidence_hits, evidence_expansion_mode = self._select_lean_evidence_hits(
                user_query, selected_hits,
            )
            evidence_expansion_names = {
                str(hit.get("memo_name") or "") for hit in evidence_hits if hit.get("memo_name")
            }
            recall_diag["source_evidence_expansion"] = {
                "mode": evidence_expansion_mode,
                "selected": len(evidence_expansion_names),
                "memos": sorted(evidence_expansion_names),
                "per_memory_limit": int(self.episodic_evidence_per_memory),
            }
        blocks: list[dict[str, Any]] = []
        total = 0
        injection_modes = {"full": 0, "passage": 0, "compact": 0}
        for rank, h in enumerate(selected_hits):
            mn = h.get("memo_name", "")
            if self.recall_dedup_window > 0 and mn and mn in dedup_window and not lean_ready:
                continue
            content = await self._fetch_memo_content(mn, h.get("chunk_text", ""))
            if not content:
                continue
            use_full, mode_reason = self._should_inject_full_diary(h, content, rank, query)
            passage_detail = None
            inject_content = content
            if not use_full:
                inject_content, passage_detail = self._expanded_matched_passage(h, content)
            mode = "full" if use_full else "passage"
            injection_modes[mode] += 1
            h["_injection_mode"] = mode
            h["_injection_mode_reason"] = mode_reason
            if lean_ready:
                self._prepare_fused_memory_hit(
                    user_query,
                    h,
                    include_source_evidence=mn in evidence_expansion_names,
                )
            block = self._format_inject_block(
                h, inject_content, passage_mode=not use_full, passage_detail=passage_detail,
                reference_now_ts=request_now.timestamp(),
            )
            layer = self._memory_layer(h)
            part_chars = dict(h.get("_injection_part_chars") or {})
            blocks.append({
                "layer": layer, "text": block, "mode": mode, "memo_name": mn,
                "diary_chars": int(part_chars.get("diary") or len(block)),
                "event_core_chars": int(part_chars.get("event_core") or 0),
                "source_evidence_chars": int(part_chars.get("source_evidence") or 0),
                "_hit": h, "_content": content,
            })
            total += len(block)
            if mn and self.recall_dedup_window > 0:
                seen_in_this_call.add(mn)
        # v4.5.1 soft injection target: when the assembled memory set exceeds the
        # target, degrade items from the lowest rank upward (full diary ->
        # passage -> compact excerpt) instead of dropping them. The top-ranked
        # memory always keeps its originally chosen mode, so overflow is valid
        # and is reported explicitly instead of pretending this is a hard cap.
        inject_budget = int(getattr(self, "inject_char_budget", 10000) or 0)
        budget_diag: dict[str, Any] = {
            "budget": inject_budget,
            "soft_target": True,
            "enabled": inject_budget > 0,
            "before": total,
            "demoted": 0,
            "compacted": 0,
            "dropped": 0,
            "preserved_memories": len(blocks),
            "protected_top_chars": len(blocks[0]["text"]) if blocks else 0,
        }
        if inject_budget > 0 and total > inject_budget and len(blocks) > 1:
            def _rebuild(entry: dict[str, Any], *, compact: bool) -> bool:
                nonlocal total
                h = entry["_hit"]
                inject_content, passage_detail = self._expanded_matched_passage(h, entry["_content"])
                new_text = self._format_inject_block(
                    h, inject_content, passage_mode=True, passage_detail=passage_detail,
                    reference_now_ts=request_now.timestamp(), compact_mode=compact,
                )
                if len(new_text) >= len(entry["text"]):
                    return False
                total += len(new_text) - len(entry["text"])
                injection_modes[entry["mode"]] -= 1
                new_mode = "compact" if compact else "passage"
                injection_modes[new_mode] += 1
                part_chars = dict(h.get("_injection_part_chars") or {})
                entry.update({
                    "text": new_text, "mode": new_mode,
                    "diary_chars": int(part_chars.get("diary") or len(new_text)),
                    "event_core_chars": int(part_chars.get("event_core") or 0),
                    "source_evidence_chars": int(part_chars.get("source_evidence") or 0),
                })
                h["_injection_mode"] = new_mode
                h["_injection_mode_reason"] = "budget_compacted" if compact else "budget_demoted"
                return True
            for entry in reversed(blocks[1:]):
                if total <= inject_budget:
                    break
                if entry["mode"] == "full" and _rebuild(entry, compact=False):
                    budget_diag["demoted"] += 1
            for entry in reversed(blocks[1:]):
                if total <= inject_budget:
                    break
                if entry["mode"] != "compact" and _rebuild(entry, compact=True):
                    budget_diag["compacted"] += 1
        budget_diag["after"] = total
        budget_diag["overflow_chars"] = max(0, total - inject_budget) if inject_budget > 0 else 0
        budget_diag["target_met"] = (total <= inject_budget) if inject_budget > 0 else None
        if inject_budget > 0 and budget_diag["before"] > inject_budget:
            outcome = (
                "target met"
                if budget_diag["target_met"]
                else f"soft target exceeded by {budget_diag['overflow_chars']} chars"
            )
            self._log_event("inject", (
                f"soft target: {budget_diag['before']}字 -> {total}字; {outcome} "
                f"(demoted={budget_diag['demoted']} compacted={budget_diag['compacted']}, 未丢弃任何入选记忆)"
            ), budget_diag)
        for entry in blocks:
            entry.pop("_hit", None)
            entry.pop("_content", None)
        if not blocks:
            self._finish_request_injection_stat(request_stat, "no_injectable_blocks")
            return
        if self.recall_dedup_window > 0:
            prev_q = self._recent_injected.get(session_key)
            if prev_q is None:
                prev_q = self._deque_factory(maxlen=self.recall_dedup_window)
            prev_q.extend(seen_in_this_call)
            self._recent_injected[session_key] = prev_q
        try:
            from astrbot.core.agent.message import TextPart
            extra_before = self._extra_parts_text(req)
            context_chars = self._request_context_chars(req)
            rp_stat = self._rp_stats.get(event.unified_msg_origin, {}) if isinstance(self._rp_stats, dict) else {}
            rp_block_chars = rp_stat.get("block_chars", {}) if isinstance(rp_stat, dict) else {}
            current_time_chars = int(rp_block_chars.get("time") or 0) if isinstance(rp_block_chars, dict) else 0
            enhancer_total_chars = int(rp_stat.get("used") or 0) if isinstance(rp_stat, dict) else 0
            enhancer_chars = max(0, enhancer_total_chars - current_time_chars)
            xinchao_chars = sum(
                len(match.group(0))
                for match in re.finditer(r"(?s)<DynamicMindState\b.*?</DynamicMindState>", extra_before)
            )
            other_extra_chars = max(
                0,
                len(extra_before) - enhancer_total_chars - xinchao_chars - time_insight_chars,
            )
            evidence_packet = self._format_episode_evidence_packet(user_query, selected_hits)
            text_block, inject_parts = self._format_injection_with_profile(
                blocks,
                return_parts=True,
                evidence_packet=evidence_packet,
                include_semantic_state=False,
                semantic_state_already_injected=state_chars > 0,
            )
            memory_full_chars = int(inject_parts.get("memory_full", len(text_block)))
            context_stat = self._context_stats.get(event.unified_msg_origin, {}) if isinstance(self._context_stats, dict) else {}
            composition = {
                "current_time": current_time_chars,
                "profile": int(inject_parts.get("profile", 0)),
                "semantic_state": state_chars,
                "time_insight": time_insight_chars + int(inject_parts.get("time_insight", 0)),
                "diary": int(inject_parts.get("diary", total)),
                "event_core": int(inject_parts.get("event_core", 0)),
                "evidence": int(inject_parts.get("evidence", 0)),
                "memory_structure": int(inject_parts.get("memory_structure", 0)),
                "xinchao": xinchao_chars,
                "enhancer": enhancer_chars,
                "context": context_chars,
                "other_extra": other_extra_chars,
            }
            stat = request_stat
            stat.update({
                "session": event.unified_msg_origin,
                "count": len(blocks),
                "chars": memory_full_chars,
                "memo_chars": total,
                "order": self.inject_order,
                "format": self.inject_format,
                "composition": composition,
                "total_est_chars": sum(composition.values()),
                "context_total": context_stat.get("total"),
                "context_kept": context_stat.get("kept"),
                "context_trimmed": context_stat.get("trimmed"),
                "enhancer_blocks": rp_stat.get("injected", []) if isinstance(rp_stat, dict) else [],
                "recall_postprocess": recall_diag,
                "injection_modes": injection_modes,
                "injection_budget": budget_diag,
                "memos": [block.get("memo_name", "") for block in blocks if block.get("memo_name")],
                "preview": (blocks[0]["text"][:80] + "...") if blocks else "",
                "outcome": "injected",
                "duration_ms": round(max(0.0, time.time() - float(request_stat.get("ts") or time.time())) * 1000, 1),
            })
            if getattr(req, "extra_user_content_parts", None) is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(TextPart(text=text_block).mark_as_temp())
            self._time_insight.move_injection_last(req)
            self._xinchao.move_injection_last(req)
            self._remember_injection_stats(stat)
            if int(composition.get("time_insight") or 0) > 0:
                logger.info(
                    "[memos-memory][time] 时间洞察已注入: %d字 | profile=%d diary=%d total=%d",
                    int(composition.get("time_insight") or 0),
                    int(composition.get("profile") or 0),
                    int(composition.get("diary") or 0),
                    int(stat.get("total_est_chars") or 0),
                )
            self._log_event(
                "inject",
                (
                    f"注入 {len(blocks)}条 "
                    f"diary={composition['diary']}字 memory={memory_full_chars}字 "
                    f"event={composition['event_core']}字 evidence={composition['evidence']}字 "
                    f"state={composition['semantic_state']} profile={composition['profile']} current_time={composition['current_time']} "
                    f"time={composition['time_insight']} "
                    f"xinchao={composition['xinchao']} "
                    f"enhancer={composition['enhancer']} "
                    f"context={composition['context']} full={injection_modes['full']} passage={injection_modes['passage']} "
                    f"compact={injection_modes.get('compact', 0)} soft_target={budget_diag['budget']} "
                    f"overflow={budget_diag['overflow_chars']}"
                ),
                stat,
            )

            logger.info(
                "[memos-memory] 注入 %d 条记忆 | diary=%d字 event=%d字 evidence=%d字 state=%d字 memory=%d字 profile=%d current_time=%d time=%d xinchao=%d enhancer=%d context=%d | full=%d passage=%d compact=%d soft_target=%d overflow=%d 顺序=%s 格式=%s",
                len(blocks),
                int(composition.get("diary") or 0),
                int(composition.get("event_core") or 0),
                int(composition.get("evidence") or 0),
                int(composition.get("semantic_state") or 0),
                memory_full_chars,
                int(composition.get("profile") or 0),
                int(composition.get("current_time") or 0),
                int(composition.get("time_insight") or 0),
                int(composition.get("xinchao") or 0),
                int(composition.get("enhancer") or 0),
                int(composition.get("context") or 0),
                injection_modes["full"],
                injection_modes["passage"],
                injection_modes.get("compact", 0),
                int(budget_diag.get("budget") or 0),
                int(budget_diag.get("overflow_chars") or 0),
                self.inject_order,
                self.inject_format,
            )
        except Exception as e:
            logger.debug("[memos-memory] 注入失败: %s", e)
            self._finish_request_injection_stat(request_stat, "inject_failed")

    def _affiliate_profile_status(self) -> dict[str, Any]:
        """Read optional profile affiliate state from the shared sqlite DB."""
        if self._vec is None:
            return {"enabled": self.enable_affiliate_profile, "connected": False, "reason": "vec not ready"}
        try:
            conn = self._vec._connect()
            self._init_profile_schema(conn)
            row = conn.execute(
                "SELECT profile, profile_facts, version, updated_ts, source_count, history_json FROM memory_affiliate_profile WHERE id=1"
            ).fetchone()
            if not row or not (row["profile"] or "").strip():
                return {"enabled": self.enable_affiliate_profile, "connected": False, "reason": "no profile"}
            age_days = (time.time() - float(row["updated_ts"] or 0)) / 86400 if row["updated_ts"] else None
            fresh = age_days is None or self.affiliate_profile_max_age_days <= 0 or age_days <= self.affiliate_profile_max_age_days
            history = []
            if row["history_json"]:
                try:
                    raw_history = json.loads(row["history_json"])
                    if isinstance(raw_history, list):
                        history = raw_history[-12:]
                except Exception:
                    history = []
            runs = []
            try:
                run_rows = conn.execute(
                    "SELECT created_ts, status, message, source_count FROM memory_affiliate_runs ORDER BY created_ts DESC LIMIT 12"
                ).fetchall()
                runs = [
                    {
                        "created_ts": float(r["created_ts"] or 0),
                        "status": r["status"] or "",
                        "message": r["message"] or "",
                        "source_count": int(r["source_count"] or 0),
                    }
                    for r in run_rows
                ]
            except Exception:
                runs = []
            return {
                "enabled": self.enable_affiliate_profile,
                "connected": True,
                "fresh": fresh,
                "age_days": round(age_days, 2) if age_days is not None else None,
                "version": int(row["version"] or 1),
                "updated_ts": float(row["updated_ts"] or 0),
                "source_count": int(row["source_count"] or 0),
                "profile_chars": len(row["profile"] or ""),
                "facts_chars": len(row["profile_facts"] or ""),
                "profile": row["profile"] or "",
                "profile_facts": row["profile_facts"] or "",
                "history": history,
                "history_count": len(history),
                "runs": runs,
            }
        except Exception as e:
            return {"enabled": self.enable_affiliate_profile, "connected": False, "reason": str(e)}

    def _semantic_state_injection_block(self) -> str:
        state_status = self._semantic_state_status() if self.semantic_state_enable else {"ready": False}
        if not state_status.get("ready"):
            return ""
        state = state_status.get("state") if isinstance(state_status.get("state"), dict) else {}
        rendered = str(state.get("rendered_text") or "").strip()
        if not rendered:
            return ""
        return (
            '<CurrentSemanticState role="convergent_current_self">\n'
            "这是角色经历历史后形成的当前关系与心理状态，不是事件清单。"
            "自然表现这些倾向；不要逐条解释，也不要据此虚构新的历史事实。\n"
            + rendered
            + "\n</CurrentSemanticState>"
        )

    def _inject_semantic_state_for_request(
        self,
        req: ProviderRequest,
        request_stat: dict[str, Any],
    ) -> int:
        state_block = self._semantic_state_injection_block()
        if not state_block:
            return 0
        try:
            from astrbot.core.agent.message import TextPart
            if getattr(req, "extra_user_content_parts", None) is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(TextPart(text=state_block).mark_as_temp())
            self._xinchao.move_injection_last(req)
            state_chars = len(state_block)
            request_stat["semantic_state_chars"] = state_chars
            request_stat.setdefault("composition", {})["semantic_state"] = state_chars
            request_stat["total_est_chars"] = int(request_stat.get("total_est_chars") or 0) + state_chars
            return state_chars
        except Exception as exc:
            logger.warning("[memos-memory][state] injection failed open: %s", exc)
            return 0

    def _format_injection_with_profile(
        self,
        blocks: list[dict[str, Any]],
        return_parts: bool = False,
        evidence_packet: str = "",
        include_semantic_state: bool = True,
        semantic_state_already_injected: bool = False,
    ):
        profile_block = ""
        state_block = self._semantic_state_injection_block() if include_semantic_state else ""
        state_active = bool(state_block or semantic_state_already_injected)
        if self.enable_affiliate_profile and not (state_active and self.semantic_state_replace_profile):
            st = self._affiliate_profile_status()
            if st.get("connected") and st.get("fresh", True):
                facts = (st.get("profile_facts") or "").strip()
                profile = (st.get("profile") or "").strip()
                bits = [
                    '<LongTermCharacterState temporal_role="persistent_traits">',
                    "[当前长期人格画像：这是跨时间稳定状态，不提供本轮现实日期，也不表示其中事件刚刚发生]",
                ]
                if profile:
                    bits.append(profile)
                if facts:
                    bits.append("[稳定事实]\n" + facts)
                bits.append("</LongTermCharacterState>")
                profile_block = "\n".join(bits)
        # The integrated v3 service owns request-scoped injection. The shared
        # static block remains as a compatibility path only while the old
        # affiliate plugin is active and the integrated service has yielded.
        insight_block = ""
        if self.enable_time_insight_affiliate and not self._time_insight.owns_injection:
            insight_block = self._time_insight_status().get("injection_block", "")
        if insight_block:
            insight_block = (
                '<HistoricalTimeInsight temporal_role="historical_pattern">\n'
                "以下是跨历史记忆提炼的时间模式，不是当前时间，也不表示旧事件刚刚发生。\n"
                + insight_block
                + "\n</HistoricalTimeInsight>"
            )
        memory_block = self._format_layered_injection(blocks)
        raw_memory_chars = len(memory_block)
        diary_chars = sum(int(block.get("diary_chars") or 0) for block in blocks)
        event_core_chars = sum(int(block.get("event_core_chars") or 0) for block in blocks)
        source_evidence_chars = sum(int(block.get("source_evidence_chars") or 0) for block in blocks)
        known_memory_chars = diary_chars + event_core_chars + source_evidence_chars
        memory_structure_chars = max(0, raw_memory_chars - known_memory_chars)
        if insight_block:
            memory_block = insight_block + "\n\n" + memory_block
        leading = [value for value in (state_block, profile_block) if value]
        text = "\n\n".join(value for value in leading + [memory_block] if value)
        if evidence_packet:
            text = text + "\n\n" + evidence_packet
        if return_parts:
            return text, {
                "profile": len(profile_block),
                "semantic_state": len(state_block),
                "time_insight": len(insight_block),
                "diary": diary_chars or raw_memory_chars,
                "event_core": event_core_chars,
                "source_evidence": source_evidence_chars,
                "memory_structure": memory_structure_chars if diary_chars else 0,
                "evidence": source_evidence_chars + len(evidence_packet),
                "memory_full": len(text),
            }
        return text

    def _time_insight_status(self) -> dict[str, Any]:
        """Read optional companion plugin output for profile evidence/time resonance."""
        if self._vec is None:
            return {"enabled": self.enable_time_insight_affiliate, "connected": False, "reason": "vec not ready"}
        try:
            conn = self._vec._connect()
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_time_insights (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    updated_ts REAL,
                    injection_block TEXT,
                    evidence_json TEXT,
                    stats_json TEXT
                )"""
            )
            row = conn.execute(
                "SELECT updated_ts, injection_block, evidence_json, stats_json FROM memory_time_insights WHERE id=1"
            ).fetchone()
            if not row or not (row["injection_block"] or "").strip():
                return {"enabled": self.enable_time_insight_affiliate, "connected": False, "reason": "no insight"}
            age_days = (time.time() - float(row["updated_ts"] or 0)) / 86400 if row["updated_ts"] else None
            fresh = age_days is None or self.time_insight_max_age_days <= 0 or age_days <= self.time_insight_max_age_days
            evidence = []
            stats = {}
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except Exception:
                evidence = []
            try:
                stats = json.loads(row["stats_json"] or "{}")
            except Exception:
                stats = {}
            return {
                "enabled": self.enable_time_insight_affiliate,
                "connected": True,
                "fresh": fresh,
                "age_days": round(age_days, 2) if age_days is not None else None,
                "updated_ts": float(row["updated_ts"] or 0),
                "injection_block": row["injection_block"] or "",
                "evidence": evidence,
                "stats": stats,
            }
        except Exception as e:
            return {"enabled": self.enable_time_insight_affiliate, "connected": False, "reason": str(e)}

    def _init_profile_schema(self, conn) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_affiliate_profile (
                id INTEGER PRIMARY KEY CHECK (id=1),
                profile TEXT,
                profile_facts TEXT,
                version INTEGER DEFAULT 1,
                updated_ts REAL,
                source_count INTEGER DEFAULT 0,
                history_json TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS memory_affiliate_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts REAL,
                status TEXT,
                message TEXT,
                source_count INTEGER DEFAULT 0
            )"""
        )
        conn.commit()

    async def _profile_auto_loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                if await self._profile_needs_update():
                    await self._profile_update("auto")
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[memos-memory][profile] auto loop failed: %s", e)
                await asyncio.sleep(3600)

    async def _profile_needs_update(self) -> bool:
        if not self.enable_affiliate_profile or self.profile_auto_update_days <= 0 or self._vec is None:
            return False
        try:
            if self._vec.distinct_memo_count() <= 0:
                return False
            conn = self._vec._connect()
            self._init_profile_schema(conn)
            row = conn.execute("SELECT updated_ts FROM memory_affiliate_profile WHERE id=1").fetchone()
            if not row or not row["updated_ts"]:
                return True
            return time.time() - float(row["updated_ts"]) >= self.profile_auto_update_days * 86400
        except Exception:
            return False

    def _local_index_memo_count_fast(self) -> int | None:
        """Read memo count without full plugin initialization. None means unknown/error."""
        try:
            import sqlite3
            path = Path(os.path.expanduser(self.vec_db_path or ""))
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.exists():
                return 0
            conn = sqlite3.connect(str(path), timeout=0.2)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
                ).fetchone()
                if not row:
                    return 0
                row = conn.execute("SELECT COUNT(DISTINCT memo_name) FROM chunks").fetchone()
                return int(row[0] or 0) if row else 0
            finally:
                conn.close()
        except Exception as e:
            logger.debug("[memos-memory] fast local index count failed: %s", e)
            return None

    def _profile_collect_sources(self) -> dict[str, Any]:
        if self._vec is None:
            raise RuntimeError("vec not ready")
        conn = self._vec._connect()
        self._init_profile_schema(conn)
        current = conn.execute("SELECT profile, profile_facts, version FROM memory_affiliate_profile WHERE id=1").fetchone()
        current_profile = current["profile"] if current else ""
        current_facts = current["profile_facts"] if current else ""
        version = int(current["version"] or 0) if current else 0

        def rows(sql: str, params=()):
            try:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            except Exception:
                return []

        persona_types = tuple(_PERSONA_TYPES)
        recent = rows(
            f"""SELECT memo_name, MAX(ts_text) AS ts_text, MAX(importance) AS importance,
                       MAX(memory_type) AS memory_type, MAX(long_effect) AS long_effect,
                       MAX(trigger_hint) AS trigger_hint, MIN(chunk_text) AS chunk_text, MAX(tags) AS tags
                FROM chunks
                WHERE memory_type IN ({",".join("?" for _ in persona_types)})
                GROUP BY memo_name
                ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?""",
            (*persona_types, self.profile_recent_persona_limit),
        )
        important = rows(
            """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(importance) AS importance,
                      MAX(memory_type) AS memory_type, MAX(long_effect) AS long_effect,
                      MAX(trigger_hint) AS trigger_hint, MIN(chunk_text) AS chunk_text, MAX(tags) AS tags
               FROM chunks GROUP BY memo_name HAVING MAX(importance)>=4
               ORDER BY MAX(importance) DESC,
                        COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?""",
            (self.profile_anchor_limit,),
        )
        manual = rows(
            """SELECT memo_name, MAX(ts_text) AS ts_text, MAX(importance) AS importance,
                      MAX(memory_type) AS memory_type, MAX(long_effect) AS long_effect,
                      MAX(trigger_hint) AS trigger_hint, MIN(chunk_text) AS chunk_text, MAX(tags) AS tags
               FROM chunks GROUP BY memo_name HAVING MAX(manual)=1
               ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?""",
            (self.profile_manual_limit,),
        )
        feedback = rows(
            """SELECT c.memo_name, MAX(c.ts_text) AS ts_text, MAX(c.importance) AS importance,
                      MAX(c.memory_type) AS memory_type, MAX(c.long_effect) AS long_effect,
                      MAX(c.trigger_hint) AS trigger_hint, MIN(c.chunk_text) AS chunk_text,
                      MAX(c.tags) AS tags, SUM(f.effect) AS feedback_score
               FROM recall_feedback_events f JOIN chunks c ON c.memo_name=f.memo_name
               WHERE f.action IN ('useful','key') AND f.effect > 0
               GROUP BY c.memo_name HAVING SUM(f.effect) > 0
               ORDER BY SUM(f.effect) DESC,
                        COALESCE(NULLIF(MAX(c.event_ts),0), NULLIF(MAX(c.source_created_ts),0), MAX(c.created_ts)) DESC LIMIT ?""",
            (self.profile_feedback_limit,),
        )
        return {
            "current_profile": current_profile or "",
            "current_facts": current_facts or "",
            "version": version,
            "recent": recent,
            "important": important,
            "manual": manual,
            "feedback": feedback,
        }

    def _profile_format_items(self, title: str, items: list[dict], max_text: int = 320) -> str:
        lines = [f"【{title}】"]
        if not items:
            lines.append("- 无")
            return "\n".join(lines)
        for it in items:
            text = (it.get("long_effect") or it.get("trigger_hint") or it.get("chunk_text") or "").strip()
            lines.append(
                f"- [{it.get('memory_type','')}/★{it.get('importance',3)}] "
                f"{it.get('ts_text','')} {it.get('memo_name','')}: {text[:max_text]}"
            )
        return "\n".join(lines)

    async def _profile_call_llm(self, prompt: str) -> str:
        prov = None
        if self.profile_provider_id:
            try:
                prov = self.context.get_provider_by_id(self.profile_provider_id)
            except Exception as e:
                logger.warning("[memos-memory][profile] provider %s failed: %s", self.profile_provider_id, e)
        if prov is None:
            prov = self.context.get_using_provider()
        if prov is None:
            raise RuntimeError("no LLM provider")
        logger.info("[memos-memory][profile] LLM=%s", _resolve_provider_name(prov))
        resp = await self._retry(
            lambda: prov.text_chat(prompt=prompt, contexts=[], system_prompt=""),
            "profile_llm",
            timeout=max(20.0, float(self.profile_llm_timeout or 90)),
            offload_thread=True,
        )
        return getattr(resp, "completion_text", "") or ""

    def _profile_build_prompt(self, src: dict[str, Any]) -> str:
        character = self.character_name or "角色"
        payload = "\n\n".join([
            self._profile_format_items("最近人格类记忆", src["recent"]),
            self._profile_format_items("高重要度长期记忆", src["important"]),
            self._profile_format_items("手动钉记忆", src["manual"]),
            self._profile_format_items("高反馈记忆", src["feedback"]),
        ])
        return f"""你是长期 RP 角色人格画像维护器。请为 {character} 生成一份“当前长期人格画像”。

这不是原始人设，不要写成角色设定卡；它是长期相处后形成的动态状态。
更新方式是融合：读取旧画像、稳定事实和新记忆后，重写当前画像，而不是追加历史版本。

效果第一：重要的长期变化、承诺、称呼、边界、关键情绪变化、行为倾向不能丢。不要因为追求短而删掉关键内容。
但也不要按月份堆叠，不要逐条复述记忆，不要写“某月画像”。请合并重复倾向。

建议画像约 {self.profile_target_chars} 字；稳定事实约 {self.profile_facts_target_count} 条以内。

旧画像:
{src['current_profile']}

旧稳定事实:
{src['current_facts']}

本次输入记忆:
{payload}

输出 JSON:
{{
  "profile": "当前长期人格画像，强调她现在的内心状态、亲密边界、稳定反应、害怕/渴望/承诺/习惯。",
  "profile_facts": ["稳定事实1", "稳定事实2"],
  "notes": "本次融合更新说明，简短"
}}
"""

    async def _profile_update(self, reason: str) -> dict[str, Any]:
        src = self._profile_collect_sources()
        source_count = sum(len(src[k]) for k in ("recent", "important", "manual", "feedback"))
        if source_count == 0 and not src.get("current_profile"):
            raise RuntimeError("no memory sources")
        text = await self._profile_call_llm(self._profile_build_prompt(src))
        data = _parse_json_object(text)
        if not data:
            raise RuntimeError("profile LLM returned invalid JSON")
        profile = str(data.get("profile") or "").strip()
        facts_raw = data.get("profile_facts") or []
        if isinstance(facts_raw, list):
            facts = "\n".join(f"- {str(x).strip()}" for x in facts_raw if str(x).strip())
        else:
            facts = str(facts_raw or "").strip()
        if not profile:
            raise RuntimeError("empty profile")
        if self._vec is None:
            raise RuntimeError("vec not ready")
        conn = self._vec._connect()
        self._init_profile_schema(conn)
        old = conn.execute("SELECT history_json FROM memory_affiliate_profile WHERE id=1").fetchone()
        history = []
        if old and old["history_json"]:
            try:
                raw_history = json.loads(old["history_json"])
                if isinstance(raw_history, list):
                    history = raw_history
            except Exception:
                history = []
        if src.get("current_profile"):
            history.append({
                "version": int(src.get("version") or 0),
                "updated_ts": time.time(),
                "profile": src.get("current_profile"),
                "profile_facts": src.get("current_facts"),
            })
        history = history[-12:]
        version = int(src.get("version") or 0) + 1
        conn.execute(
            """INSERT OR REPLACE INTO memory_affiliate_profile
               (id, profile, profile_facts, version, updated_ts, source_count, history_json)
               VALUES (1,?,?,?,?,?,?)""",
            (profile, facts, version, time.time(), source_count, json.dumps(history, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO memory_affiliate_runs(created_ts,status,message,source_count) VALUES (?,?,?,?)",
            (time.time(), "ok", f"{reason}: profile v{version}", source_count),
        )
        conn.commit()
        return {
            "version": version,
            "chars": len(profile),
            "facts": facts.count("\n") + (1 if facts else 0),
            "source_count": source_count,
        }

    def _memory_layer(self, hit: dict) -> str:
        raw = str(hit.get("memory_type") or "").strip()
        if raw == "plot_fact" and not hit.get("long_effect") and not hit.get("trigger_hint"):
            mt = _normalize_memory_type("", hit.get("chunk_text", ""), [])
        else:
            mt = _normalize_memory_type(raw, hit.get("chunk_text", ""), [])
        if mt in _PERSONA_TYPES:
            return "persona"
        if mt in _PLOT_TYPES:
            return "plot"
        if mt in _TEXTURE_TYPES:
            return "texture"
        return "plot"

    def _query_terms(self, query: str) -> set[str]:
        query = (query or "").strip()
        terms = {w for w in re.split(r"\s+", query) if len(w) >= 2}
        try:
            import jieba
            terms.update(w.strip() for w in jieba.lcut(query) if len(w.strip()) >= 2)
        except Exception:
            pass
        compact = re.sub(r"\s+", "", query)
        for i in range(max(0, len(compact) - 1)):
            terms.add(compact[i:i + 2])
        return {t for t in terms if t and t not in {"今天", "明天", "昨天", "感觉", "然后", "我们"}}

    @staticmethod
    def _keyword_hit_relevance(hit: dict[str, Any]) -> float:
        """Estimate keyword-only evidence without pretending every match is 0.9 semantic relevance."""
        hit_count = max(1, int(hit.get("hits") or 1))
        weight = max(0.0, float(hit.get("weight") or 0.0))
        relevance = 0.48 + min(4, hit_count) * 0.055 + min(6.0, weight) * 0.015
        if hit_count <= 1:
            relevance = min(relevance, 0.58)
        return round(min(0.82, relevance), 4)

    @staticmethod
    def _term_hits(text: str, terms: set[str], max_hits: int = 4) -> list[str]:
        if not text or not terms:
            return []
        return [t for t in terms if t in text][:max_hits]

    def _annotate_injection_scores(self, query: str, hits: list[dict], recent_seen: set[str] | None = None) -> None:
        terms = self._query_terms(query)
        recent_seen = recent_seen or set()
        for h in hits:
            base = float(h.get("score") or h.get("relevance") or 0.0)
            reasons: list[str] = []
            penalties: list[str] = []
            bonus = 0.0
            trigger_hits = self._term_hits(str(h.get("trigger_hint") or ""), terms, 5)
            effect_hits = self._term_hits(str(h.get("long_effect") or ""), terms, 4)
            tag_hits = self._term_hits(str(h.get("tags") or ""), terms, 4)
            text_hits = self._term_hits(str(h.get("chunk_text") or ""), terms, 4)
            if trigger_hits:
                bonus += 0.18 + min(0.08, len(trigger_hits) * 0.02)
                reasons.append("trigger:" + ",".join(trigger_hits[:3]))
            if effect_hits:
                bonus += 0.09
                reasons.append("long_effect:" + ",".join(effect_hits[:3]))
            if tag_hits:
                bonus += 0.06
                reasons.append("tag:" + ",".join(tag_hits[:3]))
            if text_hits:
                bonus += 0.04
                reasons.append("text:" + ",".join(text_hits[:3]))
            imp = int(h.get("importance") or 3)
            if imp >= 5:
                bonus += 0.10
                reasons.append("importance5")
            if int(h.get("manual") or 0):
                bonus += 0.12
                reasons.append("manual")
            fb = float(h.get("feedback_boost") or 0.0)
            if fb:
                reasons.append(f"query_feedback{fb:+.2f}")
            layer = self._memory_layer(h)
            if layer == "texture" and not (trigger_hits or tag_hits or imp >= 4):
                bonus -= 0.06
                penalties.append("weak_texture")
            if len(str(h.get("chunk_text") or "")) > 900 and imp < 4 and not trigger_hits:
                bonus -= 0.04
                penalties.append("long_weak")
            mn = h.get("memo_name", "")
            if mn and mn in recent_seen:
                bonus -= 0.35
                penalties.append("recent_injected")
            protected = bool(
                int(h.get("manual") or 0)
                or imp >= 5
                or fb >= 0.08
                or trigger_hits
            )
            score = max(0.0, base + bonus)
            h["_layer"] = layer
            h["_injection_score"] = score if self.recall_rerank_enable else base
            h["_injection_reasons"] = reasons
            h["_injection_penalties"] = penalties
            h["_protected"] = protected
            h["_trigger_hits"] = trigger_hits

    def _cluster_candidate_hits(self, hits: list[dict]) -> dict[str, str]:
        names = [h.get("memo_name", "") for h in hits if h.get("memo_name")]
        if not self.recall_cluster_fold_enable or not names or self._vec is None:
            return {n: f"solo:{n}" for n in names}
        cluster_map = {}
        try:
            cluster_map.update(self._vec.similarity_cluster_map(names))
        except Exception as e:
            logger.debug("[memos-memory] similarity cluster map failed: %s", e)
        missing = [n for n in names if n not in cluster_map]
        if not missing:
            return cluster_map

        parent = {n: n for n in missing}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        try:
            threshold = float(self.recall_cluster_similarity or 0.88)
            embeddings = {n: self._vec.get_memo_embedding(n) for n in missing}
            for i, a in enumerate(missing):
                emb_a = embeddings.get(a)
                if not emb_a:
                    continue
                for b in missing[i + 1:]:
                    emb_b = embeddings.get(b)
                    if not emb_b:
                        continue
                    if self._cosine_sim(emb_a, emb_b) >= threshold:
                        union(a, b)
        except Exception as e:
            logger.debug("[memos-memory] temporary candidate cluster failed: %s", e)
        label_by_root: dict[str, str] = {}
        idx = 0
        for n in missing:
            root = find(n)
            if root not in label_by_root:
                idx += 1
                label_by_root[root] = f"tmp-{idx}"
            cluster_map[n] = label_by_root[root]
        return cluster_map

    def _apply_cluster_fold(self, hits: list[dict]) -> tuple[list[dict], dict[str, Any]]:
        enabled = bool(self.recall_cluster_fold_enable)
        apply = bool(self.recall_cluster_fold_apply)
        if not enabled or not apply:
            for h in hits:
                h["_cluster_id"] = ""
                h["_folded"] = False
            return hits, {
                "enabled": enabled,
                "apply": apply,
                "folded": 0,
                "groups": {},
                "reason": "disabled" if not enabled else "diagnostic_only",
            }
        cluster_map = self._cluster_candidate_hits(hits)
        base_per = max(1, int(self.recall_cluster_base_per_group or 2))
        allow_protected = bool(self.recall_cluster_allow_protected)
        kept: list[dict] = []
        counts: dict[str, int] = {}
        group_sizes: dict[str, int] = {}
        folded = 0
        for h in hits:
            cid = cluster_map.get(h.get("memo_name", ""), "")
            h["_cluster_id"] = cid
            h["_folded"] = False
            if cid:
                group_sizes[cid] = group_sizes.get(cid, 0) + 1
        for h in hits:
            cid = h.get("_cluster_id") or ""
            count = counts.get(cid, 0)
            protected = bool(h.get("_protected"))
            if not cid or count < base_per or (allow_protected and protected):
                kept.append(h)
                if cid:
                    counts[cid] = count + 1
                continue
            h["_folded"] = True
            h["_reject_reason"] = f"同相似簇已保留{base_per}篇"
            folded += 1
        return kept, {
            "enabled": True,
            "apply": bool(self.recall_cluster_fold_apply),
            "base_per_group": base_per,
            "similarity": float(self.recall_cluster_similarity or 0.88),
            "folded": folded,
            "groups": group_sizes,
        }

    @staticmethod
    def _hit_evidence_text(hit: dict) -> str:
        base = " ".join(str(hit.get(key) or "") for key in (
            "retrieval_key", "scene_anchor", "state_change", "entities", "trigger_hint",
            "long_effect", "tags", "chunk_text", "ts_text", "occurred_at",
        ))
        source = " ".join(
            str(item.get("content") or "")
            for item in (hit.get("_source_turn_hits") or [])[:3]
            if isinstance(item, dict)
        )
        return (base + " " + source).strip()

    def _hit_facet_coverage(self, hit: dict, facets: dict[str, list[str]]) -> set[tuple[str, str]]:
        text = self._hit_evidence_text(hit)
        covered: set[tuple[str, str]] = set()

        def calendar_key(value: str) -> tuple[int, int, int] | tuple[int, int] | None:
            value = str(value or "")
            full = re.search(r"((?:19|20)\d{2})\D+(\d{1,2})\D+(\d{1,2})", value)
            if full:
                return int(full.group(1)), int(full.group(2)), int(full.group(3))
            month_day = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
            if month_day:
                return int(month_day.group(1)), int(month_day.group(2))
            return None

        hit_calendar_keys: set[tuple] = set()
        for field in (hit.get("occurred_at"), hit.get("ts_text")):
            key = calendar_key(str(field or ""))
            if key:
                hit_calendar_keys.add(key)
                if len(key) == 3:
                    hit_calendar_keys.add(key[1:])
        for kind, values in facets.items():
            for value in values:
                matched = bool(value and value in text)
                if kind == "temporal" and not matched:
                    key = calendar_key(value)
                    matched = bool(key and key in hit_calendar_keys)
                if matched:
                    covered.add((kind, value))
        return covered

    def _select_information_gain(
        self,
        query: str,
        hits: list[dict],
        target: int,
    ) -> tuple[list[dict], dict[str, Any]]:
        """Protect necessary diaries first, then fill remaining places by marginal evidence gain."""
        if not hits:
            return [], {"enabled": True, "necessary": 0, "target": 0, "coverage": []}
        facets = self._extract_recall_facets(query)
        priority = {"temporal": 0, "entities": 1, "relation": 2, "emotion": 3, "key_terms": 4}
        coverage = {h.get("memo_name", ""): self._hit_facet_coverage(h, facets) for h in hits}
        required_facets: list[tuple[str, str]] = []
        for kind in ("temporal", "entities", "relation", "emotion"):
            for value in facets.get(kind, []):
                if any((kind, value) in values for values in coverage.values()):
                    required_facets.append((kind, value))
        # Longer direct terms can carry a unique object/action not recognized by the typed lists.
        for value in facets.get("key_terms", []):
            belongs_to_date = value.isdigit() or any(
                value in temporal for temporal in facets.get("temporal", [])
            )
            if not belongs_to_date and len(value) >= 3 and any(("key_terms", value) in values for values in coverage.values()):
                required_facets.append(("key_terms", value))
            if len(required_facets) >= 12:
                break
        required_facets = sorted(set(required_facets), key=lambda x: (priority.get(x[0], 9), -len(x[1])))[:12]

        selected: list[dict] = []
        selected_names: set[str] = set()
        protected_reasons: dict[str, list[str]] = {}

        def base_utility(hit: dict) -> float:
            return (
                float(hit.get("_injection_score") or hit.get("score") or 0.0)
                + float(hit.get("_rrf_normalized") or 0.0) * 0.10
                + min(0.08, len(hit.get("_matched_passages") or []) * 0.02)
            )

        scope_query = any(word in query for word in (
            "都", "全部", "所有", "哪些", "几次", "每次", "过程", "完整", "从头", "来龙去脉",
        ))
        narrative_query = any(word in query for word in (
            "为什么", "怎么", "后来", "之后", "以前", "过程", "发生了什么", "从那以后", "来龙去脉",
        ))

        def event_signature(hit: dict) -> tuple[str, str, str]:
            return (
                str(hit.get("occurred_at") or hit.get("ts_text") or ""),
                str(hit.get("scene_anchor") or hit.get("chunk_text") or "")[:80],
                str(hit.get("state_change") or "")[:100],
            )

        # Protect direct answer evidence before any marginal-gain calculation. An
        # explicit calendar date is a scope, so all distinct matching events may be
        # necessary; named entities and relations normally lock one best diary,
        # expanding only when the user explicitly asks for a complete account.
        for kind, value in required_facets:
            candidates = [h for h in hits if (kind, value) in coverage.get(h.get("memo_name", ""), set())]
            if not candidates:
                continue
            candidates.sort(key=lambda h: (base_utility(h), float(h.get("relevance") or 0.0)), reverse=True)
            exact_calendar = kind == "temporal" and bool(re.search(
                r"(?:19|20)\d{2}|\d{1,2}月\d{1,2}日|\d{1,2}[-/.]\d{1,2}", value,
            ))
            protect_limit = len(candidates) if exact_calendar else (3 if scope_query else 1)
            seen_signatures: set[tuple[str, str, str]] = set()
            for candidate in candidates:
                signature = event_signature(candidate)
                if signature in seen_signatures and any(signature):
                    continue
                seen_signatures.add(signature)
                name = candidate.get("memo_name", "")
                protected_reasons.setdefault(name, []).append(f"{kind}:{value}")
                if name not in selected_names:
                    selected.append(candidate)
                    selected_names.add(name)
                if len(seen_signatures) >= protect_limit:
                    break

        # Narrative questions need distinct stages, not several rewrites of only the opening event.
        if narrative_query:
            stage_candidates = [h for h in hits if str(h.get("state_change") or "").strip()]
            if required_facets:
                related = [h for h in stage_candidates if coverage.get(h.get("memo_name", ""), set())]
                if related:
                    stage_candidates = related
            stage_candidates.sort(key=lambda h: (float(h.get("event_ts") or 0.0), -base_utility(h)))
            stage_choices: list[dict] = []
            if stage_candidates:
                stage_choices.append(stage_candidates[0])
                if len(stage_candidates) > 1:
                    stage_choices.append(stage_candidates[-1])
                strongest = max(stage_candidates, key=base_utility)
                if strongest not in stage_choices:
                    stage_choices.append(strongest)
            for h in stage_choices[:3]:
                name = h.get("memo_name", "")
                protected_reasons.setdefault(name, []).append("event_stage")
                if name not in selected_names:
                    selected.append(h)
                    selected_names.add(name)

        normal_target = max(0, int(target or 0))
        hard_cap = max(normal_target, int(self.recall_necessary_hard_cap or 14))
        if not self.recall_necessary_can_exceed_max and len(selected) > normal_target:
            selected.sort(key=base_utility, reverse=True)
            selected = selected[:normal_target]
            selected_names = {h.get("memo_name", "") for h in selected}
        elif len(selected) > hard_cap:
            selected.sort(key=base_utility, reverse=True)
            selected = selected[:hard_cap]
            selected_names = {h.get("memo_name", "") for h in selected}

        desired = min(hard_cap, max(normal_target, len(selected)))
        covered_now: set[tuple[str, str]] = set()
        selected_signatures: set[tuple[str, str, str]] = set()
        for h in selected:
            covered_now.update(coverage.get(h.get("memo_name", ""), set()))
            selected_signatures.add(event_signature(h))

        while len(selected) < desired:
            best = None
            best_gain = float("-inf")
            best_new: set[tuple[str, str]] = set()
            for h in hits:
                name = h.get("memo_name", "")
                if name in selected_names:
                    continue
                candidate_coverage = coverage.get(name, set())
                new_coverage = candidate_coverage - covered_now
                signature = event_signature(h)
                redundancy = 0.16 if signature in selected_signatures and any(signature) else 0.0
                gain = base_utility(h) + len(new_coverage) * 0.11 - redundancy
                if str(h.get("state_change") or "").strip() and signature not in selected_signatures:
                    gain += 0.06
                if gain > best_gain:
                    best, best_gain, best_new = h, gain, new_coverage
            if best is None:
                break
            best["_information_gain"] = round(best_gain, 4)
            selected.append(best)
            selected_names.add(best.get("memo_name", ""))
            covered_now.update(best_new)
            selected_signatures.add(event_signature(best))

        order = {h.get("memo_name", ""): idx for idx, h in enumerate(hits)}
        selected.sort(key=lambda h: (0 if h.get("memo_name", "") in protected_reasons else 1, order.get(h.get("memo_name", ""), 9999)))
        for h in hits:
            name = h.get("memo_name", "")
            reasons = protected_reasons.get(name, [])
            h["_necessary"] = bool(reasons)
            h["_necessary_reasons"] = reasons
            h["_facet_coverage"] = [f"{k}:{v}" for k, v in sorted(coverage.get(name, set()))]
        return selected, {
            "enabled": True,
            "target": normal_target,
            "selected": len(selected),
            "necessary": len([h for h in selected if h.get("_necessary")]),
            "necessary_can_exceed": bool(self.recall_necessary_can_exceed_max),
            "required_facets": [f"{k}:{v}" for k, v in required_facets],
            "covered_facets": [f"{k}:{v}" for k, v in sorted(covered_now)],
            "protected": {name: reasons for name, reasons in protected_reasons.items() if name in selected_names},
        }

    def _postprocess_recall_hits(
        self,
        query: str,
        hits: list[dict],
        recent_seen: set[str] | None = None,
        limit_override: int | None = None,
    ) -> tuple[list[dict], dict[str, Any], list[dict]]:
        recent_seen = recent_seen or set()
        working = [dict(h) for h in hits if h.get("memo_name")]
        self._annotate_injection_scores(query, working, recent_seen)
        for h in working:
            mn = h.get("memo_name", "")
            if mn in recent_seen:
                h["_qualified"] = False
                h["_reject_reason"] = "最近已注入"
                continue
            score = float(h.get("_injection_score") or 0.0)
            protected = bool(h.get("_protected"))
            threshold = float(self.recall_injection_min_score or 0.0)
            h["_qualified"] = bool(protected or score >= threshold or not self.recall_rerank_enable)
            if not h["_qualified"]:
                h["_reject_reason"] = f"低于注入资格线 {threshold:.2f}"
        qualified = [h for h in working if h.get("_qualified")]
        if self.recall_rerank_enable:
            qualified.sort(key=lambda x: (float(x.get("_injection_score") or 0.0), float(x.get("score") or 0.0)), reverse=True)
        folded_view, cluster_info = self._apply_cluster_fold(qualified)
        episodic_mode = any(bool(hit.get("_episodic")) for hit in qualified)
        use_information_gain = bool(self.recall_information_gain_enable or episodic_mode)
        # Information-gain selection owns real injection safety. Similarity clusters
        # remain diagnostic and must never hide a diary before necessity is evaluated.
        selection_pool = qualified if use_information_gain else (
            folded_view if (self.recall_cluster_fold_enable and self.recall_cluster_fold_apply) else qualified
        )
        if limit_override is not None:
            limit = max(0, int(limit_override))
            dynamic_diag = {"target": limit, "mode": "episodic_plan"}
        elif not self.recall_dynamic_count_enable:
            limit = max(self.recall_top_k, self.persona_top_k + self.plot_top_k + self.texture_top_k)
            dynamic_diag = {"target": limit, "mode": "fixed"}
        else:
            limit, dynamic_diag = self._dynamic_inject_limit(selection_pool)
        if use_information_gain:
            selected, information_gain_diag = self._select_information_gain(query, selection_pool, limit)
        else:
            selected = self._select_layered_hits(selection_pool, limit=limit)
            information_gain_diag = {"enabled": False, "target": limit, "selected": len(selected)}
        selected_names = {h.get("memo_name") for h in selected}
        min_inject = max(0, int(self.recall_min_inject or 0))
        if self.recall_dynamic_count_enable and len(selected) < min_inject:
            for h in selection_pool:
                if h.get("memo_name") not in selected_names:
                    selected.append(h)
                    selected_names.add(h.get("memo_name"))
                if len(selected) >= min_inject:
                    break
        for h in working:
            if h.get("memo_name") in selected_names:
                h["_selected"] = True
                h["_reject_reason"] = ""
            else:
                h["_selected"] = False
                if not h.get("_reject_reason"):
                    if h.get("_folded") and self.recall_cluster_fold_apply:
                        h["_reject_reason"] = h.get("_reject_reason") or "相似簇折叠"
                    elif h.get("_qualified"):
                        h["_reject_reason"] = "合格但未进入动态名额"
                    else:
                        h["_reject_reason"] = "未通过注入资格"
        diagnostics = {
            "candidate_pool": len(working),
            "qualified": len(qualified),
            "selected": len(selected),
            "folded": int(cluster_info.get("folded") or 0),
            "cluster": cluster_info,
            "dynamic": bool(self.recall_dynamic_count_enable),
            "min_score": float(self.recall_injection_min_score or 0.0),
            "max_inject": int(self.recall_max_inject or 0),
            "dynamic_target": int(limit),
            "dynamic_detail": dynamic_diag,
            "information_gain": information_gain_diag,
            "episodic_information_gain_forced": bool(episodic_mode and not self.recall_information_gain_enable),
            "cluster_apply": bool(self.recall_cluster_fold_apply),
        }
        return selected, diagnostics, working

    def _dynamic_inject_limit(self, hits: list[dict]) -> tuple[int, dict[str, Any]]:
        """Choose a real dynamic count from retrieval evidence, not merely a max cap."""
        if not hits:
            return 0, {"target": 0, "reason": "empty_pool", "strengths": []}
        maximum = max(0, int(self.recall_max_inject or 0))
        if maximum <= 0:
            return 0, {"target": 0, "reason": "max_disabled", "strengths": []}
        minimum = min(maximum, max(0, int(self.recall_min_inject or 0)))
        base_gate = max(0.45, min(0.72, float(self.min_similarity_to_inject or 0.52)))

        ranked: list[tuple[float, dict]] = []
        for h in hits:
            relevance = max(0.0, min(1.0, float(h.get("relevance") or 0.0)))
            rerank_score = max(0.0, min(1.0, float(h.get("_rerank_score") or 0.0)))
            strength = max(relevance, rerank_score * 0.95)
            if h.get("_trigger_hits"):
                strength += 0.06
            reasons = h.get("_injection_reasons") or []
            if any(str(x).startswith("tag:") for x in reasons):
                strength += 0.025
            if any(str(x).startswith("long_effect:") for x in reasons):
                strength += 0.015
            if int(h.get("manual") or 0):
                strength += 0.025
            strength += min(0.03, max(0.0, float(h.get("feedback_boost") or 0.0)))
            ranked.append((min(1.0, strength), h))
        ranked.sort(key=lambda item: item[0], reverse=True)

        # Each additional diary needs stronger direct retrieval evidence. This keeps
        # ordinary turns compact while still allowing genuinely broad/strong queries
        # to reach the configured maximum.
        ladder = (0.00, 0.02, 0.05, 0.08, 0.12, 0.16, 0.20, 0.24, 0.28)
        checks = []
        accepted = 0
        for idx, (strength, h) in enumerate(ranked[:maximum]):
            extra = ladder[idx] if idx < len(ladder) else ladder[-1] + 0.04 * (idx - len(ladder) + 1)
            required = min(0.92, base_gate + extra)
            forced_minimum = idx < minimum
            passed = forced_minimum or strength >= required
            checks.append({
                "rank": idx + 1,
                "memo": str(h.get("memo_name") or "")[:32],
                "strength": round(strength, 4),
                "required": round(required, 4),
                "passed": passed,
                "forced_minimum": forced_minimum,
            })
            if not passed:
                break
            accepted += 1
        target = min(maximum, max(minimum, accepted))
        return target, {
            "target": target,
            "base_gate": round(base_gate, 4),
            "pool": len(hits),
            "strengths": checks,
        }

    def _select_layered_hits(self, hits: list[dict], limit: int | None = None) -> list[dict]:
        """Pick a balanced memory mix: persona first, then plot, then light texture."""
        buckets = {"persona": [], "plot": [], "texture": []}
        for h in hits:
            buckets.setdefault(self._memory_layer(h), []).append(h)
        limits = {
            "persona": max(0, self.persona_top_k),
            "plot": max(0, self.plot_top_k),
            "texture": max(0, self.texture_top_k),
        }
        selected: list[dict] = []
        seen: set[str] = set()
        fallback_limit = max(self.recall_top_k, sum(limits.values())) if limit is None else max(0, int(limit))
        for layer in ("persona", "plot", "texture"):
            for h in buckets.get(layer, [])[:limits[layer]]:
                if len(selected) >= fallback_limit:
                    break
                mn = h.get("memo_name", "")
                if mn and mn in seen:
                    continue
                selected.append(h)
                if mn:
                    seen.add(mn)
        for h in hits:
            if len(selected) >= fallback_limit:
                break
            mn = h.get("memo_name", "")
            if mn and mn in seen:
                continue
            selected.append(h)
            if mn:
                seen.add(mn)
        return selected

    def _format_layered_injection(self, blocks: list[dict[str, Any]]) -> str:
        if getattr(self, "lean_recall_enable", False):
            story = [str(block.get("text") or "") for block in blocks if str(block.get("text") or "").strip()]
            if not story:
                return ""
            parts = [
                '<HistoricalMemorySet role="fused_past_memory">',
                "以下每条记忆融合事件核心、第一人称日记视角与按需一手证据；它们都描述过去，"
                "不是当前时间，也不是刚刚发生。事件核心用于事实与状态变化，日记视角用于情感、"
                "语气和连续性，一手证据用于校正具体细节；三者冲突时优先一手证据。"
                "当前现实时间始终以 CurrentTimeContext 为准；保留人物主体、日期、否定、承诺和因果，"
                "只在本轮相关时自然承接，不要逐条复述。",
            ]
            if story:
                parts.append("[剧情连续性]\n" + "\n\n".join(story))
            parts.append("</HistoricalMemorySet>")
            return "\n".join(parts)
        grouped = {"persona": [], "plot": [], "texture": []}
        for b in blocks:
            grouped.setdefault(b.get("layer", "plot"), []).append(b.get("text", ""))
        parts = [
            "[长期记忆：以下均为历史记忆，不代表当前现实时间，也不是刚刚发生；当前时间永远以 <CurrentTimeContext> 为准。请自然融入，不要机械复述]"
        ]
        if self.enable_injection_style_rules:
            parts.append(
                "使用原则：长期人格影响只通过语气、迟疑、靠近、回避、试探、确认和选择表现，不要直白解释“因为某段记忆所以如此”；"
                "剧情连续性只在当前话题需要时自然承接，避免突然背设定；"
                "日常氛围碎片只作为轻微生活质感，不能抢走当前情绪和剧情方向；"
                "不要把记忆发生时间误当作本轮对话的现在，也不要把召回的旧日记演成刚刚发生。"
            )
        if grouped["persona"]:
            head = "[长期人格影响]"
            if self.enable_injection_style_rules:
                head += "\n规则：把这些影响演出来，不要逐条复述。"
            parts.append(head + "\n" + "\n".join(grouped["persona"]))
        if grouped["plot"]:
            head = "[剧情连续性]"
            if self.enable_injection_style_rules:
                head += "\n规则：当前话题相关时自然提起；不相关时只作为防穿帮背景。"
            parts.append(head + "\n" + "\n".join(grouped["plot"]))
        if grouped["texture"]:
            head = "[日常氛围碎片]"
            if self.enable_injection_style_rules:
                head += "\n规则：只增加生活感和细微反应，不主动改变场景目标。"
            parts.append(head + "\n" + "\n".join(grouped["texture"]))
        return "\n\n".join(parts)

    def _should_inject_full_diary(self, hit: dict, content: str, rank: int, query: str) -> tuple[bool, str]:
        if not self.mixed_injection_enable or not self.passage_index_enable:
            return True, "mixed_disabled"
        full_diary_limit = self.episodic_full_diary_limit if hit.get("_episodic") else self.full_diary_top_n
        if rank < full_diary_limit:
            return True, "core_rank"
        if len(content) <= max(320, int(self.passage_max_chars * 1.35)):
            return True, "short_complete_diary"
        matched = hit.get("_matched_passages") or []
        if len(matched) >= 2:
            return True, "multiple_relevant_passages"
        narrative = any(word in query for word in (
            "为什么", "怎么", "发生了什么", "后来", "之后", "以前", "过程", "完整", "全部", "从那以后",
        ))
        temporal = bool(self._extract_recall_facets(query).get("temporal"))
        memory_type = str(hit.get("memory_type") or "")
        emotional_arc = memory_type in {"relationship_shift", "emotional_anchor", "promise_or_rule", "behavior_bias"}
        if hit.get("_episodic"):
            if hit.get("_necessary") and rank < max(2, full_diary_limit) and (
                narrative or temporal or emotional_arc or hit.get("state_change")
            ):
                return True, "necessary_complete_context"
            if narrative or temporal:
                return False, "episodic_supporting_passage"
            return False, "episodic_supporting_passage"
        if hit.get("_necessary") and (narrative or temporal or emotional_arc or hit.get("state_change")):
            return True, "necessary_complete_context"
        if narrative or temporal:
            return True, "narrative_or_time_query"
        return False, "supporting_passage"

    def _is_precise_memory_query(self, query: str) -> bool:
        source = str(query or "")
        precise_markers = (
            "原话", "具体说", "怎么说", "说过什么", "哪一天", "什么时候", "日期", "几点",
            "到底", "确定", "真的", "有没有", "是不是", "记错", "证据", "答应", "承诺",
            "约定", "边界", "规则", "谁说", "谁先", "原本", "细节", "当时说",
        )
        return bool(
            any(marker in source for marker in precise_markers)
            or self._extract_recall_facets(source).get("temporal")
        )

    def _prepare_fused_memory_hit(
        self,
        query: str,
        hit: dict[str, Any],
        *,
        include_source_evidence: bool = True,
    ) -> None:
        """Attach compact event and first-hand evidence layers to one diary hit."""
        hit["_fusion_event_core"] = []
        hit["_fusion_source_evidence"] = []
        if self._episodes is None:
            return
        memo_name = str(hit.get("memo_name") or "")
        episode = self._episodes.get_episode(memo_name)
        if not episode:
            return
        core: list[str] = []
        scene = str(episode.get("scene_anchor") or "").strip()
        if scene:
            core.append("事件: " + scene[:360])
        state_change = str(episode.get("state_change") or "").strip()
        if state_change:
            core.append("变化: " + state_change[:320])
        unresolved = [str(value).strip() for value in (episode.get("unresolved") or []) if str(value).strip()]
        if unresolved:
            core.append("未解决: " + "；".join(unresolved[:2])[:320])
        if str(episode.get("memory_type") or "") == "promise_or_rule":
            boundary = str(episode.get("trigger_hint") or episode.get("retrieval_key") or "").strip()
            if boundary:
                core.append("承诺/边界: " + boundary[:320])
        hit["_fusion_event_core"] = list(dict.fromkeys(core))[:4]

        if not include_source_evidence:
            hit["_fusion_evidence_quality"] = str(
                episode.get("evidence_quality") or "diary_derived"
            )
            return

        precise = self._is_precise_memory_query(query)
        source_lines: list[str] = []
        direct_turns = sorted(
            [item for item in (hit.get("_source_turn_hits") or []) if isinstance(item, dict)],
            key=lambda item: (
                bool(item.get("exact_link")), float(item.get("relevance") or 0.0),
            ),
            reverse=True,
        )
        configured_limit = max(
            1, min(8, int(getattr(self, "episodic_evidence_per_memory", 3) or 3))
        )
        direct_limit = configured_limit if precise else 1
        for item in direct_turns[:direct_limit]:
            content = " ".join(str(item.get("content") or "").split()).strip()
            if not content:
                continue
            role = str(item.get("role") or "").lower()
            actor = "用户" if role == "user" else (self.character_name or "角色")
            source_lines.append(f"{actor}: {content[:420 if precise else 280]}")

        quality = str(episode.get("evidence_quality") or "diary_derived")
        if quality in {"source_grounded", "mixed_user_edited"} and len(source_lines) < direct_limit:
            for evidence in self._episodes.evidence_for_memo(memo_name, query, limit=3):
                score = float(evidence.get("match_score") or 0.0)
                if not precise and score < 0.28:
                    continue
                actor = str(evidence.get("actor") or "").strip()
                detail = str(evidence.get("detail") or "").strip()
                quote = str(evidence.get("quote_text") or "").strip()
                value = quote or detail
                if not value:
                    continue
                line = (actor + ": " if actor else "") + value[:420 if precise else 280]
                if line not in source_lines:
                    source_lines.append(line)
                if len(source_lines) >= direct_limit:
                    break
        hit["_fusion_source_evidence"] = source_lines[:direct_limit]
        hit["_fusion_evidence_quality"] = quality

    def _select_lean_evidence_hits(
        self,
        query: str,
        selected_hits: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        if not selected_hits:
            return [], "empty"
        if not getattr(self, "lean_adaptive_evidence_enable", True):
            return selected_hits[:self.lean_story_max_inject], "compat_all"
        precise = self._is_precise_memory_query(query)
        ranked = []
        for index, hit in enumerate(selected_hits):
            memory_type = str(hit.get("memory_type") or "")
            constrained = bool(
                memory_type == "promise_or_rule"
                or hit.get("_temporal_rescue")
                or hit.get("_unresolved")
            )
            grounded = str(hit.get("_evidence_quality") or "") in {
                "source_grounded", "mixed_user_edited",
            }
            if precise or constrained:
                priority = (
                    1 if constrained else 0,
                    1 if grounded else 0,
                    float(hit.get("_injection_score") or hit.get("relevance") or 0.0),
                    -index,
                )
                ranked.append((priority, hit))
        ranked.sort(key=lambda item: item[0], reverse=True)
        limit = 2 if precise else 1
        return [item[1] for item in ranked[:limit]], "precise" if precise else "constraint"

    def _format_episode_evidence_packet(
        self,
        query: str,
        selected_hits: list[dict[str, Any]],
    ) -> str:
        if self._episodes is None or not selected_hits:
            return ""
        if getattr(self, "lean_recall_enable", False):
            # 4.4 merges event core and query-conditioned source evidence into
            # each selected memory item. A second packet would duplicate facts
            # and recreate the lost-in-the-middle problem.
            return ""
        focus_lines = []
        evidence_groups = []
        constraints = []
        detailed_count = 0
        detail_limit = max(3, min(6, int(self.episodic_default_inject or 5)))
        for hit in selected_hits:
            if not hit.get("_episodic"):
                continue
            memo_name = str(hit.get("memo_name") or "")
            episode = self._episodes.get_episode(memo_name)
            if not episode:
                continue
            occurred = str(episode.get("occurred_at") or hit.get("ts_text") or "未注明")
            anchor = str(episode.get("scene_anchor") or hit.get("scene_anchor") or "").strip()
            state_change = str(episode.get("state_change") or "").strip()
            long_effect = str(episode.get("long_effect") or "").strip()
            focus = " | ".join(value for value in [occurred, anchor, state_change or long_effect] if value)
            if focus:
                focus_lines.append("- " + focus[:520])
            quality = str(episode.get("evidence_quality") or "diary_derived")
            quality_label = {
                "source_grounded": "原始对话可追溯",
                "mixed_user_edited": "原始证据与用户编辑日记并存",
                "diary_derived": "由旧日记推导",
            }.get(quality, quality)
            memory_type = str(episode.get("memory_type") or "")
            trigger = str(episode.get("trigger_hint") or "").strip()
            expand_detail = bool(detailed_count < detail_limit or hit.get("_necessary") or memory_type == "promise_or_rule")
            if expand_detail:
                evidence = self._episodes.evidence_for_memo(
                    memo_name,
                    query,
                    limit=self.episodic_evidence_per_memory,
                )
                lines = [
                    f'<EpisodeEvidence memo="{memo_name.replace(chr(34), "")}" '
                    f'quality="{quality}" occurred_at="{occurred.replace(chr(34), "")}">',
                    f"证据等级: {quality_label}。",
                ]
                for item in evidence:
                    actor = str(item.get("actor") or "").strip()
                    detail = str(item.get("detail") or "").strip()
                    quote = str(item.get("quote_text") or "").strip()
                    prefix = f"{actor}: " if actor else ""
                    if detail:
                        lines.append("- " + prefix + detail[:420])
                    if quote and quote != detail:
                        lines.append("  原话: 「" + quote[:260] + "」")
                lines.append("</EpisodeEvidence>")
                evidence_groups.append("\n".join(lines))
                detailed_count += 1
            if memory_type == "promise_or_rule" and (anchor or trigger):
                constraints.append("- 承诺/边界: " + (trigger or anchor)[:420])
            unresolved = episode.get("unresolved") if isinstance(episode.get("unresolved"), list) else []
            for item in unresolved[:2]:
                constraints.append("- 尚未解决: " + str(item)[:420])
        if not evidence_groups:
            return ""
        parts = [
            '<RecalledMemoryEvidence role="query_conditioned_historical_evidence">',
            "使用规则: 只使用与本轮问题相关的历史证据；保留人物主体、日期、否定、边界和未解决状态。"
            "不得把旧事件说成刚刚发生，不得把由旧日记推导的内容伪装成逐字原始对话。",
        ]
        if focus_lines:
            parts.append("[本轮记忆焦点]\n" + "\n".join(focus_lines))
        parts.append("[可追溯证据]\n" + "\n".join(evidence_groups))
        if constraints:
            unique_constraints = list(dict.fromkeys(constraints))[:8]
            parts.append("[本轮响应约束]\n" + "\n".join(unique_constraints))
        parts.append("</RecalledMemoryEvidence>")
        return "\n\n".join(parts)

    def _expanded_matched_passage(self, hit: dict, content: str) -> tuple[str, dict[str, Any]]:
        matched = list(hit.get("_matched_passages") or [])
        best = matched[0] if matched else {
            "passage_index": int(hit.get("passage_index") or 0),
            "char_start": int(hit.get("char_start") or 0),
            "char_end": int(hit.get("char_end") or 0),
            "text": hit.get("chunk_text") or "",
        }
        start = max(0, int(best.get("char_start") or 0))
        end = max(start, int(best.get("char_end") or 0))
        if not content or end <= start or start >= len(content):
            return str(best.get("text") or hit.get("chunk_text") or content), best
        start = max(0, start - self.passage_expand_chars)
        end = min(len(content), end + self.passage_expand_chars)
        boundary_chars = "。！？!?\n"
        while start > 0 and content[start - 1] not in boundary_chars:
            start -= 1
        while end < len(content) and content[end - 1] not in boundary_chars:
            end += 1
        excerpt = content[start:end].strip()
        detail = dict(best)
        detail.update({"expanded_start": start, "expanded_end": end})
        return excerpt or str(best.get("text") or ""), detail

    def _format_inject_block(
        self,
        hit: dict,
        content: str,
        *,
        passage_mode: bool = False,
        passage_detail: dict[str, Any] | None = None,
        reference_now_ts: float | None = None,
        compact_mode: bool = False,
    ) -> str:
        """Format one memory item for layered injection."""
        ts = str(hit.get("ts_text", "未注明"))
        occurred_at = str(hit.get("occurred_at") or "").strip()
        event_ts = float(hit.get("event_ts") or 0.0)
        time_basis = str(hit.get("time_basis") or "unknown")
        age = relative_memory_age(event_ts, reference_now_ts if reference_now_ts is not None else time.time())
        raw = str(hit.get("memory_type") or "").strip()
        if raw == "plot_fact" and not hit.get("long_effect") and not hit.get("trigger_hint"):
            mt = _normalize_memory_type("", content, [])
        else:
            mt = _normalize_memory_type(raw, content, [])
        if getattr(self, "lean_recall_enable", False):
            display_time = occurred_at or ts
            mode = "紧凑摘录" if compact_mode else ("相关段落" if passage_mode else "完整日记")
            core_lines = [
                str(value).strip() for value in (hit.get("_fusion_event_core") or [])
                if str(value).strip()
            ]
            evidence_lines = [
                str(value).strip() for value in (hit.get("_fusion_source_evidence") or [])
                if str(value).strip()
            ]
            if compact_mode:
                # Budget degradation keeps the memory present with its event
                # core intact; only the literary body and evidence shrink.
                compact_chars = max(80, int(getattr(self, "inject_compact_chars", 200)))
                if len(content) > compact_chars:
                    content = content[:compact_chars].rstrip() + "…"
                evidence_lines = evidence_lines[:1]
            header = f"[{display_time} · {age} | {mt} | {mode}]"
            parts = [header]
            event_text = ""
            evidence_text = ""
            if core_lines:
                event_text = "[事件核心]\n" + "\n".join("- " + value for value in core_lines)
                parts.append(event_text)
            diary_text = "[日记视角]\n" + content
            parts.append(diary_text)
            if evidence_lines:
                evidence_text = "[一手证据]\n" + "\n".join("- " + value for value in evidence_lines)
                parts.append(evidence_text)
            hit["_injection_part_chars"] = {
                "header": len(header),
                "event_core": len(event_text),
                "diary": len(diary_text),
                "source_evidence": len(evidence_text),
            }
            return "\n".join(parts)
        long_effect = _safe_meta_text(hit.get("long_effect"))
        trigger_hint = _safe_meta_text(hit.get("trigger_hint"))
        suffix_parts = []
        if mt in _PERSONA_TYPES and long_effect:
            suffix_parts.append("长期影响: " + long_effect)
        if trigger_hint:
            suffix_parts.append("触发线索: " + trigger_hint)
        suffix = ("（" + "；".join(suffix_parts) + "）") if suffix_parts else ""
        attr_ts = ts.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        attr_occurred = occurred_at.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        attr_mt = mt.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        def wrap_historical(body: str) -> str:
            if passage_mode:
                detail = passage_detail or {}
                passage_index = int(detail.get("passage_index") or hit.get("passage_index") or 0) + 1
                return (
                    f'<HistoricalMemoryPassage memo="{str(hit.get("memo_name") or "")}" '
                    f'occurred_at="{attr_occurred or attr_ts}" time_basis="{time_basis}" '
                    f'memory_type="{attr_mt}" passage="{passage_index}">\n'
                    f"来源完整日记的事件时间: {ts}\n"
                    f"与本轮当前时间的距离: {age}\n"
                    f"记忆类型: {mt}\n"
                    "范围说明: 这是完整历史日记中与本轮最相关的局部段落，不是独立新事件，也不是刚刚发生。"
                    "日期、人物关系和情感因果仍属于来源日记；不得覆盖 CurrentTimeContext。\n"
                    f"相关段落: {body}{suffix}\n"
                    "</HistoricalMemoryPassage>"
                )
            return (
                f'<HistoricalMemory occurred_at="{attr_occurred or attr_ts}" time_basis="{time_basis}" memory_type="{attr_mt}">\n'
                f"历史事件发生时间: {ts}\n"
                f"与本轮当前时间的距离: {age}\n"
                f"时间可信度: {time_basis}（只描述事件时间，不是 Memos 创建/同步时间）\n"
                f"记忆类型: {mt}\n"
                "严格时间边界: 这是旧事件记录。即使发生日期是今天，也不等于本轮刚刚发生；不得覆盖 CurrentTimeContext。\n"
                f"{body}{suffix}\n"
                "</HistoricalMemory>"
            )
        # 最干净: month_day · time_label, 如 "六月廿六 · 深夜"
        if compact_mode:
            compact_chars = max(80, int(getattr(self, "inject_compact_chars", 200)))
            head = content[:compact_chars].rstrip()
            if len(content) > compact_chars:
                head += "…"
            return wrap_historical("记忆摘录: " + head)
        if self.inject_format == "summary":
            head = content[:self.summary_chars].rstrip()
            if len(content) > self.summary_chars:
                head += "…"
            return wrap_historical("记忆摘要: " + head)
        if self.inject_format == "quote":
            return wrap_historical("你曾记下:「" + content + "」")
        # diary 格式(默认): 时间锚 + 正文
        return wrap_historical("记忆正文: " + content)

    async def _eval_recall_query(self, query: str, top_k: int | None = None) -> dict[str, Any]:
        """Dry-run recall for WebUI lab/commands. Does not mutate dedup state or inject."""
        eval_now_ts = self._request_now().timestamp()
        if not await self._ensure_init() or self._vec is None:
            raise RuntimeError("plugin not ready")
        query = (query or "").strip()
        if not query:
            raise ValueError("empty query")
        candidate_k = top_k or max(
            int(self.recall_candidate_pool or 24),
            self.recall_top_k,
            self.persona_top_k + self.plot_top_k + self.texture_top_k + 3,
        )
        time_ctx = self._session_time.get("", {}) if hasattr(self, "_session_time") else {}
        current_md = str(time_ctx.get("solar_md", "") or "").strip()
        if not current_md:
            now = time.localtime()
            current_md = f"{now.tm_mon}月{now.tm_mday}日"
        episodic_ready = bool(
            self.episodic_memory_enable
            and self._episodes is not None
            and self._episode_migration_ready
            and int(self._episodes.stats().get("episodes") or 0) > 0
        )
        lean_ready = bool(episodic_ready and self.lean_recall_enable)
        if lean_ready:
            hits, routes_diag, facets = await self._lean_recall_search(query, "", current_md)
        elif episodic_ready:
            hits, routes_diag, facets = await self._episodic_recall_search(query, "", current_md)
        else:
            hits, routes_diag, facets = await self._parallel_recall_search(query, "", candidate_k, current_md)
        keyword_hits = []
        if not episodic_ready:
            try:
                keyword_hits = self._vec.keywords_find_in_text(
                    query, limit=max(20, self.recall_top_k * 4), min_weight=1.2, min_hits=2
                )
            except Exception:
                keyword_hits = []
        if keyword_hits:
            existing = {h.get("memo_name") for h in hits}
            for kh in keyword_hits:
                mn = kh.get("memo_name")
                if not mn or mn in existing:
                    continue
                row = self._vec._connect().execute(
                    """SELECT id AS chunk_id, chunk_text, ts_text, importance, tags, memory_type,
                              long_effect, trigger_hint, created_ts, occurred_at, event_ts,
                              time_basis, source_created_ts, source_updated_ts, passage_index,
                              char_start, char_end, content_hash, scene_anchor, retrieval_key,
                              state_change, entities
                       FROM chunks WHERE memo_name=? ORDER BY rowid LIMIT 1""",
                    (mn,),
                ).fetchone()
                if row:
                    keyword_relevance = self._keyword_hit_relevance(kh)
                    hits.append({
                        "memo_name": mn,
                        "chunk_text": row["chunk_text"] or "",
                        "ts_text": row["ts_text"] or "",
                        "tags": row["tags"] or "",
                        "importance": int(row["importance"] or 3),
                        "memory_type": row["memory_type"] or "plot_fact",
                        "long_effect": row["long_effect"] or "",
                        "trigger_hint": row["trigger_hint"] or "",
                        "score": keyword_relevance,
                        "relevance": keyword_relevance,
                        "created_ts": float(row["created_ts"] or 0),
                        "occurred_at": row["occurred_at"] or "",
                        "event_ts": float(row["event_ts"] or 0),
                        "time_basis": row["time_basis"] or "unknown",
                        "source_created_ts": float(row["source_created_ts"] or 0),
                        "source_updated_ts": float(row["source_updated_ts"] or 0),
                        "chunk_id": int(row["chunk_id"] or 0),
                        "passage_index": int(row["passage_index"] or 0),
                        "char_start": int(row["char_start"] or 0),
                        "char_end": int(row["char_end"] or 0),
                        "content_hash": row["content_hash"] or "",
                        "scene_anchor": row["scene_anchor"] or "",
                        "retrieval_key": row["retrieval_key"] or "",
                        "state_change": row["state_change"] or "",
                        "entities": row["entities"] or "",
                        "_matched_passages": [{
                            "key": int(row["chunk_id"] or 0), "chunk_id": int(row["chunk_id"] or 0),
                            "passage_index": int(row["passage_index"] or 0),
                            "char_start": int(row["char_start"] or 0), "char_end": int(row["char_end"] or 0),
                            "text": row["chunk_text"] or "", "route": "keyword",
                            "relevance": keyword_relevance,
                        }],
                        "_keyword_boost": True,
                        "_keyword": kh.get("keyword", ""),
                    })
                    existing.add(mn)
        if lean_ready:
            plan = dict(routes_diag.get("plan") or {})
            if top_k is not None:
                plan["target"] = max(1, min(self.lean_story_max_inject, int(top_k)))
            selection_query = str(plan.get("search_text") or query)
            selected, recall_diag, annotated_hits = self._postprocess_lean_hits(
                selection_query, hits, plan, recent_seen=set(),
            )
            recall_diag["lean"] = routes_diag
            recall_diag["facets"] = facets
        elif episodic_ready:
            target = int(top_k or (routes_diag.get("plan") or {}).get("target") or self.episodic_default_inject)
            selection_query = str((routes_diag.get("plan") or {}).get("search_text") or query)
            selected, recall_diag, annotated_hits = self._postprocess_recall_hits(
                selection_query,
                hits,
                recent_seen=set(),
                limit_override=target,
            )
            recall_diag["episodic"] = routes_diag
            recall_diag["facets"] = facets
        elif self.enable_layered_injection:
            selected, recall_diag, annotated_hits = self._postprocess_parallel_recall_hits(query, hits, recent_seen=set())
            recall_diag["multi_query"] = routes_diag
            recall_diag["facets"] = facets
        else:
            selected = hits[:candidate_k]
            recall_diag = {"candidate_pool": len(hits), "qualified": len(hits), "selected": len(selected), "folded": 0}
            annotated_hits = selected
        blocks: list[dict[str, Any]] = []
        selected_names = {h.get("memo_name") for h in selected}
        out_hits = []
        annotated_hits = sorted(
            annotated_hits,
            key=lambda x: (bool(x.get("_selected")), float(x.get("_injection_score") or x.get("score") or 0.0)),
            reverse=True,
        )
        selected_rank = {h.get("memo_name", ""): idx for idx, h in enumerate(selected)}
        for h in annotated_hits:
            mn = h.get("memo_name", "")
            layer = self._memory_layer(h)
            content = ""
            block = ""
            if mn in selected_names:
                content = await self._fetch_memo_content(mn, h.get("chunk_text", ""))
                use_full, mode_reason = self._should_inject_full_diary(h, content, selected_rank.get(mn, 999), query)
                passage_detail = None
                inject_content = content
                if content and not use_full:
                    inject_content, passage_detail = self._expanded_matched_passage(h, content)
                if lean_ready:
                    self._prepare_fused_memory_hit(query, h)
                block = self._format_inject_block(
                    h, inject_content, passage_mode=not use_full, passage_detail=passage_detail,
                    reference_now_ts=eval_now_ts,
                ) if content else ""
                h["_injection_mode"] = "full" if use_full else "passage"
                h["_injection_mode_reason"] = mode_reason
                if block:
                    part_chars = dict(h.get("_injection_part_chars") or {})
                    blocks.append({
                        "layer": layer, "text": block,
                        "diary_chars": int(part_chars.get("diary") or len(block)),
                        "event_core_chars": int(part_chars.get("event_core") or 0),
                        "source_evidence_chars": int(part_chars.get("source_evidence") or 0),
                    })
            out_hits.append({
                "memo_name": mn,
                "ts_text": h.get("ts_text", ""),
                "occurred_at": h.get("occurred_at", ""),
                "event_ts": float(h.get("event_ts") or 0),
                "time_basis": h.get("time_basis", "unknown"),
                "importance": h.get("importance", 3),
                "memory_type": h.get("memory_type", "plot_fact"),
                "long_effect": h.get("long_effect", ""),
                "trigger_hint": h.get("trigger_hint", ""),
                "score": round(float(h.get("score") or 0), 4),
                "injection_score": round(float(h.get("_injection_score") or h.get("score") or 0), 4),
                "relevance": round(float(h.get("relevance") or 0), 4),
                "score_parts": h.get("score_parts") or {},
                "diagnostics": h.get("diagnostics") or {},
                "feedback_boost": round(float(h.get("feedback_boost") or 0), 4),
                "layer": layer,
                "selected": bool(h.get("_selected")) or mn in selected_names,
                "qualified": bool(h.get("_qualified", True)),
                "protected": bool(h.get("_protected")),
                "necessary": bool(h.get("_necessary")),
                "necessary_reasons": h.get("_necessary_reasons") or [],
                "facet_coverage": h.get("_facet_coverage") or [],
                "injection_mode": h.get("_injection_mode", ""),
                "injection_mode_reason": h.get("_injection_mode_reason", ""),
                "matched_passages": h.get("_matched_passages") or [],
                "route_evidence": h.get("_route_evidence") or [],
                "scene_anchor": h.get("scene_anchor", ""),
                "retrieval_key": h.get("retrieval_key", ""),
                "state_change": h.get("state_change", ""),
                "entities": h.get("entities", ""),
                "evidence_quality": h.get("_evidence_quality", ""),
                "affect_before": h.get("_affect_before", ""),
                "affect_after": h.get("_affect_after", ""),
                "unresolved": h.get("_unresolved") or [],
                "retrieval_bonus": round(float(h.get("_retrieval_bonus") or 0.0), 4),
                "retrieval_bonus_reasons": h.get("_retrieval_bonus_reasons") or [],
                "cross_layer_available": h.get("_cross_layer_available") or [],
                "cross_layer_supported": h.get("_cross_layer_supported") or [],
                "cross_layer_consistency": round(float(h.get("_cross_layer_consistency") or 0.0), 4),
                "layer_signals": h.get("_layer_signals") or {},
                "temporal_constraint_match": bool(h.get("_temporal_constraint_match")),
                "cluster_id": h.get("_cluster_id", ""),
                "folded": bool(h.get("_folded")),
                "reasons": h.get("_injection_reasons") or [],
                "penalties": h.get("_injection_penalties") or [],
                "source": "episodic" if h.get("_episodic") else ("keyword" if h.get("_keyword_boost") else "hybrid"),
                "keyword": h.get("_keyword", ""),
                "preview": (h.get("chunk_text", "") or "")[:240],
                "inject_preview": block[:500],
                "reject_reason": "" if mn in selected_names else (h.get("_reject_reason") or self._diagnose_reject_reason(h, selected, layer)),
            })
        evidence_packet = self._format_episode_evidence_packet(query, selected) if episodic_ready else ""
        return {
            "query": query,
            "feedback_context": {"request_id": f"eval-{time.time_ns():x}", "query": query, "source": "recall_lab"},
            "candidate_count": len(hits),
            "selected_count": len(blocks),
            "recall_postprocess": recall_diag,
            "affiliate": self._affiliate_profile_status(),
            "time_insight": self._time_insight_status(),
            "hits": out_hits,
            "semantic_state": self._semantic_state_status(include_history=False),
            "injection_preview": self._format_injection_with_profile(
                blocks,
                evidence_packet=evidence_packet,
            ),
            "recall_architecture": (
                "lean_full_memory_fusion" if lean_ready else ("episodic_cascade" if episodic_ready else "legacy_hybrid")
            ),
            "diary_chars": sum(int(block.get("diary_chars") or 0) for block in blocks),
            "event_core_chars": sum(int(block.get("event_core_chars") or 0) for block in blocks),
            "evidence_chars": len(evidence_packet) + sum(
                int(block.get("source_evidence_chars") or 0) for block in blocks
            ),
            "keyword_hits": keyword_hits[:10],
        }

    def _generate_recall_eval_cases(self, limit: int | None = None) -> dict[str, Any]:
        """Build a reproducible local eval set without calling an LLM or changing memories."""
        if self._episodes is None:
            raise RuntimeError("episodic memory store is not ready")
        limit = max(20, min(200, int(limit or self.recall_auto_eval_limit)))
        generated: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(query: str, memo_name: str, source: str, note: str) -> None:
            query = " ".join(str(query or "").split()).strip()
            memo_name = str(memo_name or "").strip()
            key = (query, memo_name)
            if len(query) < 3 or not memo_name or key in seen or len(generated) >= limit:
                return
            seen.add(key)
            case_id = "auto_" + hashlib.sha256(
                f"{source}|{query}|{memo_name}".encode("utf-8")
            ).hexdigest()[:22]
            result = self._episodes.upsert_eval_case(
                query, [memo_name], case_id=case_id, note=note,
                source=source, enabled=True,
            )
            generated.append({**result, "source": source})

        if self._vec is not None:
            try:
                for item in self._vec.feedback_event_list(limit=500):
                    if str(item.get("action") or "") not in {"useful", "key"}:
                        continue
                    add(
                        str(item.get("query_text") or ""),
                        str(item.get("memo_name") or ""),
                        "positive_feedback",
                        "用户在 WebUI 明确认定为有帮助或关键记忆；高置信评测样本。",
                    )
            except Exception as exc:
                logger.debug("[memos-memory][eval] positive feedback sampling failed: %s", exc)

        for item in reversed(list(self._last_injection_stats or [])):
            query = str(item.get("query") or "").strip()
            for memo_name in list(item.get("memos") or [])[:2]:
                add(
                    query, str(memo_name), "telemetry_regression",
                    "来自真实历史注入，仅用于检测新版是否丢失旧版已能找到的结果，不等同人工正确性标注。",
                )
                if len(generated) >= max(8, limit // 3):
                    break
            if len(generated) >= max(8, limit // 3):
                break

        episodes = self._episodes.list_episodes(limit=min(1000, max(limit * 4, 120)))
        for episode in episodes:
            if len(generated) >= limit:
                break
            memo_name = str(episode.get("memo_name") or "")
            detail = self._episodes.episode_detail(memo_name) or episode
            evidence = list(detail.get("evidence") or [])
            evidence_text = next((
                str(ev.get("detail") or ev.get("quote_text") or "").strip()
                for ev in evidence
                if 8 <= len(str(ev.get("detail") or ev.get("quote_text") or "").strip()) <= 180
            ), "")
            cue = (
                evidence_text
                or str(episode.get("trigger_hint") or "").strip()
                or str(episode.get("scene_anchor") or "").strip()
                or str(episode.get("retrieval_key") or "").strip()
            )
            if not cue:
                continue
            cue = cue[:180]
            occurred = str(episode.get("occurred_at") or "").strip()
            query = f"关于{cue}，你还记得当时发生了什么？"
            if occurred and any(ch.isdigit() for ch in occurred):
                query = f"{occurred}，关于{cue}，当时发生了什么？"
            add(
                query, memo_name, "episode_holdout",
                "从已有情景证据生成的留出查询；不调用 LLM，不改写日记或原文。",
            )
        by_source: dict[str, int] = {}
        for item in generated:
            source = str(item.get("source") or "unknown")
            by_source[source] = by_source.get(source, 0) + 1
        return {
            "generated": len(generated), "limit": limit, "by_source": by_source,
            "total_enabled": len(self._episodes.list_eval_cases(enabled_only=True, limit=2000)),
            "writes": "local_eval_tables_only",
            "memory_content_changed": False,
        }

    async def _run_recall_ablation(
        self,
        modes: list[str] | None = None,
        *,
        case_limit: int = 80,
    ) -> dict[str, Any]:
        if not await self._ensure_init() or self._vec is None or self._episodes is None:
            raise RuntimeError("plugin not ready")
        cases = self._episodes.list_eval_cases(enabled_only=True, limit=case_limit)
        if not cases:
            raise ValueError("recall evaluation set is empty")
        requested = modes or ["optimized", "baseline", "no_source", "direct", "vector"]
        valid_modes = [
            mode for mode in requested
            if mode in {"optimized", "baseline", "lean", "no_source", "direct", "vector", "legacy"}
        ]
        if not valid_modes:
            raise ValueError("no valid evaluation mode")
        now = time.localtime()
        current_md = f"{now.tm_mon}月{now.tm_mday}日"
        details = []
        aggregates = {
            mode: {"cases": 0, "hit3": 0, "hit5": 0, "hit10": 0, "rr": 0.0, "latency_ms": 0.0}
            for mode in valid_modes
        }
        for case in cases:
            query = str(case.get("query") or "").strip()
            expected = {str(value) for value in (case.get("expected_memos") or []) if str(value)}
            if not query or not expected:
                continue
            case_result = {
                "case_id": case.get("case_id"), "query": query,
                "expected_memos": sorted(expected), "modes": {},
            }
            for mode in valid_modes:
                started = time.perf_counter()
                if mode in {"optimized", "lean"}:
                    hits, diag, _ = await self._lean_recall_search(query, "", current_md)
                elif mode == "baseline":
                    hits, diag, _ = await self._lean_recall_search(
                        query, "", current_md, optimizer_enabled=False,
                    )
                elif mode == "no_source":
                    hits, diag, _ = await self._lean_recall_search(
                        query, "", current_md, source_evidence_enabled=False,
                    )
                elif mode == "direct":
                    hits, diag, _ = await self._lean_recall_search(
                        query, "", current_md, temporal_enabled=False,
                        event_index_enabled=False, source_evidence_enabled=False,
                        bm25_weight=0.30, optimizer_enabled=False,
                    )
                elif mode == "vector":
                    hits, diag, _ = await self._lean_recall_search(
                        query, "", current_md, temporal_enabled=False,
                        event_index_enabled=False, source_evidence_enabled=False,
                        bm25_weight=0.0, optimizer_enabled=False,
                    )
                else:
                    hits, diag, _ = await self._parallel_recall_search(
                        query, "", self.lean_recall_candidate_k, current_md,
                    )
                latency_ms = (time.perf_counter() - started) * 1000
                names = [str(hit.get("memo_name") or "") for hit in hits[:10]]
                first_rank = next((index + 1 for index, name in enumerate(names) if name in expected), 0)
                result = {
                    "first_rank": first_rank,
                    "hit_at_3": bool(first_rank and first_rank <= 3),
                    "hit_at_5": bool(first_rank and first_rank <= 5),
                    "hit_at_10": bool(first_rank and first_rank <= 10),
                    "reciprocal_rank": round(1.0 / first_rank, 6) if first_rank else 0.0,
                    "latency_ms": round(latency_ms, 2),
                    "top_memos": names,
                    "diagnostics": diag,
                }
                case_result["modes"][mode] = result
                agg = aggregates[mode]
                agg["cases"] += 1
                agg["hit3"] += int(result["hit_at_3"])
                agg["hit5"] += int(result["hit_at_5"])
                agg["hit10"] += int(result["hit_at_10"])
                agg["rr"] += float(result["reciprocal_rank"])
                agg["latency_ms"] += latency_ms
            details.append(case_result)
        metrics = {}
        for mode, agg in aggregates.items():
            count = max(1, int(agg["cases"]))
            metrics[mode] = {
                "cases": int(agg["cases"]),
                "hit_at_3": round(agg["hit3"] / count, 4),
                "hit_at_5": round(agg["hit5"] / count, 4),
                "hit_at_10": round(agg["hit10"] / count, 4),
                "mrr": round(agg["rr"] / count, 4),
                "avg_latency_ms": round(agg["latency_ms"] / count, 2),
            }
        run_id = self._episodes.record_eval_run("ablation", metrics, details)
        return {"run_id": run_id, "case_count": len(details), "metrics": metrics, "details": details}

    def _diagnose_reject_reason(self, hit: dict, selected: list[dict], layer: str) -> str:
        if float(hit.get("relevance") or 0) < self.min_similarity_to_inject and not hit.get("_rerank_boost"):
            return "低于相似度阈值"
        selected_names = {h.get("memo_name") for h in selected}
        if hit.get("memo_name") in selected_names:
            return ""
        layer_counts = {"persona": 0, "plot": 0, "texture": 0}
        for h in selected:
            layer_counts[self._memory_layer(h)] = layer_counts.get(self._memory_layer(h), 0) + 1
        limits = {"persona": self.persona_top_k, "plot": self.plot_top_k, "texture": self.texture_top_k}
        if layer_counts.get(layer, 0) >= limits.get(layer, self.recall_top_k):
            return "同层名额已满"
        return "总召回名额已满或被更高分记忆压过"

    async def _fetch_memo_content(self, memo_name: str, fallback: str) -> str:
        if not memo_name or not memo_name.startswith("memos/") or self._memos is None:
            return fallback
        try:
            memo = await self._memos.get_memo(memo_name)
            if not memo:
                return fallback
            _ts, body, _tags = _parse_memo_content((memo.get("content") or "").strip())
            return body or fallback
        except Exception as e:
            logger.debug("[memos-memory] fetch memo %s failed: %s", memo_name, e)
            return fallback

    async def _classify_memory_with_llm(self, ts_text: str, body: str, tags: list[str], old_meta: dict | None = None) -> dict[str, Any] | None:
        old_meta = old_meta or {}
        prompt = f"""你是长期角色记忆整理器。请只根据给定记忆内容，为这条旧记忆补全分类元数据。

可选 memory_type:
- plot_fact: 剧情事实，防止身份、地点、事件结果、物品状态穿帮。
- relationship_shift: 关系位置发生变化。
- emotional_anchor: 强烈情绪锚点。
- behavior_bias: 之后更可能出现的反应方式。
- promise_or_rule: 承诺、禁忌、边界、称呼、约定。
- daily_texture: 日常氛围和生活质感。

要求:
1. 不改写正文，不编造正文没有依据的事实。
2. long_effect 写这条记忆对角色长期内心/关系反应的影响，尽量具体。
3. trigger_hint 写未来什么话题/场景会触发，以及应该如何自然表现。
4. importance 1-5；长期关系变化、承诺、离别/重逢、身份真相、重大剧情为4-5；普通日常为2-3。
5. scene_anchor 提取最有辨识度的一句话、动作或物件；retrieval_key 用一句高密度检索句概括人物、事件和情感结果。
6. state_change 写经历前后的关系/情绪/行为变化，没有则留空；entities 只列人物、称呼、地点、物件、约定和事件名。
7. 只输出 JSON 对象，不要解释。

旧元数据:
{json.dumps(old_meta, ensure_ascii=False)}

时间: {ts_text}
标签: {" ".join(tags)}
正文:
{body}

输出格式:
{{"memory_type":"emotional_anchor","long_effect":"...","trigger_hint":"...","scene_anchor":"...","retrieval_key":"...","state_change":"...","entities":["..."],"importance":4}}
"""
        text = await self._call_llm_compress(prompt)
        data = _parse_json_object(text)
        if not data:
            return None
        return {
            "memory_type": _normalize_memory_type(data.get("memory_type"), body, tags),
            "long_effect": _safe_meta_text(data.get("long_effect"), 220),
            "trigger_hint": _safe_meta_text(data.get("trigger_hint"), 220),
            "scene_anchor": _safe_meta_text(data.get("scene_anchor"), 160),
            "retrieval_key": _safe_meta_text(data.get("retrieval_key"), 260),
            "state_change": _safe_meta_text(data.get("state_change"), 260),
            "entities": [str(x).strip() for x in (data.get("entities") or []) if str(x).strip()][:20]
                if isinstance(data.get("entities") or [], list) else [],
            "importance": _normalize_importance(data.get("importance"), self._auto_importance(body)),
        }

    def _build_memo_text_with_meta(
        self,
        ts_text: str,
        body: str,
        tags: list[str],
        *,
        importance: int,
        manual: int,
        memory_type: str,
        long_effect: str,
        trigger_hint: str,
        occurred_at: str = "",
        time_basis: str = "unknown",
        machine_meta: dict[str, Any] | None = None,
    ) -> str:
        text = (ts_text or "未注明时间").strip() + "\n" + (body or "").strip()
        extra = []
        if long_effect:
            extra.append("长期影响: " + _safe_meta_text(long_effect, 220))
        if trigger_hint:
            extra.append("触发线索: " + _safe_meta_text(trigger_hint, 220))
        if extra:
            text += "\n" + "\n".join(extra)
        if tags:
            text += "\n" + " ".join(tags)
        meta64 = _machine_meta64(machine_meta or {})
        text += (
            f"\n<!-- memos-memory:importance={max(1, min(5, int(importance or 3)))};"
            f"manual={1 if manual else 0};type={memory_type};occurred_at={occurred_at};"
            f"time_basis={time_basis or 'unknown'}{';meta64=' + meta64 if meta64 else ''} -->"
        )
        return text

    async def _retag_one_memo(self, memo_name: str) -> dict[str, Any]:
        if self._vec is None:
            raise RuntimeError("vec not ready")
        meta = self._vec.get_memo_meta(memo_name) or {}
        raw_content = ""
        if self._memos is not None and memo_name.startswith("memos/"):
            memo = await self._memos.get_memo(memo_name)
            raw_content = (memo.get("content") or "") if memo else ""
        if raw_content:
            ts_text, body, tags = _parse_memo_content(_strip_internal_metadata(raw_content))
            restored_imp, restored_manual = _importance_from_memo_text(raw_content, tags)
        else:
            ts_text = meta.get("ts_text", "")
            body = await self._fetch_memo_content(memo_name, meta.get("chunk_text", ""))
            tags = meta.get("tags", [])
            restored_imp = meta.get("importance", 3)
            restored_manual = int(meta.get("manual", 0))
        if not body:
            raise RuntimeError("empty memo body")
        old = {
            "memory_type": meta.get("memory_type", ""),
            "long_effect": meta.get("long_effect", ""),
            "trigger_hint": meta.get("trigger_hint", ""),
            "importance": restored_imp or meta.get("importance", 3),
        }
        machine_meta = _machine_meta_from_memo_text(raw_content) if raw_content else {
            "scene_anchor": meta.get("scene_anchor", ""),
            "retrieval_key": meta.get("retrieval_key", ""),
            "state_change": meta.get("state_change", ""),
            "entities": meta.get("entities", []),
        }
        new_meta = await self._classify_memory_with_llm(ts_text, body, tags, old)
        if not new_meta:
            raise RuntimeError("LLM classify failed")
        machine_meta.update({
            "scene_anchor": new_meta.get("scene_anchor") or machine_meta.get("scene_anchor", ""),
            "retrieval_key": new_meta.get("retrieval_key") or machine_meta.get("retrieval_key", ""),
            "state_change": new_meta.get("state_change") or machine_meta.get("state_change", ""),
            "entities": new_meta.get("entities") or machine_meta.get("entities", []),
        })
        self._vec.update_memory_meta(
            memo_name,
            memory_type=new_meta["memory_type"],
            long_effect=new_meta["long_effect"],
            trigger_hint=new_meta["trigger_hint"],
            importance=new_meta["importance"],
            scene_anchor=machine_meta.get("scene_anchor"),
            retrieval_key=machine_meta.get("retrieval_key"),
            state_change=machine_meta.get("state_change"),
            entities=machine_meta.get("entities"),
        )
        if self._memos is not None and memo_name.startswith("memos/"):
            try:
                new_text = self._build_memo_text_with_meta(
                    ts_text, body, tags,
                    importance=new_meta["importance"],
                    manual=restored_manual,
                    memory_type=new_meta["memory_type"],
                    long_effect=new_meta["long_effect"],
                    trigger_hint=new_meta["trigger_hint"],
                    occurred_at=meta.get("occurred_at", ""),
                    time_basis=meta.get("time_basis", "unknown"),
                    machine_meta=machine_meta,
                )
                updated = await self._memos.update_memo(memo_name, new_text)
                record = dict(updated or {})
                record.setdefault("name", memo_name)
                record.setdefault("content", new_text)
                await self._index_memo_record(record, replace=True)
            except Exception as e:
                logger.debug("[memos-memory] retag update memos failed: %s", e)
        return {"memo_name": memo_name, **new_meta}

    # ---------- /memos-remember ----------
    @filter.command("memos-retag", alias={"重分类记忆", "记忆重分类"})
    async def cmd_retag(self, event: AstrMessageEvent):
        if not await self._ensure_init() or self._vec is None:
            yield event.plain_result("[记忆] 插件未就绪")
            return
        args = (event.message_str or "").strip().split()
        limit = 30
        only_missing = True
        if len(args) >= 2:
            try:
                limit = max(1, min(300, int(args[1])))
            except Exception:
                pass
        if len(args) >= 3 and args[2].lower() in ("all", "全部"):
            only_missing = False
        yield event.plain_result(f"[记忆] 开始 LLM 重分类，limit={limit}, only_missing={only_missing}，请稍等...")
        conn = self._vec._connect()
        if only_missing:
            rows = conn.execute(
                """SELECT memo_name FROM chunks
                   GROUP BY memo_name
                   HAVING COALESCE(MAX(long_effect),'')='' OR COALESCE(MAX(trigger_hint),'')=''
                      OR COALESCE(MAX(memory_type),'')=''
                   ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT memo_name FROM chunks GROUP BY memo_name
                   ORDER BY COALESCE(NULLIF(MAX(event_ts),0), NULLIF(MAX(source_created_ts),0), MAX(created_ts)) DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        names = [r["memo_name"] for r in rows]
        ok = 0
        fail = 0
        samples = []
        for mn in names:
            try:
                res = await self._retag_one_memo(mn)
                ok += 1
                if len(samples) < 3:
                    samples.append(f"{mn}: {res['memory_type']} ★{res['importance']}")
            except Exception as e:
                fail += 1
                logger.warning("[memos-memory] retag failed %s: %s", mn, e)
            await asyncio.sleep(0)
        try:
            self._rebuild_aux_indexes(rebuild_graph=False)
        except Exception:
            pass
        msg = f"[记忆] 重分类完成: 成功 {ok} 条，失败 {fail} 条"
        if samples:
            msg += "\n" + "\n".join(samples)
        yield event.plain_result(msg)

    @filter.command("memos-remember", alias={"记住", "存记忆"})
    async def cmd_remember(self, event: AstrMessageEvent):
        if not await self._ensure_init():
            yield event.plain_result("[记忆] 插件未就绪,请检查 memos 服务与 emb 配置")
            return
        text = (event.message_str or "").strip()
        for prefix in ("memos-remember", "记住", "存记忆"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if not text:
            yield event.plain_result("用法: /memos-remember 你想记的内容 (可选 #标签)")
            return
        tags: list[str] = []
        body_words: list[str] = []
        for w in text.split():
            if w.startswith("#") and len(w) > 1:
                tags.append(w)
            else:
                body_words.append(w)
        body = " ".join(body_words).strip()
        if not body:
            yield event.plain_result("内容为空, 已忽略")
            return
        diary = {
            "time_basis": "conversation_now",
            "content": body,
            "memory_type": _normalize_memory_type("", body, tags),
            "long_effect": "这是一条手动钉住的长期记忆，之后应优先影响角色反应。",
            "trigger_hint": "当当前话题与这条内容相关时，优先自然体现这条记忆。",
            "scene_anchor": re.split(r"(?<=[。！？!?])", body, maxsplit=1)[0][:120],
            "retrieval_key": body[:260],
            "state_change": "",
            "entities": [t.lstrip("#") for t in tags if t.strip("#")],
            "tags": tags + ["#手动", "#重要"],
            "importance": 5,
        }
        stored = await self._store_one_diary(diary, source_session=event.unified_msg_origin, importance=5, manual=True)
        if stored:
            if self._episodes is not None:
                try:
                    diary["_evidence_quality"] = "diary_derived"
                    await self._persist_episode_for_diary(
                        diary,
                        source_batch_id="",
                        source_kind="manual",
                    )
                except Exception as e:
                    logger.warning("[memos-memory][episode] 手动记忆事件卡写入失败，可用重建命令修复: %s", e)
            yield event.plain_result("已记住 (字数 " + str(len(body)) + ", 标签 " + str(len(tags)) + " 个)")
        else:
            yield event.plain_result("记忆写入失败，Memos 与本地索引都未成功，请检查日志后重试")

    async def _index_memo_record(self, memo: dict[str, Any], *, replace: bool = False) -> bool:
        """Build traceable passage rows for one complete Memos diary."""
        if self._vec is None:
            return False
        name = str(memo.get("name") or "").strip()
        raw_content = str(memo.get("content") or "").strip()
        if not name or not raw_content:
            return False
        ts_text, body, tags = _parse_memo_content(raw_content)
        if not body:
            return False
        restored_imp, restored_manual = _importance_from_memo_text(raw_content, tags)
        memory_type, long_effect, trigger_hint = _memory_meta_from_memo_text(raw_content, body, tags)
        machine = _machine_meta_from_memo_text(raw_content)
        if not machine.get("scene_anchor"):
            first_sentence = re.split(r"(?<=[。！？!?])", body, maxsplit=1)[0].strip()
            machine["scene_anchor"] = first_sentence[:120]
        if not machine.get("retrieval_key"):
            machine["retrieval_key"] = " ".join(
                [x for x in [" ".join(t.lstrip("#") for t in tags[:8]), body[:180]] if x]
            )[:260]
        if not isinstance(machine.get("entities"), list) or not machine.get("entities"):
            machine["entities"] = [t.lstrip("#") for t in tags[:12] if t.strip("#")]
        source_created_ts, source_updated_ts = memo_source_times(memo)
        time_meta = _time_meta_from_memo_text(
            raw_content, ts_text, source_created_ts=source_created_ts,
            timezone_name=self.rp_time_timezone,
        )
        if self.passage_index_enable:
            passages = self._split_into_passages(body, self.passage_max_chars, self.passage_overlap_chars)
        else:
            passages = self._split_into_passages(body, _DEFAULT_CHUNK_CHARS, _DEFAULT_OVERLAP_CHARS)
        chunks = [p["text"] for p in passages]
        embedding_texts = [self._passage_embedding_text(text, machine) for text in chunks]
        embeddings = await self._embed_batch(embedding_texts)
        if replace:
            self._vec.delete_memo_index(name)
        inserted = await self._vec.insert_chunks(
            memo_name=name, chunks=chunks, embeddings=embeddings,
            ts_text=ts_text, tags=tags,
            importance=restored_imp or self._auto_importance(body),
            source_session="", manual=restored_manual,
            memory_type=memory_type, long_effect=long_effect, trigger_hint=trigger_hint,
            occurred_at=time_meta["occurred_at"], event_ts=time_meta["event_ts"],
            time_basis=time_meta["time_basis"], source_created_ts=source_created_ts,
            source_updated_ts=source_updated_ts, passages=passages,
            content_hash=self._content_hash(body), scene_anchor=machine.get("scene_anchor", ""),
            retrieval_key=machine.get("retrieval_key", ""), state_change=machine.get("state_change", ""),
            entities=machine.get("entities", []),
        )
        return inserted > 0

    # ---------- /memos-reindex ----------
    @filter.command("memos-reindex", alias={"重灌向量"})
    async def cmd_reindex(self, event: AstrMessageEvent):
        if not await self._ensure_init():
            yield event.plain_result("[记忆] 插件未就绪")
            return
        if self._memos is None or self._vec is None:
            yield event.plain_result("[记忆] 依赖未就绪")
            return
        yield event.plain_result("[记忆] 开始全量重灌向量, 请稍候...")
        all_memos: list[dict] = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        try:
            while True:
                batch = await self._memos.list_memos(page_size=100, page_token=page_token)
                all_memos.extend(batch.get("memos", []))
                next_token = batch.get("next_page_token", "")
                if not next_token:
                    break
                if next_token == page_token or next_token in seen_page_tokens:
                    raise RuntimeError("Memos 返回重复 page token，已停止以避免无限循环")
                seen_page_tokens.add(next_token)
                page_token = next_token
        except Exception as e:
            yield event.plain_result("[记忆] 拉取 memos 失败: " + str(e))
            return
        await self._vec.clear_all()
        ok = 0
        fail = 0
        total = len(all_memos)
        batch_yield = 5
        progress_every = max(10, total // 5) if total else 10
        for idx, memo in enumerate(all_memos, 1):
            try:
                if await self._index_memo_record(memo, replace=False):
                    ok += 1
            except Exception as e:
                logger.warning("[memos-memory] reindex %s 失败: %s", memo.get("name", ""), e)
                fail += 1
            if idx % batch_yield == 0:
                await asyncio.sleep(0)
            if idx % progress_every == 0:
                logger.info("[memos-memory] reindex 进度 %d/%d (成功 %d, 失败 %d)", idx, total, ok, fail)
        self._vec.set_meta_value("passage_embedding_strategy", _PASSAGE_VECTOR_STRATEGY)
        self._passage_vector_migration_state = {
            "status": "ready", "strategy": _PASSAGE_VECTOR_STRATEGY,
            "updated": ok, "updated_ts": time.time(), "source": "full_reindex",
        }
        episode_result: dict[str, Any] = {}
        if self._episodes is not None:
            try:
                episode_result = await self._rebuild_episodic_from_memos(force=True, memos=all_memos)
            except Exception as e:
                logger.warning("[memos-memory][episode] reindex 事件卡重建失败，旧检索仍可用: %s", e)
                episode_result = {"failed": len(all_memos), "error": str(e)}
        kw_count, edge_count = self._rebuild_aux_indexes(rebuild_graph=True)
        yield event.plain_result(
            "[记忆] 重灌完成: 日记成功 %d 条, 失败 %d 条, 事件卡更新 %d 条, "
            "事件卡失败 %d 条, 关键词 %d 篇, 图谱 %d 边"
            % (
                ok, fail, int(episode_result.get("ok") or 0),
                int(episode_result.get("failed") or 0), kw_count, edge_count,
            )
        )

    @filter.command("memos-episodic-rebuild", alias={"重建情景记忆", "重建事件记忆"})
    async def cmd_episodic_rebuild(self, event: AstrMessageEvent):
        """Rebuild machine episode cards from Memos without rewriting source diaries."""
        if not await self._ensure_init() or self._memos is None or self._episodes is None:
            yield event.plain_result("[记忆] 情景记忆库未就绪")
            return
        yield event.plain_result("[记忆] 正在从 Memos 重建情景记忆。不会修改日记正文，请稍候...")
        try:
            result = await self._rebuild_episodic_from_memos(force=True)
        except Exception as e:
            yield event.plain_result("[记忆] 情景记忆重建失败: " + str(e))
            return
        yield event.plain_result(
            "[记忆] 情景记忆重建完成: 更新 %d，跳过 %d，删除 %d，失败 %d；"
            "现有 %d 个事件，原始对话可追溯 %d，旧日记推导 %d。"
            % (
                int(result.get("ok") or 0), int(result.get("skipped") or 0),
                int(result.get("removed") or 0), int(result.get("failed") or 0),
                int(result.get("episodes") or 0), int(result.get("source_grounded") or 0),
                int(result.get("diary_derived") or 0),
            )
        )

    @filter.command("memos-passage-rebuild", alias={"重建段落索引", "日记段落重建"})
    async def cmd_passage_rebuild(self, event: AstrMessageEvent):
        """Rebuild traceable passages for all old Memos without touching source diaries."""
        if not await self._ensure_init() or self._memos is None or self._vec is None:
            yield event.plain_result("[记忆] 插件未就绪")
            return
        yield event.plain_result("[记忆] 开始重建段落索引。不会修改 Memos 原文、查询反馈或画像，请稍候...")
        try:
            all_memos = await self._memos.list_all_memos(page_size=100)
        except Exception as e:
            yield event.plain_result("[记忆] 拉取 Memos 失败: " + str(e))
            return
        ok = 0
        fail = 0
        for idx, memo in enumerate(all_memos, 1):
            try:
                if await self._index_memo_record(memo, replace=True):
                    ok += 1
            except Exception as e:
                fail += 1
                logger.warning("[memos-memory] passage rebuild %s failed: %s", memo.get("name", ""), e)
            if idx % 5 == 0:
                await asyncio.sleep(0)
        kw_count, edge_count = self._rebuild_aux_indexes(rebuild_graph=True)
        self._vec.set_meta_value("passage_embedding_strategy", _PASSAGE_VECTOR_STRATEGY)
        self._passage_vector_migration_state = {
            "status": "ready", "strategy": _PASSAGE_VECTOR_STRATEGY,
            "updated": ok, "updated_ts": time.time(), "source": "passage_rebuild",
        }
        stats = self._vec.passage_index_stats()
        self._log_event("sync", f"段落索引重建: {ok}篇/{stats['passages']}段", {
            "ok": ok, "fail": fail, "passages": stats["passages"], "traced": stats["traced_passages"],
        })
        yield event.plain_result(
            "[记忆] 段落索引重建完成: 成功 %d 篇, 失败 %d 篇, 共 %d 段, 可追溯 %d 段, 关键词 %d 篇, 相似边 %d 条"
            % (ok, fail, stats["passages"], stats["traced_passages"], kw_count, edge_count)
        )

    # ---------- /memos-buffer-status ----------
    @filter.command("memos-buffer-status", alias={"缓冲状态", "记忆状态"})
    async def cmd_buffer_status(self, event: AstrMessageEvent):
        if self._vec is None:
            yield event.plain_result("[记忆] 插件未就绪")
            return
        try:
            rows = self._vec._connect().execute(
                """SELECT session_id, COUNT(*) AS n, MAX(created_ts) AS last_ts,
                          MIN(CASE WHEN COALESCE(event_ts,0)>0 THEN event_ts ELSE created_ts END) AS first_event_ts,
                          MAX(CASE WHEN COALESCE(event_ts,0)>0 THEN event_ts ELSE created_ts END) AS last_event_ts
                   FROM pending_messages GROUP BY session_id"""
            ).fetchall()
        except Exception as e:
            yield event.plain_result("[记忆] 读取缓冲失败: " + str(e))
            return
        if not rows:
            yield event.plain_result("[记忆] 缓冲为空(所有对话都已压缩入 memos)")
            return
        lines = ["[记忆] 当前缓冲积压:"]
        for r in rows:
            sid = r["session_id"]
            n = r["n"]
            last = r["last_ts"]
            ago = ""
            if last:
                ago_sec = int(time.time() - float(last))
                if ago_sec < 60:
                    ago = f"{ago_sec}秒前"
                elif ago_sec < 3600:
                    ago = f"{ago_sec//60}分前"
                else:
                    ago = f"{ago_sec//3600}时前"
            event_range = ""
            if r["first_event_ts"] and r["last_event_ts"]:
                tz = timezone_or_default(self.rp_time_timezone)
                start = datetime.fromtimestamp(float(r["first_event_ts"]), tz)
                end = datetime.fromtimestamp(float(r["last_event_ts"]), tz)
                start_text = start.strftime("%Y-%m-%d %H:%M")
                end_text = end.strftime("%Y-%m-%d %H:%M")
                event_range = start_text if start_text == end_text else f"{start_text} -> {end_text}"
            lines.append(f"  - {sid[:16]}...: {n} 条 (最近{ago}; 对话时间 {event_range or '未知'})")
        lines.append(f"累计压缩: {self._compress_count} 次, 注入顺序: {self.inject_order}, 格式: {self.inject_format}")
        lines.append(f"emb 缓存: {len(self._emb_cache)}/{self.emb_cache_size}")
        yield event.plain_result("\n".join(lines))

    # ---------- /memos-sync ----------
    @filter.command("memos-sync", alias={"同步记忆"})
    async def cmd_sync(self, event: AstrMessageEvent):
        if not await self._ensure_init() or self._memos is None or self._vec is None:
            yield event.plain_result("[记忆] 插件未就绪")
            return
        yield event.plain_result("[记忆] 开始同步 memos 与 vec, 请稍候...")
        all_memos: list[dict] = []
        page_token = ""
        seen_page_tokens: set[str] = set()
        try:
            while True:
                batch = await self._memos.list_memos(page_size=100, page_token=page_token)
                all_memos.extend(batch.get("memos", []))
                next_token = batch.get("next_page_token", "")
                if not next_token:
                    break
                if next_token == page_token or next_token in seen_page_tokens:
                    raise RuntimeError("Memos 返回重复 page token，已停止以避免无限循环")
                seen_page_tokens.add(next_token)
                page_token = next_token
        except Exception as e:
            yield event.plain_result("[记忆] 拉取 memos 失败: " + str(e))
            return
        try:
            existing = self._vec._connect().execute(
                "SELECT memo_name, MAX(source_updated_ts) AS ts FROM chunks GROUP BY memo_name"
            ).fetchall()
            existing_map = {r["memo_name"]: float(r["ts"]) for r in existing}
        except Exception:
            existing_map = {}
        synced = 0
        skipped = 0
        for memo in all_memos:
            name = memo.get("name", "")
            content = (memo.get("content") or "").strip()
            source_created_ts, source_updated_ts = memo_source_times(memo)
            mt = source_updated_ts
            if not content or not name:
                continue
            ts_old = existing_map.get(name, 0)
            if name in existing_map and ((mt and mt <= ts_old + 1) or (not mt and ts_old > 0)):
                skipped += 1
                continue
            try:
                if await self._index_memo_record(memo, replace=name in existing_map):
                    synced += 1
            except Exception as e:
                logger.warning("[memos-memory] sync %s failed: %s", name, e)
        # v1.4: 清理 vec 里已被 memos 删除的日记(之前只补不删)
        live_names = {m.get("name", "") for m in all_memos if m.get("name")}
        vec_names = self._vec.all_memo_names()
        stale = {n for n in vec_names if n.startswith("memos/") and n not in live_names}
        deleted = 0
        for name in stale:
            try:
                self._vec.delete_by_memo_name(name)
                if self._episodes is not None:
                    self._episodes.delete_by_memo_name(name)
                deleted += 1
            except Exception as e:
                logger.warning("[memos-memory] sync 删除失败 %s: %s", name, e)
        if deleted:
            logger.info("[memos-memory] sync 从 vec 清除 %d 篇已被 memos 删除的日记", deleted)
            self._log_event("sync", f"memos-sync 清理: 删除{deleted}篇过期日记", {"deleted": deleted})
            self._schedule_semantic_state_full_rebuild("memos_sync_delete")
        episode_result: dict[str, Any] = {}
        if self._episodes is not None:
            try:
                episode_result = await self._rebuild_episodic_from_memos(force=False, memos=all_memos)
            except Exception as e:
                logger.warning("[memos-memory][episode] sync 事件卡增量对接失败，旧检索仍可用: %s", e)
                episode_result = {"failed": len(all_memos), "error": str(e)}
        kw_count, edge_count = self._rebuild_aux_indexes(rebuild_graph=(synced > 0 or deleted > 0))

        yield event.plain_result(
            "[记忆] 同步完成: 日记补 %d 条, 删 %d 条, 跳过 %d 条；事件卡更新 %d 条, "
            "失败 %d 条；关键词 %d 篇, 图谱 %d 边 (vec 共 %d 篇)" % (
                synced, deleted, skipped, int(episode_result.get("ok") or 0),
                int(episode_result.get("failed") or 0), kw_count, edge_count,
                self._vec.distinct_memo_count()))

    # ---------- /memos-search(手动检索测试) ----------
    @filter.command("memos-search", alias={"搜索记忆", "检索记忆"})
    async def cmd_search(self, event: AstrMessageEvent):
        if not await self._ensure_init() or self._vec is None:
            yield event.plain_result("[记忆] 插件未就绪")
            return
        text = (event.message_str or "").strip()
        for prefix in ("memos-search", "搜索记忆", "检索记忆"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if not text:
            yield event.plain_result("用法: /memos-search 你想查的内容")
            return
        try:
            qv = await self._embed(text)
            hits = await self._vec.search_topk(
                qv, top_k=5, min_similarity=0.0,
                w_relevance=self.w_relevance,
                w_importance=self.w_importance,
                w_recency=self.w_recency,
                pin_boost=self.pin_boost,
                bm25_query=text,
                rerank_fn=self._rerank_fn,
            )
        except Exception as e:
            yield event.plain_result("[记忆] 检索失败: " + str(e))
            return
        if not hits:
            yield event.plain_result("[记忆] 召回 0 条 (库里没有或都低于阈值)")
            return
        lines = ["[记忆] 召回 top-%d (query=%s):" % (len(hits), text)]
        for i, h in enumerate(hits, 1):
            ts = h.get("ts_text", "")
            mn = h.get("memo_name", "")
            score = h.get("score", 0.0)
            rel = h.get("relevance", 0.0)
            imp = h.get("importance", 3)
            ct = (h.get("chunk_text", "") or "")[:60].replace("\n", " ")
            lines.append(
                "%d. [%s] score=%.3f rel=%.3f imp=%d | %s…"
                % (i, ts, score, rel, imp, ct)
            )
        yield event.plain_result("\n".join(lines))

    # ---------- 清理 ----------
    @staticmethod
    def _conversation_history_list(conversation: Any) -> list[dict]:
        if conversation is None:
            return []
        raw = getattr(conversation, "history", None)
        if raw is None:
            raw = getattr(conversation, "content", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if isinstance(raw, dict):
            raw = raw.get("history") or raw.get("messages") or raw.get("content") or []
        if not isinstance(raw, (list, tuple)):
            return []
        return [item if isinstance(item, dict) else {"role": "unknown", "content": str(item)} for item in raw]

    async def _current_conversation_history(self, event: AstrMessageEvent) -> tuple[Any, str | None, list[dict]]:
        mgr = getattr(self.context, "conversation_manager", None)
        if mgr is None:
            raise RuntimeError("AstrBot conversation_manager not available")
        umo = event.unified_msg_origin
        cid = await mgr.get_curr_conversation_id(umo)
        if not cid:
            return mgr, None, []
        conv = await mgr.get_conversation(umo, cid)
        if not conv:
            return mgr, cid, []
        return mgr, cid, self._conversation_history_list(conv)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        if not self.enable or not getattr(self, "context_exclude_command_turns", True):
            return
        if not self._event_is_command(event):
            return
        try:
            await self._remove_persisted_command_turn(event)
        except Exception as e:
            logger.warning("[memos-memory][context] 指令回合持久上下文清理失败: %s", e)

    def _context_archive_plan(self, history: list[dict]) -> dict[str, Any]:
        total = len(history)
        kept, removed = self._trim_context_history(history, self.context_archive_keep_recent_messages)
        return {
            "total": total,
            "kept": len(kept),
            "removed": removed,
            "will_archive": total >= self.context_archive_min_total_messages and removed > 0,
            "keep_recent_messages": self.context_archive_keep_recent_messages,
            "min_total_messages": self.context_archive_min_total_messages,
            "history": kept,
        }

    def _write_context_backup(self, umo: str, cid: str | None, history: list[dict], plan: dict[str, Any]) -> str:
        base = Path(os.path.expanduser(self.context_archive_backup_dir or "./data/astrbot_plugin_memos_memory/context_backups"))
        if not base.is_absolute():
            base = Path.cwd() / base
        base.mkdir(parents=True, exist_ok=True)
        safe_umo = re.sub(r"[^A-Za-z0-9_.-]+", "_", umo)[-80:]
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        path = base / f"{ts}_{safe_umo}_{cid or 'no-cid'}.json"
        payload = {
            "plugin": "astrbot_plugin_memos_memory",
            "version": _PLUGIN_VERSION,
            "created_ts": time.time(),
            "unified_msg_origin": umo,
            "conversation_id": cid,
            "plan": {k: v for k, v in plan.items() if k != "history"},
            "history": history,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    async def _archive_current_context(self, event: AstrMessageEvent, execute: bool = False) -> dict[str, Any]:
        mgr, cid, history = await self._current_conversation_history(event)
        plan = self._context_archive_plan(history)
        plan["conversation_id"] = cid
        if not execute or not plan["will_archive"] or not cid:
            return plan
        backup_path = self._write_context_backup(event.unified_msg_origin, cid, history, plan)
        await mgr.update_conversation(event.unified_msg_origin, cid, history=plan["history"])
        plan["backup_path"] = backup_path
        self._log_event("system", f"context archived: {plan['total']}->{plan['kept']}", {
            "conversation_id": cid,
            "removed": plan["removed"],
            "backup_path": backup_path,
        })
        return plan

    async def _archive_seen_session(self, umo: str) -> dict[str, Any] | None:
        mgr = getattr(self.context, "conversation_manager", None)
        if mgr is None:
            return None
        cid = await mgr.get_curr_conversation_id(umo)
        if not cid:
            return None
        conv = await mgr.get_conversation(umo, cid)
        if not conv:
            return None
        history = self._conversation_history_list(conv)
        plan = self._context_archive_plan(history)
        if not plan["will_archive"]:
            return plan
        backup_path = self._write_context_backup(umo, cid, history, plan)
        await mgr.update_conversation(umo, cid, history=plan["history"])
        plan["backup_path"] = backup_path
        self._log_event("system", f"context auto-archived: {plan['total']}->{plan['kept']}", {
            "conversation_id": cid,
            "removed": plan["removed"],
        })
        return plan

    async def _context_archive_loop(self):
        interval = max(1, self.context_archive_interval_days) * 86400
        await asyncio.sleep(30)
        while True:
            try:
                if self.context_archive_enable:
                    for umo in list(self._seen_context_sessions):
                        try:
                            await self._archive_seen_session(umo)
                        except Exception as e:
                            logger.debug("[memos-memory] context auto archive failed for %s: %s", umo, e)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[memos-memory] context archive loop failed: %s", e)
                await asyncio.sleep(min(interval, 3600))

    @filter.command("memos-context-status", alias={"上下文状态", "记忆上下文状态"})
    async def cmd_context_status(self, event: AstrMessageEvent):
        try:
            _, cid, history = await self._current_conversation_history(event)
            stat = self._context_stats.get(event.unified_msg_origin, {})
            yield event.plain_result(
                f"[上下文] 当前会话={cid or '-'} | 持久历史={len(history)}条 | "
                f"请求裁剪={stat.get('total', 0)}->{stat.get('kept', 0)} "
                f"(移除{stat.get('trimmed', 0)}条，仅影响当次请求)"
            )
        except Exception as e:
            yield event.plain_result(f"[上下文] 状态读取失败: {e}")

    @filter.command("memos-context-preview", alias={"上下文归档预览"})
    async def cmd_context_preview(self, event: AstrMessageEvent):
        try:
            _, cid, history = await self._current_conversation_history(event)
            plan = self._context_archive_plan(history)
            yield event.plain_result(
                f"[上下文] 归档预览 会话={cid or '-'} | 总计{plan['total']}条 | "
                f"保留{plan['kept']}条 | 可移除{plan['removed']}条 | "
                f"{'可执行 /memos-context-archive exec' if plan['will_archive'] else '未达到归档阈值'}"
            )
        except Exception as e:
            yield event.plain_result(f"[上下文] 预览失败: {e}")

    @filter.command("memos-context-archive", alias={"上下文归档"})
    async def cmd_context_archive(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip().lower()
        execute = any(x in text.split() for x in {"exec", "execute", "确认", "执行"})
        try:
            plan = await self._archive_current_context(event, execute=execute)
            if not execute:
                yield event.plain_result(
                    f"[上下文] 默认只预览: 总计{plan['total']}条, 保留{plan['kept']}条, "
                    f"可移除{plan['removed']}条。确认执行: /memos-context-archive exec"
                )
                return
            if not plan.get("will_archive"):
                yield event.plain_result(
                    f"[上下文] 未归档: 总计{plan['total']}条, 阈值{plan['min_total_messages']}条, "
                    f"可移除{plan['removed']}条。"
                )
                return
            yield event.plain_result(
                f"[上下文] 已归档: {plan['total']}->{plan['kept']}条，移除{plan['removed']}条。"
                f"备份: {plan.get('backup_path', '-')}"
            )
        except Exception as e:
            yield event.plain_result(f"[上下文] 归档失败: {e}")

    @filter.command("memos-mind-status", alias={"心潮状态", "心理状态"})
    async def cmd_xinchao_status(self, event: AstrMessageEvent):
        try:
            data = await self._xinchao.status()
            drives = " / ".join(
                f"{item['label']} {float(item['value']):.2f}"
                for item in data.get("topDrives", [])[:4]
            ) or "尚无显著倾向"
            intent = data.get("intent") or {}
            perception = data.get("lastPerception") or {}
            yield event.plain_result(
                "[心潮] "
                f"{data.get('consciousness', 'awake')} | revision {data.get('revision', 0)} | "
                f"疲劳 {float(data.get('fatigue') or 0):.2f}\n"
                f"主要倾向: {drives}\n"
                f"当前意向: {intent.get('label') or '未形成'} | "
                f"最近感知: {perception.get('source') or '暂无'}"
            )
        except Exception as e:
            yield event.plain_result(f"[心潮] 读取失败: {e}")

    @filter.command("memos-mind-settle", alias={"心潮结算"})
    async def cmd_xinchao_settle(self, event: AstrMessageEvent):
        try:
            data = await self._xinchao.settle_now("")
            yield event.plain_result(
                f"[心潮] 结算完成: revision {data.get('revision', 0)}，"
                f"意识={data.get('consciousness', 'awake')}，疲劳={float(data.get('fatigue') or 0):.2f}"
            )
        except Exception as e:
            yield event.plain_result(f"[心潮] 结算失败: {e}")

    @filter.command("memos-mind-feedback", alias={"心潮反馈"})
    async def cmd_xinchao_feedback(self, event: AstrMessageEvent):
        args = (event.message_str or "").strip().split()
        if len(args) < 3:
            yield event.plain_result(
                "[心潮] 用法: /memos-mind-feedback <drive> <delta>\n"
                "drive: possess monitor crave share libido curiosity boredom social duty reflection grieve anger"
            )
            return
        try:
            data = await self._xinchao.feedback("", args[1], float(args[2]))
            value = next(
                (item["value"] for item in data.get("drives", []) if item["key"] == args[1]),
                0,
            )
            yield event.plain_result(f"[心潮] {args[1]} 已调整为 {float(value):.2f}")
        except Exception as e:
            yield event.plain_result(f"[心潮] 反馈失败: {e}")

    @filter.command("memos-mind-thought", alias={"加入心潮念头"})
    async def cmd_xinchao_thought(self, event: AstrMessageEvent):
        args = (event.message_str or "").strip().split(maxsplit=3)
        if len(args) < 4:
            yield event.plain_result(
                "[心潮] 用法: /memos-mind-thought <drive> <0.1-1.0> <念头>"
            )
            return
        try:
            data = await self._xinchao.add_thought("", args[1], args[3], float(args[2]))
            yield event.plain_result(
                f"[心潮] 已加入闪念，revision {data.get('revision', 0)}"
            )
        except Exception as e:
            yield event.plain_result(f"[心潮] 加入失败: {e}")

    @filter.command("memos-mind-reset", alias={"重置心潮"})
    async def cmd_xinchao_reset(self, event: AstrMessageEvent):
        args = (event.message_str or "").strip().split()
        if len(args) < 2 or args[1] != "CONFIRM":
            yield event.plain_result("[心潮] 此操作会清空当前心潮状态。确认执行: /memos-mind-reset CONFIRM")
            return
        try:
            data = await self._xinchao.reset("")
            yield event.plain_result(f"[心潮] 已重置，revision {data.get('revision', 0)}")
        except Exception as e:
            yield event.plain_result(f"[心潮] 重置失败: {e}")

    async def initialize(self) -> None:
        """AstrBot 插件激活时调用(事件循环已在运行),启动 WebUI。"""
        try:
            await self._apply_pending_data_restore_once()
            # v4.5.0: warm up the core index and legacy-episode bridge in the
            # background at activation so the first real request does not race
            # the migration and fall back to the legacy recall chain. Fail-open:
            # a warmup failure leaves lazy init to the first request as before.
            if self._warmup_task is None or self._warmup_task.done():
                async def _warmup():
                    try:
                        await self._ensure_init()
                    except Exception as exc:
                        logger.debug("[memos-memory] background warmup failed open: %s", exc)
                self._warmup_task = asyncio.create_task(_warmup())
            try:
                await self._xinchao.initialize()
            except Exception as e:
                logger.warning("[memos-memory][xinchao] initialize failed open: %s", e)
            try:
                await self._time_insight.initialize()
            except Exception as e:
                logger.warning("[memos-memory][time-insight] initialize failed open: %s", e)
            # v4.5.0: managed Memos is started by the warmup's _ensure_init
            # (behind _init_lock); a second direct start here could race it.
            if self.memos_mode == "managed" and (self._warmup_task is None or self._warmup_task.done()):
                await self._managed_memos.start()
            if self.context_archive_enable and self.context_archive_interval_days > 0 and self._context_archive_task is None:
                self._context_archive_task = asyncio.create_task(self._context_archive_loop())
                logger.info("[memos-memory] context archive loop enabled, interval=%d days", self.context_archive_interval_days)
            await self._ensure_webui_started()
        except Exception as e:
            logger.warning("[memos-memory] WebUI 启动失败: %s", e)

    @filter.command("memos-insight-update", alias={"更新记忆洞察"})
    async def cmd_time_insight_update(self, event: AstrMessageEvent):
        try:
            result = await self._time_insight.update("manual", event.unified_msg_origin)
            if not result.get("updated"):
                reason = result.get("reason", "unknown")
                if reason == "external_affiliate_active":
                    yield event.plain_result("[时间洞察] 旧附属插件正在运行，内置引擎已让出；请使用附属插件更新或将其停用。")
                else:
                    yield event.plain_result(f"[时间洞察] 未更新: {reason}")
                return
            yield event.plain_result(
                f"[时间洞察] v3 更新完成 | 来源 {result.get('source_memories', 0)} | "
                f"有效日期 {result.get('dated_memories', 0)} | 候选 {result.get('candidate_count', 0)} | "
                f"环境洞察 {result.get('static_selected', 0)} | LLM {result.get('llm_status', 'disabled')}"
            )
        except Exception as e:
            yield event.plain_result(f"[时间洞察] 更新失败: {e}")

    @filter.command("memos-insight-preview", alias={"预览记忆洞察"})
    async def cmd_time_insight_preview(self, event: AstrMessageEvent):
        try:
            result = await self._time_insight.preview()
            candidates = result.get("candidates") or []
            lines = [
                f"- {item.get('kind')} score={float(item.get('score') or 0):.2f} "
                f"confidence={float(item.get('confidence') or 0):.2f}"
                for item in candidates[:8]
            ]
            yield event.plain_result(
                f"[时间洞察预览] 来源 {result.get('source_memories', 0)} | "
                f"有效日期 {result.get('dated_memories', 0)} | 候选 {result.get('candidate_count', 0)}\n"
                + ("\n".join(lines) if lines else "- 无合格候选")
                + "\n\n"
                + (result.get("ambient_block") or "当前没有达到环境注入门槛的洞察。")
            )
        except Exception as e:
            yield event.plain_result(f"[时间洞察预览] 失败: {e}")

    @filter.command("memos-insight-status", alias={"记忆洞察状态"})
    async def cmd_time_insight_status(self, event: AstrMessageEvent):
        status = await self._time_insight.status()
        stats = status.get("stats") or {}
        mode = "旧附属兼容" if status.get("external_affiliate_active") else "主插件内置"
        yield event.plain_result(
            f"[时间洞察] v3 | {mode} | {'启用' if status.get('enabled') else '关闭'}\n"
            f"来源 {stats.get('source_memories', 0)} | 有效日期 {stats.get('dated_memories', 0)} | "
            f"候选 {status.get('candidate_count', 0)} | 环境 {stats.get('static_selected', 0)} | "
            f"最近按问题补充 {((status.get('last_runtime') or {}).get('stats') or {}).get('selected', 0)}"
        )

    @filter.command("memos-profile-update", alias={"更新记忆画像", "更新人格画像"})
    async def cmd_profile_update(self, event: AstrMessageEvent):
        if not self.enable_affiliate_profile:
            yield event.plain_result("[画像] 内置长期画像未启用")
            return
        if not await self._ensure_init():
            yield event.plain_result("[画像] 插件初始化失败")
            return
        yield event.plain_result("[画像] 开始融合更新长期人格画像，请稍等...")
        try:
            res = await self._profile_update("manual")
            yield event.plain_result(
                f"[画像] 更新完成: v{res['version']}，{res['chars']}字，稳定事实约{res['facts']}条，来源{res['source_count']}条"
            )
        except Exception as e:
            try:
                if self._vec is not None:
                    conn = self._vec._connect()
                    self._init_profile_schema(conn)
                    conn.execute(
                        "INSERT INTO memory_affiliate_runs(created_ts,status,message,source_count) VALUES (?,?,?,?)",
                        (time.time(), "failed", str(e), 0),
                    )
                    conn.commit()
            except Exception:
                pass
            yield event.plain_result(f"[画像] 更新失败: {e}")

    @filter.command("memos-profile-status", alias={"记忆画像状态", "人格画像状态"})
    async def cmd_profile_status(self, event: AstrMessageEvent):
        if not await self._ensure_init():
            yield event.plain_result("[画像] 插件初始化失败")
            return
        st = self._affiliate_profile_status()
        if not st.get("connected"):
            yield event.plain_result(f"[画像] 尚未生成画像: {st.get('reason', 'no profile')}。使用 /memos-profile-update")
            return
        yield event.plain_result(
            f"[画像] v{st.get('version')} | {st.get('profile_chars', 0)}字 | "
            f"{st.get('age_days', 0)}天前 | 来源{st.get('source_count', 0)}条 | "
            f"{'由滚动状态接管注入' if self.semantic_state_enable and self.semantic_state_replace_profile else ('可注入' if st.get('fresh', True) else '已过期')}"
        )

    @filter.command("memos-state-status", alias={"滚动状态", "当前记忆状态"})
    async def cmd_semantic_state_status(self, event: AstrMessageEvent):
        if not await self._ensure_init():
            yield event.plain_result("[状态] 插件初始化失败")
            return
        status = self._semantic_state_status()
        if not status.get("ready"):
            yield event.plain_result(
                f"[状态] 尚未建立滚动状态 | pending={status.get('pending', 0)} | "
                "可等待自动初始化或使用 /memos-state-rebuild"
            )
            return
        state = status.get("state") or {}
        yield event.plain_result(
            f"[状态] v{state.get('version', 0)} | {len(state.get('rendered_text') or '')}字 | "
            f"pending={status.get('pending', 0)} | 已接管长期人格注入"
        )

    @filter.command("memos-state-rebuild", alias={"重建滚动状态", "重建当前状态"})
    async def cmd_semantic_state_rebuild(self, event: AstrMessageEvent):
        if not await self._ensure_init():
            yield event.plain_result("[状态] 插件初始化失败")
            return
        yield event.plain_result("[状态] 正在融合现有 4.0 情景记忆并重建当前状态，请稍等...")
        try:
            result = await self._bootstrap_semantic_state(force=True)
            if not result.get("updated"):
                yield event.plain_result(f"[状态] 未更新: {result.get('reason', 'unknown')}")
                return
            yield event.plain_result(
                f"[状态] 重建完成: v{result.get('version')} | {len(result.get('rendered_text') or '')}字"
            )
        except Exception as exc:
            yield event.plain_result(f"[状态] 重建失败: {exc}")

    async def _command_rebuild_similarity_clusters(self, event: AstrMessageEvent, label: str = "相似簇"):
        if not await self._ensure_init():
            yield event.plain_result("[记忆] 初始化失败")
            return
        yield event.plain_result(f"[记忆] 正在重建{label}...")
        try:
            stats = self._build_similarity_cluster_index()
            yield event.plain_result(
                f"[记忆] {label}重建完成: {stats.get('memos', 0)} 篇, "
                f"{stats.get('clusters', 0)} 个簇, 多成员簇 {stats.get('real_clusters', 0)} 个, "
                f"{stats.get('edges', 0)} 条相似边, 阈值 {float(stats.get('threshold') or 0):.2f}"
            )
        except Exception as e:
            yield event.plain_result(f"[记忆] {label}重建失败: {e}")

    @filter.command("memos-graph-rebuild", alias={"重建图谱"})
    async def cmd_graph_rebuild(self, event: AstrMessageEvent):
        async for r in self._command_rebuild_similarity_clusters(event, label="相似簇图"):
            yield r

    @filter.command("memos-cluster-rebuild", alias={"重建相似簇", "记忆重新分簇"})
    async def cmd_cluster_rebuild(self, event: AstrMessageEvent):
        async for r in self._command_rebuild_similarity_clusters(event, label="相似簇"):
            yield r

    @filter.command("memos-health", alias={"记忆健康检查"})
    async def cmd_health(self, event: AstrMessageEvent):
        if not await self._ensure_init() or self._vec is None:
            yield event.plain_result("[健康] 插件未就绪")
            return
        try:
            data = self._vec.health_check()
            lines = [
                f"[健康] 记忆 {data['total_memos']} 篇 / chunks {data['total_chunks']} / 问题 {data['issue_count']} 个",
                f"高 {data['counts']['high']} / 中 {data['counts']['medium']} / 低 {data['counts']['low']}",
            ]
            for issue in data["issues"][:12]:
                memo = issue.get("memo_name") or "-"
                lines.append(f"- [{issue['severity']}] {issue['kind']} {memo}: {issue['message']}")
            if data["issue_count"] > 12:
                lines.append("更多请在 WebUI 健康检查页查看。")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            yield event.plain_result(f"[健康] 检查失败: {e}")

    @filter.command("memos-managed-status", alias={"托管memos状态", "memos托管状态"})
    async def cmd_managed_memos_status(self, event: AstrMessageEvent):
        st = self._managed_memos.as_dict()
        lines = [
            f"[托管 Memos] mode={st['mode']} status={st['status']}",
            f"URL: {st['base_url']}",
            f"exe: {st['exe_path']} ({'存在' if st['exe_exists'] else '缺失'})",
            f"data: {st['data_dir']}",
            f"process: {'running pid=' + str(st['pid']) if st.get('process_running') else 'not managed/running'}",
        ]
        if st.get("message"):
            lines.append("message: " + str(st["message"]))
        if st.get("log_path"):
            lines.append("log: " + str(st["log_path"]))
        if st.get("state_path"):
            lines.append("state: " + str(st["state_path"]))
        yield event.plain_result("\n".join(lines))

    def _plan_eod_checkpoint(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Size an evidence-extraction window without forcing diary count."""
        assistant_messages = [
            message for message in messages if message.get("role") == "assistant"
        ]
        turns = len(assistant_messages)
        dates = recorded_message_dates(messages, self.rp_time_timezone)
        windows = 1 if turns else 0
        previous_ts = 0.0
        previous_date = ""
        for message in assistant_messages:
            try:
                event_ts = float(message.get("event_ts") or 0.0)
            except (TypeError, ValueError):
                event_ts = 0.0
            current_date = ""
            if event_ts > 0:
                current_date = datetime.fromtimestamp(
                    event_ts, timezone_or_default(self.rp_time_timezone)
                ).strftime("%Y-%m-%d")
            if previous_ts > 0 and event_ts > 0:
                # A two-hour real pause or a local-date transition is a useful
                # scene-boundary hint. The LLM may still merge a continuous arc.
                if current_date != previous_date or event_ts - previous_ts >= 2 * 3600:
                    windows += 1
            previous_ts = event_ts or previous_ts
            previous_date = current_date or previous_date
        density_capacity = max(1, (turns + 7) // 8) if turns else 0
        configured_cap = max(
            1, int(getattr(self, "eod_checkpoint_max_diaries", 6) or 6)
        )
        capacity = min(
            configured_cap,
            max(1, len(dates), windows, density_capacity),
        ) if turns else 0
        # _compress_and_store raises this lower bound to the number of represented
        # dates, so a long cross-day backlog never loses calendar coverage.
        return {
            "turns": turns,
            "dates": dates,
            "scene_windows": windows,
            "density_capacity": density_capacity,
            "diary_capacity": capacity,
            "message_count": len(messages),
            "chars": sum(len(str(item.get("content") or "")) for item in messages),
        }

    async def _run_eod_flush_once(self, now: datetime | None = None) -> dict[str, Any]:
        """Run the bounded 23:45 memory checkpoint with snapshot-aware retries."""
        now = now or datetime.now(timezone_or_default(self.rp_time_timezone))
        stats: dict[str, Any] = {
            "attempted": 0, "written": 0, "cooldown": 0, "busy": 0,
            "planned": 0, "insight_refresh": False, "sessions": [],
        }
        if (
            now.hour != 23
            or now.minute < 45
            or self._vec is None
            or not self.enable_auto_compress
            or not self.eod_checkpoint_enable
        ):
            return stats
        today_str = now.strftime("%Y-%m-%d")
        now_ts = now.timestamp()
        for umo in list(self._buffer):
            lock = self._compress_locks.setdefault(umo, asyncio.Lock())
            if lock.locked():
                stats["busy"] += 1
                continue
            snapshot, snapshot_seq = await self._vec.buffer_snapshot(
                umo, self.compress_batch_max_messages,
            )
            snapshot_seq_int = int(snapshot_seq if snapshot_seq is not None else -1)
            done = self._eod_flush_done.get(umo)
            if isinstance(done, dict) and done.get("date") == today_str:
                if int(done.get("snapshot_seq") if done.get("snapshot_seq") is not None else -1) >= snapshot_seq_int:
                    continue
            plan = self._plan_eod_checkpoint(snapshot)
            if plan["turns"] < self.eod_checkpoint_min_turns:
                continue
            retry = self._eod_flush_retry.get(umo) or {}
            if retry.get("date") != today_str or int(
                retry.get("snapshot_seq") if retry.get("snapshot_seq") is not None else -1
            ) != snapshot_seq_int:
                retry = {
                    "date": today_str, "snapshot_seq": snapshot_seq_int,
                    "attempts": 0, "next_ts": 0.0,
                }
            if int(retry.get("attempts") or 0) >= 3 or now_ts < float(retry.get("next_ts") or 0.0):
                stats["cooldown"] += 1
                continue
            eod_count = int(plan["diary_capacity"])
            stats["attempted"] += 1
            stats["planned"] += eod_count
            session_diag = {"session": umo[:12], "snapshot_seq": snapshot_seq_int, **plan}
            stats["sessions"].append(session_diag)
            logger.info(
                "[memos-memory][eod] checkpoint: %d turns, %d dates, %d windows -> up to %d diaries",
                plan["turns"], len(plan["dates"]), plan["scene_windows"], eod_count,
            )
            self._log_event(
                "compress", f"夜间检查点: {plan['turns']}轮 -> 最多{eod_count}个情景",
                {**session_diag, "attempt": int(retry["attempts"]) + 1},
            )
            try:
                written = await self._compress_with_lock(
                    umo, snapshot, diary_count=eod_count, source_kind="eod",
                    buffer_up_to_seq=snapshot_seq,
                )
            except Exception as exc:
                logger.warning("[memos-memory][eod] compress failed: %s", exc)
                written = 0
            if written > 0:
                stats["written"] += written
                self._eod_flush_retry.pop(umo, None)
                self._buffer_last_turn[umo] = 0
                remaining, _ = await self._vec.buffer_snapshot(
                    umo, self.compress_batch_max_messages,
                )
                remaining_turns = sum(
                    1 for message in remaining if message.get("role") == "assistant"
                )
                if remaining_turns < self.eod_checkpoint_min_turns:
                    self._eod_flush_done[umo] = {
                        "date": today_str, "snapshot_seq": snapshot_seq_int,
                    }
                logger.info(
                    "[memos-memory][eod] committed: %d diaries | raw archive + episode view + state queue ready",
                    written,
                )
                continue
            attempts = int(retry.get("attempts") or 0) + 1
            retry = {
                "date": today_str,
                "snapshot_seq": snapshot_seq_int,
                "attempts": attempts,
                "next_ts": now_ts + 300.0,
            }
            self._eod_flush_retry[umo] = retry
            logger.warning(
                "[memos-memory][eod] no diary written; retry %d/3 after 5 minutes",
                attempts,
            )
            self._log_event(
                "compress", "保底压缩未写入，进入退避",
                {"session": umo[:12], "attempt": attempts, "max_attempts": 3, "retry_after_seconds": 300},
            )
        if stats["written"] > 0:
            schedule_refresh = getattr(
                getattr(self, "_time_insight", None), "schedule_refresh", None
            )
            if callable(schedule_refresh):
                stats["insight_refresh"] = bool(schedule_refresh("eod_checkpoint"))
        self._eod_last_status = {"ts": now_ts, "date": today_str, **stats}
        return stats

    async def _eod_flush_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                await self._run_eod_flush_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("[memos-memory][eod] error: %s", e)

    def _data_backup_status(self) -> dict[str, Any]:
        try:
            status = self._data_backup.status()
            status["startup_restore"] = dict(self._startup_restore_result)
            return status
        except Exception as exc:
            return {
                "enabled": self.data_backup_enable,
                "interval_days": self.data_backup_interval_days,
                "keep": self.data_backup_keep,
                "backup_dir": self.data_backup_dir,
                "due": False,
                "error": str(exc),
            }

    async def _apply_pending_data_restore_once(self) -> dict[str, Any]:
        if self._pending_restore_checked:
            return dict(self._startup_restore_result)
        async with self._restore_apply_lock:
            if self._pending_restore_checked:
                return dict(self._startup_restore_result)
            try:
                result = await asyncio.to_thread(self._data_backup.apply_pending_restore)
            except Exception as exc:
                result = {"applied": False, "reason": "failed", "error": str(exc)[:500]}
            self._startup_restore_result = dict(result or {})
            self._pending_restore_checked = True
            if result.get("applied"):
                logger.warning(
                    "[memos-memory][backup] startup restore applied file=%s safety=%s",
                    result.get("file"), result.get("safety_backup"),
                )
            elif result.get("reason") == "failed":
                logger.error(
                    "[memos-memory][backup] startup restore failed open: %s",
                    result.get("error"),
                )
            return dict(self._startup_restore_result)

    async def _data_backup_list(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._data_backup.list_archives)

    async def _data_backup_inspect(self, file_name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._data_backup.inspect, file_name)

    async def _prepare_data_restore(self, file_name: str) -> dict[str, Any]:
        result = await asyncio.to_thread(self._data_backup.prepare_restore, file_name)
        pending = result.get("pending_restore") or {}
        logger.warning(
            "[memos-memory][backup] restore scheduled for next reload file=%s safety=%s",
            file_name, pending.get("safety_backup"),
        )
        self._log_event("system", "已预约下次重载恢复插件数据", {
            "file": file_name,
            "safety_backup": pending.get("safety_backup"),
        })
        return result

    async def _cancel_data_restore(self) -> dict[str, Any]:
        result = await asyncio.to_thread(self._data_backup.cancel_restore)
        if result.get("cancelled"):
            logger.info("[memos-memory][backup] pending restore cancelled")
        return result

    async def _delete_data_backup(self, file_name: str) -> dict[str, Any]:
        result = await asyncio.to_thread(self._data_backup.delete_archive, file_name)
        logger.info("[memos-memory][backup] deleted %s", file_name)
        return result

    async def _run_data_backup_once(
        self,
        *,
        force: bool = False,
        reason: str = "scheduled",
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(
            self._data_backup.create,
            force=force,
            reason=reason,
        )
        if result.get("created"):
            self._log_event("system", "插件数据备份完成", {
                "file": result.get("file"),
                "bytes": result.get("bytes"),
                "reason": reason,
                "pruned": result.get("pruned", 0),
            })
            logger.info(
                "[memos-memory][backup] created %s bytes=%s reason=%s",
                result.get("file"), result.get("bytes"), reason,
            )
        elif result.get("reason") == "failed":
            self._log_event("system", "插件数据备份失败", {
                "error": result.get("error", "")[:300],
                "reason": reason,
            })
            logger.warning(
                "[memos-memory][backup] failed open: %s", result.get("error")
            )
        return result

    async def _data_backup_loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self._run_data_backup_once(reason="scheduled")
                await asyncio.sleep(6 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[memos-memory][backup] scheduler failed open: %s", exc)
                await asyncio.sleep(1800)

    async def terminate(self):
        try:
            self._save_runtime_telemetry(force=True)
        except Exception:
            pass
        try:
            await self._xinchao.terminate()
        except Exception:
            pass
        try:
            await self._time_insight.terminate()
        except Exception:
            pass
        owned_tasks = []
        for tn in (
            "_warmup_task", "_reconcile_task", "_eod_flush_task", "_profile_task",
            "_context_archive_task", "_data_backup_task", "_episode_migration_task", "_passage_vector_migration_task",
            "_source_turn_vector_migration_task",
            "_semantic_state_task",
        ):
            t = getattr(self, tn, None)
            if t is not None:
                try:
                    t.cancel()
                    owned_tasks.append(t)
                except Exception:
                    pass
        for task in list(getattr(self, "_semantic_state_pending_tasks", set())):
            try:
                task.cancel()
                owned_tasks.append(task)
            except Exception:
                pass
        if owned_tasks:
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        # 停 WebUI
        webui = getattr(self, "_webui", None)
        if webui is not None:
            try:
                await webui.stop()
            except Exception:
                pass
            self._webui = None
        try:
            if self._memos is not None:
                await self._memos.close()
        except Exception:
            pass
        try:
            await self._managed_memos.stop()
        except Exception:
            pass
        try:
            if self._vec is not None:
                self._vec.close()
        except Exception:
            pass
        try:
            if self._episodes is not None:
                self._episodes.close()
        except Exception:
            pass
        logger.info("[memos-memory] terminated")
