"""
Repetition Guard - Anti-repetition module for RP scenarios

Two detection systems:
1. RepetitionGuide: Detects repetitive openers and high-frequency phrases
   in assistant responses, injects soft guidance to diversify expression.

2. MirrorAlert: Detects when assistant mirrors/parrots user's words directly,
   injects guidance to respond with original thought instead.

Design principles:
- REDUCE, never BAN: all guidance is soft ("try to...", not "never...")
- Only scans assistant turns (user content is never flagged)
- Uses pure text algorithms (no LLM API calls, zero extra cost)
- All injections are mark_as_temp (don't pollute history)
- Preserves character voice while encouraging variety
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RepetitionReport:
    """Result of repetition analysis"""
    repeated_openers: list[tuple[str, int]] = field(default_factory=list)
    """(opener_text, count) pairs"""
    repeated_phrases: list[tuple[str, int]] = field(default_factory=list)
    """(phrase_text, count) pairs"""
    mirror_count: int = 0
    mirror_examples: list[tuple[str, str]] = field(default_factory=list)
    """(user_text_snippet, assistant_mirror_snippet) pairs"""
    total_assistant_turns: int = 0
    """Recent K assistant turns (truncated by k_recent)"""
    absolute_assistant_turns: int = 0
    """Untruncated total assistant turns - used for cooldown timing"""
    should_inject_repetition: bool = False
    should_inject_mirror: bool = False
    # v6.0: extended fields for tier-based injection and over-flag protection
    structural_mirror_count: int = 0
    """Sub-count of structural mirrors (declarative -> question flips)"""
    synonym_mirror_count: int = 0
    """Sub-count of mirrors caught via synonym library (higher threshold required)"""
    verbal_tier: int = 0
    """0 = quiet (log only), 1 = standard inject, 2 = detailed with concrete suggestions. Default 0 ensures early-return short convs never inject."""
    cooldown_active: bool = False
    """True when within cooldown window of a prior MirrorAlert injection"""
    skipped_safe_responses: int = 0
    """Count of responses skipped due to verbatim-safe blacklist (for diagnostics)"""
    cluster_mirror_count: int = 0
    """How many pairs hit same action semantic cluster via different verbs (v1.6.5/v1.2.5)"""


# === v6.0: Synonym library for structural mirror detection ===
# Catches user "我吃好了" -> asst "你吃饱了？" (predicates differ but mean the same)
# Library only covers RP-common predicates; pure synonym hits need 2x threshold to inject.
_STRUCTURAL_SYNONYMS: dict[str, list[str]] = {
    # Eating / drinking
    "吃好": ["吃饱", "吃完", "吃了", "吃过了", "用餐了"],
    "吃饱": ["吃好", "吃完", "吃了"],
    "吃完": ["吃好", "吃饱", "吃了"],
    "喝了": ["饮了", "喝完", "饮尽"],
    # Movement
    "去过": ["到过", "赴过", "去过了"],
    "回来": ["归来", "返回", "回到了"],
    "出门": ["出去", "外出"],
    # Sleep / rest
    "想睡": ["要睡", "困了", "想歇"],
    "睡了": ["歇了", "入睡了", "安歇"],
    "醒了": ["起身", "起来"],
    # Decisions / intent
    "决定": ["打算", "决意", "想好"],
    "想去": ["打算去", "准备去"],
    "想做": ["打算做", "想试试"],
    # Emotion / state
    "喜欢": ["中意", "倾心", "爱慕"],
    "饿了": ["饥了", "肚饿"],
    "累了": ["乏了", "疲倦", "困乏"],
    "想哭": ["要哭", "落泪"],
    "高兴": ["开心", "欢喜", "愉悦"],
    "好了": ["完了", "齐了"],
}

_SYNONYM_LOOKUP: dict[str, list[str]] = {}
for _canon, _syns in _STRUCTURAL_SYNONYMS.items():
    for _s in _syns:
        _SYNONYM_LOOKUP.setdefault(_s, []).append(_canon)
    _SYNONYM_LOOKUP.setdefault(_canon, []).append(_canon)

# === v1.6.5 / v1.2.5: Action semantic clusters (cross-verb paraphrase) ===
# Group verbs by underlying action. 36 clusters covering common RP actions.
_ACTION_CLUSTERS = {
    'walk':    ['散步', '走走', '溜达', '逛逛', '出去走走', '出门走走'],
    'go':      ['去玩', '前往', '过去', '动身'],
    'come':    ['过来', '来呀', '过来吧'],
    'leave':   ['离开', '走了', '离去', '出门了'],
    'return':  ['回来', '回去', '归来'],
    'run':     ['奔跑', '跑一跑', '去跑'],
    'eat':     ['吃饭', '用膳', '吃东西', '用餐', '吃好', '吃饱', '吃完', '吃点'],
    'drink':   ['喝水', '饮', '喝茶', '饮酒'],
    'cook':    ['做饭', '烹饪', '做菜', '下厨'],
    'sleep':   ['睡觉', '歇着', '安歇', '入眠', '去睡', '歇会儿', '休息', '入睡', '想睡', '要睡', '困了'],
    'wake':    ['醒来', '起床', '醒了'],
    'sit':     ['坐下', '坐会儿', '坐坐'],
    'stand':   ['站立', '起身', '站起来'],
    'cry':     ['哭', '流泪', '掉眼泪', '啜泣', '落泪'],
    'laugh':   ['笑了', '微笑', '笑一笑', '乐了'],
    'sigh':    ['叹气', '叹息', '叹了口气'],
    'say':     ['讲', '提及', '说起', '开口', '告诉你', '对你说'],
    'ask':     ['问起', '询问', '问一下', '提问'],
    'tell':    ['告诉', '说与', '告知', '知会'],
    'shout':   ['喊', '呼喊', '大叫'],
    'think':   ['思考', '思量', '思索', '寻思', '琢磨'],
    'miss':    ['想念', '挂念', '惦记', '思念'],
    'love':    ['喜欢', '爱', '中意', '爱慕'],
    'hate':    ['讨厌', '恨', '厌烦', '厌恶', '嫌弃'],
    'have':    ['有', '拥有', '持有'],
    'give':    ['送给', '赠与', '递给'],
    'take':    ['拿走', '取了', '收下', '接过'],
    'choose':  ['选择', '决定', '挑', '选好'],
    'tired':   ['累', '疲惫', '疲倦', '乏', '困', '疲急'],
    'hungry':  ['饿', '饥饿', '肚子饿'],
    'thirsty': ['渴', '口渴'],
    'cold':    ['冷', '着凉', '受寒'],
    'hot':     ['热', '发烧', '发热'],
    'happy':   ['高兴', '开心', '快乐', '欢喜'],
    'sad':     ['难过', '悲伤', '伤心', '难受'],
    'wait':    ['等', '等候', '等等'],
}


def _match_action_cluster(core: str) -> str | None:
    """Return cluster name if core contains any word in a cluster, else None.

    Longest words matched first to avoid "吃饭" matching "吃" alone.
    """
    if not core or len(core) < 2:
        return None
    all_words = []
    for cluster_name, words in _ACTION_CLUSTERS.items():
        for w in words:
            all_words.append((w, cluster_name))
    all_words.sort(key=lambda x: -len(x[0]))  # longest first
    for word, cluster_name in all_words:
        if word in core:
            return cluster_name
    return None


def _is_cluster_mirror(user_text: str, assistant_text: str, enable: bool = True) -> bool:
    """Detect cross-verb paraphrase mirror via semantic clusters (v1.6.5/v1.2.5).

    Catches patterns where user and assistant say semantically equivalent things
    but using different vocabulary in the SAME cluster:
      user: "我想去散步" -> asst: "知宥，你要出去走走？"
      user: "我累了" -> asst: "知宥，你疲急了？"
      user: "我想睡了" -> asst: "知宥，你要歇着吗？"

    Requirements (all must hold to be detected):
      1. cluster detection enabled
      2. user core and asst core hit SAME cluster (semantic paraphrase)
      3. sentence mood flip: asst is a question, user is statement
    Returns True if cluster paraphrase detected.
    """
    if not enable or not user_text or not assistant_text:
        return False
    user_raw = user_text.strip()
    asst_raw = assistant_text.strip()
    # If user is a question, skip
    if (user_raw.endswith("？") or user_raw.endswith("?")
            or user_raw.endswith("吗") or user_raw.endswith("么")):
        return False
    # If asst is not a question, skip
    asst_first = asst_raw[:40]
    if not (("？" in asst_first) or ("?" in asst_first)
            or ("吗" in asst_first) or ("么" in asst_first)):
        return False
    # Use functions from main module (will be imported)
    user_norm = _normalize_text(user_text)
    user_core = _strip_pronoun(user_norm.rstrip("。.!！，,?？"))
    asst_norm = _normalize_text(assistant_text)
    asst_core = _strip_pronoun(asst_norm.rstrip("。.!！，,?？"))
    user_cluster = _match_action_cluster(user_core)
    asst_cluster = _match_action_cluster(asst_core)
    if not user_cluster or not asst_cluster:
        return False
    # They must be in SAME cluster (paraphrase)
    if user_cluster != asst_cluster:
        return False
    # Length floor: at least 2 chars on each side (1 char alone is too noisy)
    if len(user_core) < 2 or len(asst_core) < 2:
        return False
    return True



def _get_predicate_synonyms(predicate: str) -> list[str]:
    """Return canonical predicates that share meaning with given predicate."""
    if not predicate:
        return []
    result: set[str] = set()
    if predicate in _SYNONYM_LOOKUP:
        for c in _SYNONYM_LOOKUP[predicate]:
            result.add(c)
    for syn, canonicals in _SYNONYM_LOOKUP.items():
        if syn and syn != predicate and syn in predicate:
            for c in canonicals:
                result.add(c)
    return list(result)


def _is_synonym_mirror(user_predicate: str, assistant_text_norm: str) -> bool:
    """Detect synonym-based structural mirror.

    user "我吃好了" -> asst "知宥，你吃饱了？" (predicates differ but mean the same)

    Looser than _is_structural_mirror; pure synonym hits should require 2x threshold
    to actually inject (over-flag safety valve).
    """
    if len(user_predicate) < 2 or len(assistant_text_norm) < 2:
        return False
    synonyms = _get_predicate_synonyms(user_predicate)
    if not synonyms:
        return False
    asst_open = assistant_text_norm[:30]
    for canon in synonyms:
        # Check all surface forms of this canonical predicate
        all_forms = [canon] + _STRUCTURAL_SYNONYMS.get(canon, [])
        for form in all_forms:
            if form and form != user_predicate and form in asst_open:
                return True
    return False


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: strip whitespace, punctuation"""
    return re.sub(r"""[\s，。！？、；：""''《》（）()\[\]【】\-,.!?;:\"'…~\n\r]+""", "", text)


def _extract_opener(text: str, max_len: int = 25) -> str:
    """Extract the opening pattern of a response.

    Takes first meaningful segment (up to max_len chars), stripping
    leading punctuation/ellipsis/whitespace.
    """
    # Strip leading whitespace and common RP punctuation
    cleaned = re.sub(r"^[\s…·—\-「」『』""'']+", "", text)
    if not cleaned:
        return ""
    # Take first max_len chars or until first sentence break
    segment = cleaned[:max_len]
    # Truncate at first major sentence break if found
    for sep in ["。", "！", "？", "…", "\n", ". ", "! ", "? "]:
        idx = segment.find(sep)
        if 3 < idx < max_len:
            segment = segment[:idx]
            break
    return segment.strip()


def _extract_ngrams(text: str, n: int = 4) -> list[str]:
    """Extract character-level n-grams from normalized text"""
    normalized = _normalize_text(text)
    if len(normalized) < n:
        return []
    return [normalized[i:i+n] for i in range(len(normalized) - n + 1)]


def _compute_ngram_similarity(text_a: str, text_b: str, n: int = 4) -> float:
    """Compute n-gram overlap similarity between two texts.

    Returns 0.0-1.0 where 1.0 means identical n-gram sets.
    """
    ngrams_a = set(_extract_ngrams(text_a, n))
    ngrams_b = set(_extract_ngrams(text_b, n))
    if not ngrams_a or not ngrams_b:
        return 0.0
    intersection = ngrams_a & ngrams_b
    # Jaccard-like but weighted toward shorter text (containment)
    shorter_len = min(len(ngrams_a), len(ngrams_b))
    return len(intersection) / shorter_len if shorter_len > 0 else 0.0


def _is_substring_mirror(user_text: str, assistant_text: str, min_len: int = 8) -> bool:
    """Check if user's text appears as substring in assistant's response.

    Only checks meaningful-length substrings to avoid false positives
    on common short words.
    """
    user_norm = _normalize_text(user_text)
    asst_norm = _normalize_text(assistant_text)
    if len(user_norm) < min_len:
        return False
    # Check if substantial portion of user text appears in assistant text
    # Use sliding window: if any 8+ char window of user appears in assistant
    window_size = max(min_len, len(user_norm) // 2)
    if window_size > len(user_norm):
        window_size = len(user_norm)
    for i in range(len(user_norm) - window_size + 1):
        if user_norm[i:i+window_size] in asst_norm:
            return True
    return False


def _is_opener_mirror(user_text: str, assistant_opener: str, threshold: float = 0.5) -> bool:
    """Check if assistant's opener is mirroring user's text.

    Catches patterns like:
    - user: "你为什么不告诉我？" / assistant: "我为什么不告诉你..."
    - user: "我想吃糖画" / assistant: "你想吃糖画？"
    """
    if not user_text or not assistant_opener:
        return False
    # Normalize both
    user_norm = _normalize_text(user_text)
    asst_norm = _normalize_text(assistant_opener)
    if len(user_norm) < 6 or len(asst_norm) < 6:
        return False

    # Strategy 1: Direct 4-gram overlap on opener
    sim = _compute_ngram_similarity(user_text[:50], assistant_opener, n=4)
    if sim > threshold:
        return True

    # Strategy 2: Pronoun-swap detection
    # user: "我想..." → assistant: "你想..." (common mirror with pronoun swap)
    user_swap = user_norm.replace("我", "你").replace("你", "我")
    if len(user_swap) >= 6:
        swap_sim = _compute_ngram_similarity(user_swap[:40], asst_norm[:40], n=4)
        if swap_sim > 0.6:
            return True

    return False


def _strip_pronoun(s: str) -> str:
    """Remove leading pronouns (and optional preceding name) to expose predicate.

    Patterns handled (v1.6.5/v1.2.5 - upgraded):
      "我吃好了"          -> "吃好了"
      "你想睡了"          -> "想睡了"
      "知宥你吃好了"       -> "吃好了"  (RP name + pronoun, name is 1-3 chars)
      "小姐你来了"        -> "来了"
    """
    PRONOUNS = ("咱们", "我们", "你", "他", "她", "它", "我")
    s = s.lstrip()
    if not s:
        return s
    # Pass 1: direct pronoun strip
    for pron in PRONOUNS:
        if s.startswith(pron):
            return s[len(pron):]
    # Pass 2: [name 1-3 chars] + [pronoun] pattern
    for nlen in (3, 2, 1):
        if len(s) > nlen:
            tail = s[nlen:]
            for pron in PRONOUNS:
                if tail.startswith(pron):
                    return tail[len(pron):]
    return s


def _is_structural_mirror(user_text: str, assistant_text: str) -> bool:
    """Detect statement-to-question structural mirroring.

    Catches patterns where user makes a declarative statement and assistant
    flips it to a question with pronoun swap (and may add a name prefix):
      user: "我吃好了" -> asst: "知宥，你吃好了？"
      user: "我去过了" -> asst: "你去过了？"
      user: "我想睡了" -> asst: "你想睡了？嗯，那便歇着。"

    Different from _is_opener_mirror: here the assistant ALSO flips
    sentence mood (declarative -> interrogative).

    Returns True if mirror detected.
    """
    if not user_text or not assistant_text:
        return False

    user_raw = user_text.strip()
    asst_raw = assistant_text.strip()
    if len(user_raw) < 3 or len(asst_raw) < 3:
        return False

    # Skip if user is already a question (only care about 陈述 -> 疑问 flip)
    if (user_raw.endswith("？") or user_raw.endswith("?")
            or user_raw.endswith("吗") or user_raw.endswith("么")):
        return False

    # Asst opening must be a question (within first 40 chars)
    asst_first = asst_raw[:40]
    is_q = (("？" in asst_first) or ("?" in asst_first)
            or ("吗" in asst_first) or ("么" in asst_first))
    if not is_q:
        return False

    # Extract user predicate (no pronoun, no trailing sentence punct)
    user_norm = _normalize_text(user_text)
    user_core = _strip_pronoun(user_norm.rstrip("。.!！，,？?"))
    if len(user_core) < 2:
        return False

    asst_norm = _normalize_text(assistant_text)
    if len(asst_norm) < 3:
        return False

    # Up to 6-char fingerprint appears in asst opening
    fp_len = min(6, len(user_core))
    fp = user_core[:fp_len]
    if fp in asst_norm[:30]:
        return True

    # Fallback: 3-gram overlap on opening
    if len(user_core) >= 3 and len(asst_norm) >= 3:
        sim = _compute_ngram_similarity(user_core, asst_norm[:30], n=3)
        if sim > 0.5:
            return True

    return False


def _assess_verbal_tier(turn_count: int, short_threshold: int = 10, long_threshold: int = 50) -> int:
    """Tier-based injection strength (v6.0 #4 self-adaptive thresholds).

    Returns:
        0 = quiet (log only, do not inject) - too early to nag in short conversation
        1 = standard inject (current behavior)
        2 = detailed inject with concrete alternative suggestions
    """
    if turn_count < short_threshold:
        return 0
    if turn_count < long_threshold:
        return 1
    return 2


# v6.0 safety valve #1: verbatim blacklist
# Patterns that look like mirrors but should NEVER be flagged.
_VERBATIM_SAFE_PREFIXES = [
    "你觉得", "你的意思是", "你是说", "你以为",
    "好的", "好啊", "嗯", "我知道了", "我明白",
    "我想", "我要", "我可以",
]


def _is_verbatim_safe(text: str) -> bool:
    """Returns True if text starts with a known-safe RP response pattern."""
    norm = _normalize_text(text)
    if not norm:
        return False
    for safe in _VERBATIM_SAFE_PREFIXES:
        if norm.startswith(safe):
            return True
    return False


def _bucket_opener_by_length(opener_norm: str) -> str:
    """Bucket opener with length tier for more precise grouping (v6.0 #2).

    "知宥，路上小心" (S/M tier) won't share a bucket with
    "知宥，路上小心，照顾好自己" (L tier), so they're counted separately.
    """
    if len(opener_norm) < 3:
        return ""
    sig = opener_norm[:6]
    if len(opener_norm) <= 6:
        tier = "S"
    elif len(opener_norm) <= 12:
        tier = "M"
    else:
        tier = "L"
    return f"{sig}|{tier}"


def _phrase_overlap_len(a: str, b: str) -> int:
    """Return the length of the maximal overlap between two strings.

    Considers three relationships:
      1. containment (a in b or b in a) -> min(len(a), len(b))
      2. suffix(a) == prefix(b)  (a 接 b)
      3. suffix(b) == prefix(a)  (b 接 a)
    Returns the largest overlap length found, else 0.
    """
    if not a or not b:
        return 0
    # containment
    if a in b:
        return len(a)
    if b in a:
        return len(b)
    max_chk = min(len(a), len(b))
    best = 0
    # suffix(a) == prefix(b)
    for i in range(max_chk, 0, -1):
        if a[-i:] == b[:i]:
            best = max(best, i)
            break
    # suffix(b) == prefix(a)
    for i in range(max_chk, 0, -1):
        if b[-i:] == a[:i]:
            best = max(best, i)
            break
    return best


def _stitch_phrases(a: str, b: str) -> str | None:
    """If a and b are overlapping windows of one longer phrase, stitch them.

    Returns the merged longer string, or None if they can't be stitched.
    Examples:
        ("桂花树下的石", "花树下的石凳")  -> "桂花树下的石凳"
        ("花树下的石凳", "树下的石凳上")  -> "花树下的石凳上"
        ("abc", "xyz")                  -> None
    """
    if not a or not b:
        return None
    if a in b:
        return b
    if b in a:
        return a
    max_chk = min(len(a), len(b))
    # a 的后缀 == b 的前缀  ->  a + b去掉重叠部分
    for i in range(max_chk, 1, -1):  # require >=2 char overlap to stitch
        if a[-i:] == b[:i]:
            return a + b[i:]
    # b 的后缀 == a 的前缀  ->  b + a去掉重叠部分
    for i in range(max_chk, 1, -1):
        if b[-i:] == a[:i]:
            return b + a[i:]
    return None


def _merge_overlapping_phrases(
    phrases: list[tuple[str, int]],
    min_overlap: int = 4,
) -> list[tuple[str, int]]:
    """Merge sliding-window phrase fragments back into their longest form.

    The n-gram extractor produces many overlapping windows of the same
    underlying sentence (e.g. "桂花树下的石凳上" -> "桂花树下的石" / "花树下的石凳"
    / "树下的石凳上"), each separately counted. This inflates the phrase count.

    This function groups fragments that overlap by >= min_overlap chars (or
    where one contains the other), stitches them into the longest representative
    string, and reports a single (phrase, count) per group.

    Count strategy: use the MAX count among the cluster members. Since all
    fragments of one repeated sentence appear together the same number of times,
    the max equals the true repetition count of the full sentence.

    Args:
        phrases: list of (phrase, count), pre-sorted by count desc preferred
        min_overlap: minimum char overlap to consider two fragments related

    Returns:
        Deduplicated list of (representative_phrase, count), count desc.
    """
    if not phrases:
        return []

    # Each cluster: {"members": [(phrase, count)...], "rep": stitched_string}
    clusters: list[dict[str, Any]] = []

    for phrase, count in phrases:
        placed = False
        for cluster in clusters:
            rep = cluster["rep"]
            # Related if overlap is large enough OR containment in either direction
            overlap = _phrase_overlap_len(phrase, rep)
            contained = (phrase in rep) or (rep in phrase)
            if contained or overlap >= min_overlap:
                cluster["members"].append((phrase, count))
                # Try to extend the representative to the longer stitched form
                stitched = _stitch_phrases(rep, phrase)
                if stitched and len(stitched) > len(rep):
                    cluster["rep"] = stitched
                elif len(phrase) > len(rep):
                    cluster["rep"] = phrase
                placed = True
                break
        if not placed:
            clusters.append({"members": [(phrase, count)], "rep": phrase})

    # Second pass: clusters may themselves overlap (chain stitching across
    # fragments that weren't adjacent in input order). Merge clusters whose
    # representatives now overlap.
    merged_again = True
    while merged_again:
        merged_again = False
        for i in range(len(clusters)):
            if clusters[i] is None:
                continue
            for j in range(i + 1, len(clusters)):
                if clusters[j] is None:
                    continue
                rep_i = clusters[i]["rep"]
                rep_j = clusters[j]["rep"]
                contained = (rep_i in rep_j) or (rep_j in rep_i)
                overlap = _phrase_overlap_len(rep_i, rep_j)
                if contained or overlap >= min_overlap:
                    clusters[i]["members"].extend(clusters[j]["members"])
                    stitched = _stitch_phrases(rep_i, rep_j)
                    if stitched and len(stitched) >= max(len(rep_i), len(rep_j)):
                        clusters[i]["rep"] = stitched
                    elif len(rep_j) > len(rep_i):
                        clusters[i]["rep"] = rep_j
                    clusters[j] = None
                    merged_again = True
        clusters = [c for c in clusters if c is not None]

    # Build result: representative + max count of its members
    result: list[tuple[str, int]] = []
    for cluster in clusters:
        rep = cluster["rep"]
        max_count = max(c for _, c in cluster["members"])
        result.append((rep, max_count))

    # Sort by count desc, then length desc
    result.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
    return result


def analyze_repetition(
    contexts: list[dict[str, Any]],
    k_recent: int = 20,
    opener_threshold: int = 2,
    phrase_threshold: int = 3,
    mirror_k_recent: int = 5,
    mirror_count_threshold: int = 2,
    mirror_similarity: float = 0.5,
    mirror_cooldown_turns: int = 3,
    last_mirror_inject_turn: int = -1,
    mirror_cluster_enable: bool = True,
) -> RepetitionReport:
    """Full repetition analysis on conversation history.

    Args:
        contexts: list of message dicts with "role" and "content" keys
        k_recent: how many recent turns to scan for repetition
        opener_threshold: min occurrences to flag an opener as repeated
        phrase_threshold: min occurrences to flag a phrase as repeated
        mirror_k_recent: how many recent pairs to scan for mirroring
        mirror_count_threshold: min mirror occurrences to trigger alert
        mirror_similarity: n-gram similarity threshold for mirror detection
        mirror_cooldown_turns: turns to skip re-injection after a prior alert (v1.6/v1.2)
        last_mirror_inject_turn: total turn index of last mirror injection, or -1 (v1.6/v1.2)
        mirror_cluster_enable: enable cross-verb semantic cluster detection (v1.6.5/v1.2.5)

    Returns:
        RepetitionReport with all findings
    """
    report = RepetitionReport()

    if not contexts:
        return report

    # --- Part 1: Repetition Guide (openers + phrases) ---
    # Extract recent assistant messages
    assistant_msgs: list[str] = []
    for msg in contexts:
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if role == "assistant" and isinstance(content, str) and content.strip():
            assistant_msgs.append(content)

    # Total assistant turns (untruncated - used for cooldown timing)
    report.absolute_assistant_turns = len(assistant_msgs)
    # Only look at recent K
    recent_assistant = assistant_msgs[-k_recent:]
    report.total_assistant_turns = len(recent_assistant)

    if len(recent_assistant) < 3:
        return report  # Not enough data

    # 1a. Opener pattern detection (v6.0 #2: bucket by signature + length tier)
    openers: list[str] = []
    for text in recent_assistant:
        opener = _extract_opener(text)
        if opener and len(opener) >= 3:
            openers.append(opener)

    opener_prefixes: list[str] = []
    for o in openers:
        bucket = _bucket_opener_by_length(_normalize_text(o))
        if bucket:
            opener_prefixes.append(bucket)

    prefix_counter = Counter(opener_prefixes)
    # Map back to readable openers
    prefix_to_readable: dict[str, str] = {}
    for o in openers:
        prefix = _bucket_opener_by_length(_normalize_text(o))
        if prefix and prefix not in prefix_to_readable:
            prefix_to_readable[prefix] = o

    for prefix, count in prefix_counter.most_common(5):
        if count >= opener_threshold:
            readable = prefix_to_readable.get(prefix, prefix)
            report.repeated_openers.append((readable, count))

    # 1b. Phrase-level repetition (multi-window n-gram frequency)
    all_phrases: Counter = Counter()
    for text in recent_assistant:
        # Extract meaningful phrases (6-15 char windows)
        normalized = _normalize_text(text)
        for window_size in [6, 8, 10, 12]:
            for i in range(len(normalized) - window_size + 1):
                phrase = normalized[i:i+window_size]
                all_phrases[phrase] += 1

    # Collect raw candidates above threshold (with internal-diversity filter)
    raw_candidates: list[tuple[str, int]] = []
    for phrase, count in all_phrases.most_common(200):
        if count < phrase_threshold:
            break
        # Skip if too repetitive internally (e.g. "哈哈哈哈")
        unique_chars = len(set(phrase))
        if unique_chars < len(phrase) * 0.4:
            continue
        raw_candidates.append((phrase, count))

    # v1.6.6: merge overlapping sliding-window fragments back into their
    # longest representative form, so "桂花树下的石" / "花树下的石凳" /
    # "树下的石凳上" (all x6) collapse into one "桂花树下的石凳上" x6
    # instead of being reported as three separate phrases.
    merged = _merge_overlapping_phrases(raw_candidates, min_overlap=4)

    # Take top 8 merged phrases
    repeated: list[tuple[str, int]] = merged[:8]

    report.repeated_phrases = repeated

    # Determine if injection needed
    if report.repeated_openers or report.repeated_phrases:
        report.should_inject_repetition = True

    # --- Part 2: Mirror Alert ---
    # Extract recent user-assistant pairs
    pairs: list[tuple[str, str]] = []
    msgs_flat = list(contexts)
    for i in range(len(msgs_flat) - 1):
        msg_curr = msgs_flat[i]
        msg_next = msgs_flat[i + 1]
        role_curr = msg_curr.get("role", "") if isinstance(msg_curr, dict) else getattr(msg_curr, "role", "")
        role_next = msg_next.get("role", "") if isinstance(msg_next, dict) else getattr(msg_next, "role", "")
        if role_curr == "user" and role_next == "assistant":
            content_curr = msg_curr.get("content", "") if isinstance(msg_curr, dict) else getattr(msg_curr, "content", "")
            content_next = msg_next.get("content", "") if isinstance(msg_next, dict) else getattr(msg_next, "content", "")
            if isinstance(content_curr, str) and isinstance(content_next, str):
                pairs.append((content_curr.strip(), content_next.strip()))

    # Only look at recent K pairs
    recent_pairs = pairs[-mirror_k_recent:]

    mirror_count = 0
    mirror_examples: list[tuple[str, str]] = []
    structural_mirror_count = 0
    synonym_mirror_count = 0
    skipped_safe = 0

    for user_text, assistant_text in recent_pairs:
        if not user_text or not assistant_text:
            continue

        assistant_opener = assistant_text[:50]

        # Safety valve #1: verbatim blacklist - never flag known-safe patterns
        if _is_verbatim_safe(assistant_opener):
            skipped_safe += 1
            continue

        is_mirror = False
        is_structural = False
        is_synonym = False

        # Check 1: substring mirror
        if _is_substring_mirror(user_text, assistant_text[:100], min_len=8):
            is_mirror = True

        # Check 2: opener mirror (with pronoun swap)
        if not is_mirror and _is_opener_mirror(user_text, assistant_opener, threshold=mirror_similarity):
            is_mirror = True

        # Check 3: structural mirror (statement -> question flip)
        if not is_mirror and _is_structural_mirror(user_text, assistant_text):
            is_mirror = True
            is_structural = True

        # Check 4: synonym mirror (looser, requires 2x threshold to inject)
        if not is_mirror:
            user_norm = _normalize_text(user_text)
            user_core = _strip_pronoun(user_norm.rstrip("\u3002.!\uff01\uff0c,?\uff1f"))
            asst_norm = _normalize_text(assistant_text)
            asst_first = assistant_text[:40]
            asst_is_q = ("\uff1f" in asst_first or "?" in asst_first
                         or "\u5417" in asst_first or "\u4e48" in asst_first)
            if asst_is_q and len(user_core) >= 2 and _is_synonym_mirror(user_core, asst_norm):
                is_mirror = True
                is_synonym = True

        # Check 5: cluster mirror (v1.6.5/v1.2.5 - cross-verb paraphrase)
        if mirror_cluster_enable and _is_cluster_mirror(user_text, assistant_text, enable=True):
            report.cluster_mirror_count += 1
            if not is_mirror:
                is_mirror = True

        if is_mirror:
            mirror_count += 1
            if is_structural:
                structural_mirror_count += 1
            if is_synonym:
                synonym_mirror_count += 1
            mirror_examples.append((user_text[:30], assistant_opener[:30]))

    report.mirror_count = mirror_count
    report.mirror_examples = mirror_examples
    report.structural_mirror_count = structural_mirror_count
    report.synonym_mirror_count = synonym_mirror_count
    report.skipped_safe_responses = skipped_safe

    # v6.0 #4: tier-based injection strength based on conversation length
    report.verbal_tier = _assess_verbal_tier(len(pairs))

    # Safety valve #2: pure synonym mirrors need DOUBLE the threshold
    # (over-flag protection: synonyms can semantically diverge)
    effective_threshold = mirror_count_threshold
    if synonym_mirror_count > 0 and synonym_mirror_count == mirror_count:
        # All hits are synonym-only - require 2x threshold
        effective_threshold = max(mirror_count_threshold * 2, mirror_count_threshold + 1)

    # Safety valve #3: tier 0 (short conversation) never injects, only logs
    if mirror_count >= effective_threshold and report.verbal_tier > 0:
        report.should_inject_mirror = True

    # Safety valve #4: cooldown (v1.6/v1.2 - same session, skip re-injection for N turns)
    # Still detect and count (for diagnostics), but suppress injection within window.
    # Use ABSOLUTE turn count, not k_recent-truncated, so cooldown works correctly
    # in long conversations (otherwise total_assistant_turns caps at k_recent and
    # cooldown never lifts).
    if last_mirror_inject_turn >= 0:
        turns_since = report.absolute_assistant_turns - last_mirror_inject_turn
        if 0 <= turns_since < mirror_cooldown_turns:
            report.cooldown_active = True
            report.should_inject_mirror = False

    return report


def format_repetition_guide(report: RepetitionReport) -> str:
    """Format RepetitionGuide injection text.

    Uses soft language - suggests, doesn't forbid.
    """
    lines = [
        "<RepetitionGuide>",
        "【表达多样性建议】以下是你最近对话的表达习惯统计。",
        "这不是禁止清单——经典表达偶尔使用是好的，但过于频繁会让对话失去新鲜感。",
        "",
    ]

    if report.repeated_openers:
        lines.append("近期常用开头（建议尝试不同方式起笔）：")
        for opener, count in report.repeated_openers[:5]:
            lines.append(f"  · \"{opener}...\"（最近出现 {count} 次）")
        lines.append("")

    if report.repeated_phrases:
        lines.append("近期高频表达（建议偶尔换种说法）：")
        for phrase, count in report.repeated_phrases[:6]:
            lines.append(f"  · \"{phrase}\"（{count} 次）")
        lines.append("")

    lines.extend([
        "建议：",
        "  - 用动作、神态、环境描写代替重复的语言表达",
        "  - 用沉默、停顿、欲言又止代替重复的台词",
        "  - 同一个意思可以通过比喻、反问、省略等方式重新表达",
        "  - 用户给你写过的素材（诗、信、回忆）你仍可以自由引用",
        "</RepetitionGuide>",
    ])

    return "\n".join(lines)


def format_mirror_alert(report: RepetitionReport) -> str:
    """Format MirrorAlert injection text."""
    lines = [
        "<MirrorAlert>",
        "【镜像复读提醒】检测到你最近有复述用户原话的倾向。",
        "",
    ]

    if report.mirror_examples:
        if report.verbal_tier >= 2:
            lines.append("近期镜像复读示例（长对话，建议用动作/神态/环境替代语言镜像）：")
        else:
            lines.append("近期镜像复读示例：")
        for user_snip, asst_snip in report.mirror_examples[:3]:
            lines.append(f"  用户: \"{user_snip}...\"")
            lines.append(f"  你的回复开头: \"{asst_snip}...\"")
            lines.append("")

    lines.extend([
        "请注意：",
        "  - 当用户提问或陈述时，用你的感受、判断、动作来回应",
        "  - 不要把用户的原话改写成疑问句作为回复的开头",
        "  - 可以回应用户话中的情感，但用自己的语言",
        "",
        "好的回应方式示例：",
        "  ✗ 用户: \"你为什么不告诉我？\" → 你: \"我为什么不告诉你...\"",
        "  ✓ 用户: \"你为什么不告诉我？\" → 你: \"怕你担心。\"",
        "  ✗ 用户: \"我想吃糖画\" → 你: \"你想吃糖画？\"",
        "  ✓ 用户: \"我想吃糖画\" → 你: \"前面巷口便有，我带你去。\"",
        "  ✗ 用户: \"我吃好了\" → 你: \"知宥，你吃好了？\"",
        "  ✓ 用户: \"我吃好了\" → 你: \"那便好，我送你回去。\"",
        "  ✗ 用户: \"我去过了\" → 你: \"你去过了？\"",
        "  ✓ 用户: \"我去过了\" → 你: \"那处确是好景。\"",
        "  ✗ 用户: \"我想睡了\" → 你: \"你想睡了？\"",
        "  ✓ 用户: \"我想睡了\" → 你: \"夜深了，我陪你早些歇息。\"",
    ])

    # v6.0: tier 2 adds concrete alternative suggestions
    if report.verbal_tier >= 2:
        lines.extend([
            "",
            "具体替代方向（任选其一即可，不必每种都用）：",
            "  · 用动作回应：站起身/递茶/收拾碗筷/拉住衣袖/转身去做事",
            "  · 用神态回应：微笑/侧头/垂眸/轻轻点头/眉头微皱",
            "  · 用环境回应：窗外的光/桌上的书/远处的人声/院里的花",
            "  · 用回忆联想：想起类似场景/某句话/某个人/某段往事",
        ])

    lines.append("</MirrorAlert>")

    return "\n".join(lines)
