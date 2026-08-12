"""
Memory compression prompt builder.

v1.9.0 shifts compression from plain event diaries to layered role memory:
plot continuity plus long-term personality shaping.
"""
from __future__ import annotations


def _time_label(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 8:
        return "清晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "正午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def _local_datetime(timestamp: float, timezone_name: str):
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    try:
        return _dt.fromtimestamp(float(timestamp), ZoneInfo(timezone_name))
    except Exception:
        return _dt.fromtimestamp(float(timestamp))


def now_context(timezone_name: str = "Asia/Shanghai") -> str:
    """Return a stable machine-time hint for the compression model."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    try:
        now = _dt.now(ZoneInfo(timezone_name))
    except Exception:
        now = _dt.now()
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    tl = _time_label(now.hour)
    return (
        f"【压缩执行时的当前现实时间】{now.strftime('%Y-%m-%d %H:%M')} {wd} · {tl}。"
        "这是整理对话的时间，不自动等于每条记忆的发生时间。"
    )


COMPRESS_PROMPT_TEMPLATE = """【角色】你是 {character_name} 的私人记忆整理者。{character_name} 是当前对话里的 assistant 一方。
你的任务不是只记“发生过什么”，而是把对话压缩成能长期塑造 {character_name} 内心、关系反应和剧情连续性的记忆。

请以 {character_name} 的第一人称口吻，{diary_count_instruction}。它可以像私密日记，但每条都必须同时判断：这是剧情事实、关系变化、情绪锚点、行为倾向、承诺规则，还是日常氛围。

{current_time}

{message_time_context}

【硬规则】
1. 只记录对话中有明确依据的内容，不许编造。没有值得记的内容，返回 []。
2. 每条只写一个连续场景或一个明确的长期变化，不要把跨时间、跨地点的内容硬拼在一起。
3. 分清主体：哪些是 {character_name} 说/做/感受到的，哪些是 user 说/做的。绝不能把 user 的话写成 {character_name} 的话。
4. 保留关键物件、动作、称呼、承诺、边界、身体反应、关系因果；这些细节会影响之后角色怎么自然反应。
5. 正文 content 是这条记忆的主体，写成完整、自然、有文学质感的第一人称私密日记。完整性优先，不设硬字数上限；必须保留理解这段经历所需的起因、关键对话/动作/称呼/物件、情绪变化、关系因果和结果。不要为了简短删掉必要内容，也不要用空泛抒情或重复句子虚增篇幅。
6. 每个“[对话记录时间: ...]”是那轮 user 与 assistant 实际交流的现实记录时间。整理任务执行时间只表示何时整理，绝不能覆盖这些逐轮时间。确实发生于本次连续对话的场景，event_date 和 time_label 必须对应支撑该场景的记录时间。
7. 对话正文明确设定了剧情日期/时段时，剧情时间优先于现实记录标记，并使用 explicit_dialogue；回忆、转述或提到的旧事不能因为今天谈到就写成今天。没有明确剧情时间、但可由逐轮记录时间确认本段刚发生时，使用 conversation_now；仍无法确认才使用 unknown。
8. 缓冲跨越多个公历日期时，必须覆盖每个有长期保留价值的日期，不能把前一天事件归到压缩执行日。不同日期的独立场景分开写；只有一个不可分割的连续场景确实跨过午夜时才可保留一篇，此时 event_date 写场景开始日，并在 content 与 time_label 中明确它跨到次日。
9. 更偏重“长期人格塑形”：除非只是纯剧情事实，每条都要写 long_effect 和 trigger_hint。
10. 日记数量是目标而不是机械拆分命令。一个连续场景和完整情感弧不能为了凑数量被切碎；多个不同日期、场景、时间阶段或独立关系变化则必须分开记录。宁可少一条，也不能制造重复、残缺或无依据的记忆，但不能漏掉跨日缓冲中有价值的日期。
11. 先完成 content，再从已经写好的完整日记中提取 scene_anchor、retrieval_key、state_change 和 entities。这些是机器检索字段，不能把 content 写成资料卡。

【memory_type 只能从这些值里选】
- plot_fact: 剧情连续性事实，之后说出来才有用，比如身份、地点、事件结果、物品状态。
- relationship_shift: 关系位置发生变化，比如更亲近、更信任、更警惕、更依赖。
- emotional_anchor: 强烈情绪锚点，比如害怕分离、被认真记住、被安抚后的松动。
- behavior_bias: 以后更可能表现出的反应方式，比如先嘴硬、靠近、回避、试探、主动确认。
- promise_or_rule: 承诺、禁忌、边界、约定、称呼规则。
- daily_texture: 日常氛围和生活质感，只在高度相关时轻轻影响语气。

【字段含义】
- scene_anchor: 这条记忆最有辨识度的一句话、动作或物件，10-25 字。
- content: 第一人称记忆正文，保留事实和情绪因果。
- long_effect: 这件事对 {character_name} 之后的内心、人格、关系反应造成的长期影响。写成一句自然中文，不要空泛。
- trigger_hint: 未来什么话题/场景会触发这条记忆，以及触发后应该“演出来”的反应。
- retrieval_key: 一句高密度检索句，写清人物、事件、地点/物件、情感结果；只用于机器检索，不替代日记正文。
- state_change: 这段经历前后，{character_name} 的关系位置、信任、恐惧、渴望或行为倾向发生了什么变化；没有变化时留空。
- entities: 对检索有意义的人物、称呼、地点、物件、约定和事件名称；不要放“开心、事情、聊天”这类泛词。
- importance: 1-5。关系改变、承诺、分离/重逢、身份变化、重大剧情给 4-5；日常给 2-3。
- tags: 用 # 开头，包含人物、物件、关系、情绪、地点等可检索标签。不要输出 #保底压缩、#夜间压缩、#自动压缩、#迁移、#livingmemory 这类流程来源标签。

【输出格式】严格输出 JSON 数组，不要 markdown，不要 ```json，不要解释。每个元素：
{{
  "event_date": "记忆发生的公历日期或空字符串，例如 2026-07-18",
  "time_label": "清晨/上午/正午/下午/傍晚/晚上/深夜等或空字符串",
  "time_basis": "explicit_dialogue/conversation_now/unknown",
  "scene_anchor": "最有辨识度的一句话、动作或物件",
  "content": "第一人称记忆正文，一整段，无换行",
  "memory_type": "relationship_shift",
  "long_effect": "这件事让我以后在类似场景里更容易......",
  "trigger_hint": "当再次谈到......时，我会......",
  "retrieval_key": "人物、事件、关键物件与情感结果组成的高密度检索句",
  "state_change": "从......变成......，或空字符串",
  "entities": ["人物或称呼", "地点或物件", "事件名称"],
  "tags": ["#标签1", "#标签2"],
  "importance": 4
}}

【主体归因反例】
错误：我说“我想先给你洗”。（如果这句话其实是 user 说的）
正确：他低声说想先给我洗，我手里的帕子停了一下。

【效果目标】
让角色不是“记得发生过什么”，而是“因为发生过这些事，所以现在更像她自己”。剧情事实可以被自然提起；人格影响优先体现在语气、迟疑、靠近、回避、试探、确认和选择里。

【本段对话】
{messages}

【输出】只输出 JSON 数组，不要任何其他文字。"""


def build_compress_prompt(
    character_name: str,
    messages_text: str,
    num_diaries: int = 2,
    enhancer_time: dict | None = None,
    exact_diary_count: bool = False,
    timezone_name: str = "Asia/Shanghai",
    message_time_context: str = "",
) -> str:
    """Build the full compression prompt."""
    ctx = enhancer_time or {}
    lines: list[str] = []
    if ctx.get("solar_date"):
        lines.append(f"【整理任务关联的最近请求日期】{ctx['solar_date']}（仅供校验，不覆盖逐轮记录时间）")
    elif ctx.get("solar_md"):
        lines.append(f"【整理任务关联的最近请求日期】{ctx['solar_md']}（仅供校验，不覆盖逐轮记录时间）")
    else:
        lines.append(now_context(timezone_name))
    if ctx.get("period"):
        lines.append(f"【整理任务关联的最近请求时段】{ctx['period']}（不能代替每轮时间标记）")
    if exact_diary_count:
        diary_count_instruction = (
            f"把这段对话里值得长期保留的内容整理成目标 {num_diaries} 条独立记忆。"
            "按真实场景、时间阶段和完整情感弧划分；目标数量不能凌驾于记忆完整性。"
            "信息足够且确有多个独立场景时应覆盖它们，信息不足时可以少于目标条数，禁止硬拆同一场景或重复改写。"
        )
    else:
        diary_count_instruction = f"把这段对话里值得长期保留的内容写成 {num_diaries} 条以内的独立记忆"
    return COMPRESS_PROMPT_TEMPLATE.format(
        character_name=character_name,
        num_diaries=num_diaries,
        diary_count_instruction=diary_count_instruction,
        current_time="\n".join(lines),
        message_time_context=message_time_context or "【逐轮时间】旧缓冲没有可用时间标记；只能依据对话明确日期，无法判断时留空。",
        messages=messages_text,
    )


EPISODE_EXTRACTION_PROMPT = """【角色】你是 {character_name} 的情景记忆建模器。{character_name} 是对话里的 assistant。
这一步不写日记、不追求文采，只把原始对话整理成可核验的长期情景记忆。所有事实必须能指回下面带 [turn:N] 的原始轮次。

{current_time}
{message_time_context}

【目标】{count_instruction}跨日期、地点、目标或关系阶段时分开；同一完整场景不可为了凑数量硬拆。

【本地场景边界候选（候选区间）】
{scene_candidates_json}
{scene_candidate_instruction}

【严格规则】
1. evidence 是记忆的事实基础。每条必须写 kind、actor、detail、turn_indexes、tier；quote 只能逐字摘自对应轮次，不能润色或拼接。
2. tier 只能是 must_write、supporting、archive_only。承诺、边界和关键事件必须标为 must_write；用于交代氛围或因果链的证据标为 supporting；重复、微弱或泛化内容标为 archive_only。
3. 分清 user 与 {character_name}。actor 只能写 user、assistant 或双方，不得把 user 的话或感受归给角色。
4. 客观事实、角色主观理解和长期推测必须分开。affect_before/after 是角色体验；不能冒充已发生事实。
5. 保留关键称呼、原话、动作、物件、位置、身体反应、承诺、边界、拒绝、因果和结果。
6. unresolved 只记录对话结束时仍悬而未决的冲突、问题、约定或期待；没有就为空数组。
7. 对话明确剧情时间时使用 explicit_dialogue；本轮实际发生且只能依据记录时间时使用 conversation_now；无法确定时使用 unknown。
8. scene_start_turn 与 scene_end_turn 必须给出情景在原始对话中的闭区间，并说明选择该边界的 reasons。
9. 没有长期保留价值时返回 []。禁止用空泛心理描写凑记忆。

【memory_type】只能是 plot_fact、relationship_shift、emotional_anchor、behavior_bias、promise_or_rule、daily_texture。

【输出】严格 JSON 数组，不要 markdown。每项：
{{
  "episode_key":"batch 内稳定编号，如 e1",
  "event_date":"YYYY-MM-DD 或空",
  "time_label":"清晨/上午/正午/下午/傍晚/晚上/深夜或空",
  "time_basis":"explicit_dialogue/conversation_now/unknown",
  "scene_anchor":"10-30字辨识锚点",
  "scene_start_turn":0,
  "scene_end_turn":1,
  "reasons":["选择该场景范围的事实理由"],
  "memory_type":"relationship_shift",
  "evidence":[
    {{"kind":"dialogue/action/fact/object/commitment/boundary/body/setting","actor":"user/assistant/双方","detail":"忠实事实","quote":"可为空的逐字原话","turn_indexes":[0,1],"tier":"must_write/supporting/archive_only","confidence":0.0}}
  ],
  "affect_before":"角色在事件前的心理位置，可为空",
  "affect_after":"角色在事件后的心理位置，可为空",
  "state_change":"关系、信任、恐惧、渴望或行为倾向的前后变化，可为空",
  "long_effect":"这段经历可能留下的长期影响；纯剧情事实可为空",
  "trigger_hint":"未来哪些具体话题或场景会唤起它，以及更可能怎样表现",
  "retrieval_key":"人物、事件、地点/物件、关系结果组成的高密度检索句",
  "entities":["人物、称呼、地点、物件、约定或事件名"],
  "unresolved":["尚未解决的具体事项"],
  "tags":["#可见主题标签"],
  "importance":1
}}

【原始对话】
{messages}

只输出 JSON 数组。"""


DIARY_RENDER_PROMPT = """【角色】你是 {character_name} 的私人日记写作者。下面的“情景模型”已经从原始对话核验，是事实边界；你的任务只是把每个情景写成完整、有文学质感、能保留长期心理连续性的第一人称日记。

【写作规则】
1. 每个 episode_key 输出且只输出一次，不合并不同情景，不新增情景。
2. content 必须是 {character_name} 第一人称的沉浸式私密日记，而不是聊天实录；禁止使用“User:”或“Assistant:”标签，也禁止逐轮复述对话。
3. evidence 中 tier=must_write 的事实必须 100% 写入；tier=supporting 的事实整体至少写入 50%，用于保住氛围和因果；tier=archive_only 的事实必须省略。
4. 完整保留人物主体、关键原话/动作/物件、情绪转折、关系因果、结果和未解决部分，并写清这段经历对我的 effect（影响）以及事后仍延续的 afterglow（余韵）。
5. 可以组织语言和描写体验，但不得增加情景模型没有支持的事实、动作、称呼、承诺或结果。
6. 不写“根据对话”“记忆模型”“证据显示”等分析口吻。它应当像 {character_name} 真正写给自己的私密日记，而不是摘要、资料卡或条目列表。
7. 文学性来自具体感受、动作之间的停顿和真实因果，不来自空泛抒情、重复感叹或擅自扩写。
8. 不设硬字数上限。较长场景要写清完整过程；较短日常不必注水。

【情景模型】
{episodes_json}

【原始对话，仅用于核对语气和原话】
{messages}

【输出】严格 JSON 数组，不要 markdown：
[
  {{"episode_key":"e1","content":"第一人称完整日记正文"}}
]
"""


def build_episode_extraction_prompt(
    character_name: str,
    messages_text: str,
    num_diaries: int,
    *,
    exact_count: bool = False,
    timezone_name: str = "Asia/Shanghai",
    message_time_context: str = "",
    scene_candidates: list[dict] | None = None,
    diary_cap: int = 0,
) -> str:
    import json

    requested_count = max(1, int(num_diaries or 1))
    range_cap = max(1, int(diary_cap)) if diary_cap else requested_count
    if diary_cap:
        count_instruction = (
            f"动态提取 1..{range_cap} 个连续情景，最多不超过 {range_cap} 个情景；"
            "数量由有效候选范围和长期价值决定。"
        )
    elif exact_count:
        count_instruction = (
            f"目标提取 {requested_count} 个连续情景。先按真实日期、场景、目标、"
            "情绪转折和关系阶段寻找彼此独立的记忆；信息确实不足时可以少于目标，"
            "但不能因为省事漏掉已有的独立场景，也不能复制或切碎同一情感弧。"
        )
    else:
        count_instruction = f"最多提取 {requested_count} 个连续情景。"
    candidates = [item for item in (scene_candidates or []) if isinstance(item, dict)]
    if candidates:
        candidate_instruction = (
            "本地场景边界候选是硬边界：每个情景的 scene_start_turn..scene_end_turn 必须完整位于同一个候选范围内，"
            "不得跨候选拼接；无有效证据的候选可以不用。"
        )
    else:
        candidate_instruction = (
            "本地候选未启用或未检测到有效边界；请直接依据原始轮次划定连续情景边界，"
            "但仍必须输出 scene_start_turn 与 scene_end_turn。"
        )
    return EPISODE_EXTRACTION_PROMPT.format(
        character_name=character_name,
        num_diaries=requested_count,
        count_instruction=count_instruction,
        scene_candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        scene_candidate_instruction=candidate_instruction,
        current_time=now_context(timezone_name),
        message_time_context=message_time_context or "【逐轮时间】以每个 [对话记录时间] 为准。",
        messages=messages_text,
    )


EPISODE_COMPACT_RECOVERY_PROMPT = """【紧凑恢复任务】上一轮情景抽取超时或结构无效。
你是 {character_name} 的记忆证据整理器。不要写日记，只从带 [turn:N] 的原始对话提取最多 {max_episodes} 个可核验情景。

规则：
1. 每个情景必须是连续 turn 区间，不能跨越给出的本地候选边界；没有长期价值的寒暄可以忽略。
2. 每项 evidence 必须指向真实 turn_indexes。quote 只能逐字摘录；actor 只能是 user、assistant 或双方。
3. 承诺、边界、关系决定和关键事实用 must_write；因果与氛围用 supporting；重复寒暄用 archive_only。
4. 不得虚构心理。state_change、long_effect、trigger_hint 无可靠依据时留空。
5. 时间以逐轮记录为准；明确剧情日期用 explicit_dialogue，记录时间用 conversation_now，无法判断用 unknown。

本地候选：
{scene_candidates_json}

输出严格 JSON 数组，不要 markdown。每项只使用这些字段：
{{"episode_key":"e1","event_date":"YYYY-MM-DD或空","time_label":"时段或空","time_basis":"explicit_dialogue/conversation_now/unknown","scene_anchor":"辨识锚点","scene_start_turn":0,"scene_end_turn":1,"reasons":["边界理由"],"memory_type":"plot_fact/relationship_shift/emotional_anchor/behavior_bias/promise_or_rule/daily_texture","evidence":[{{"kind":"dialogue/action/fact/object/commitment/boundary/body/setting","actor":"user/assistant/双方","detail":"忠实事实","quote":"可为空的逐字原话","turn_indexes":[0],"tier":"must_write/supporting/archive_only","confidence":0.0}}],"state_change":"可为空","long_effect":"可为空","trigger_hint":"可为空","retrieval_key":"高密度检索句","entities":[],"unresolved":[],"tags":[],"importance":3}}

{message_time_context}

原始对话：
{messages}

只输出 JSON 数组。"""


def build_episode_compact_recovery_prompt(
    character_name: str,
    messages_text: str,
    max_episodes: int,
    *,
    message_time_context: str = "",
    scene_candidates: list[dict] | None = None,
) -> str:
    import json

    return EPISODE_COMPACT_RECOVERY_PROMPT.format(
        character_name=character_name,
        max_episodes=max(1, int(max_episodes or 1)),
        scene_candidates_json=json.dumps(
            [item for item in (scene_candidates or []) if isinstance(item, dict)],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        message_time_context=message_time_context or "【逐轮时间】以每个 [对话记录时间] 为准。",
        messages=messages_text,
    )


def build_diary_render_prompt(
    character_name: str,
    messages_text: str,
    episodes: list[dict],
) -> str:
    import json

    return DIARY_RENDER_PROMPT.format(
        character_name=character_name,
        episodes_json=json.dumps(episodes, ensure_ascii=False, indent=2),
        messages=messages_text,
    )


DIARY_LITERARY_REWRITE_PROMPT = """【角色】你是 {character_name} 的私人日记文学润色者。
请在不改变事实边界的前提下，把旧日记改写成更沉浸、更自然的第一人称私密记忆。

【硬规则】
1. 以情景模型为唯一事实边界；不新增或删除事件、动作、称呼、承诺、边界、因果和结果。
2. tier=must_write 的证据必须 100% 保留；tier=supporting 的证据整体至少保留 50%；tier=archive_only 必须省略。
3. 禁止聊天实录体、逐轮问答和“User:”/“Assistant:”标签；不要提到模型、证据或改写过程。
4. 保留旧日记中有情景证据支持的独特语气与细节，删去重复、空泛和无依据内容。
5. 写清经历造成的 effect（影响）和事后持续的 afterglow（余韵），但不得凭空制造心理结论。

【旧日记】
{previous_content}

【情景模型】
{episodes_json}

【输出】只输出改写后的第一人称日记正文，不要 JSON、markdown、标题或解释。"""


def build_diary_literary_rewrite_prompt(
    character_name: str,
    previous_content: str,
    episodes: list[dict],
) -> str:
    import json

    return DIARY_LITERARY_REWRITE_PROMPT.format(
        character_name=character_name,
        previous_content=str(previous_content or ""),
        episodes_json=json.dumps(episodes, ensure_ascii=False, indent=2),
    )


SEMANTIC_STATE_UPDATE_PROMPT = """【角色】你负责维护 {character_name} 的“当前滚动状态文档”。
它不是日记、不是人物生平摘要，也不是把新事件继续追加到末尾；它只回答：经历这些事情之后，{character_name} 现在处在什么关系位置、遵守什么承诺边界、倾向怎样反应、近期情绪底色如何，还有什么尚未解决。

【更新原则】
1. 输出的是一份完整的新状态，直接覆盖旧状态。保留仍成立的重要内容，合并同义重复，删除已经明确失效或被后来事实取代的状态。
2. 新变化必须由“本批次情景证据”支持。事件经过留在日记和原文档案里，不要在状态文档复述完整剧情。
3. 区分稳定倾向与一时情绪。单次轻微反应不能轻易改写人格；明确承诺、边界、关系转折、反复出现的模式和高强度事件权重更高。
4. 出现矛盾时优先保留更晚、更明确、证据更强的状态；如果尚不能判定，在“仍未解决”中保留张力，禁止武断覆盖。
5. 每个字段写高密度自然中文，可用短行；不写版本号、日期流水账、分析过程或“根据证据”。总长度目标不超过 {target_chars} 字，但重要承诺、边界和未解决事项不能因压缩而丢失。
6. 只写 {character_name} 的内在状态。不要把 user 的感受、人格或意图写成 {character_name} 的状态。

【当前旧状态】
{current_state}

【本批次情景证据】
{episodes_json}

【输出】严格输出一个 JSON 对象，不要 markdown：
{{
  "relationship_position":"现在如何理解双方关系、信任与距离",
  "commitments_boundaries":"仍有效的承诺、称呼规则、禁忌和边界",
  "behavior_tendencies":"当前较稳定的靠近、回避、试探、确认、保护等反应倾向",
  "emotional_baseline":"近期延续到现在的情绪底色，不复述具体事件",
  "open_loops":"仍未解决的冲突、疑问、期待或需要兑现的事项"
}}
"""


def build_semantic_state_update_prompt(
    character_name: str,
    current_state: dict | None,
    episodes: list[dict],
    target_chars: int = 1800,
) -> str:
    import json

    state = current_state or {}
    current_text = str(state.get("rendered_text") or "").strip()
    if not current_text:
        current_text = "（尚未建立。请只依据本批次证据初始化，不要补写设定。）"
    evidence_view = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        evidence_view.append({
            "episode_id": episode.get("episode_id") or episode.get("episode_key") or "",
            "occurred_at": episode.get("occurred_at") or episode.get("event_date") or "",
            "memory_type": episode.get("memory_type") or "plot_fact",
            "importance": episode.get("importance") or 3,
            "scene_anchor": episode.get("scene_anchor") or "",
            "state_change": episode.get("state_change") or "",
            "long_effect": episode.get("long_effect") or "",
            "trigger_hint": episode.get("trigger_hint") or "",
            "affect_before": episode.get("affect_before") or "",
            "affect_after": episode.get("affect_after") or "",
            "unresolved": episode.get("unresolved") or [],
            "evidence": [
                {
                    "tier": item.get("tier") or "supporting",
                    "actor": item.get("actor") or "",
                    "detail": item.get("detail") or item.get("quote") or "",
                    "grounded": bool(item.get("grounded")),
                }
                for item in (episode.get("evidence") or [])
                if isinstance(item, dict) and item.get("tier") != "archive_only"
            ][:12],
        })
    return SEMANTIC_STATE_UPDATE_PROMPT.format(
        character_name=character_name,
        target_chars=max(600, int(target_chars or 1800)),
        current_state=current_text,
        episodes_json=json.dumps(evidence_view, ensure_ascii=False, indent=2),
    )


QUERY_PLAN_DISAMBIGUATE_PROMPT = """你是记忆检索查询消歧器。请结合上下文判断用户查询中的人物、代词、事件、时间、地点或物件指向，生成忠实且可检索的查询计划。

【规则】
1. 不得补造上下文未提供的事实；仍有歧义时明确列出，不要擅自选定。
2. 保留用户原意、情绪和限制条件，把上下文只用于解析指代和省略信息。
3. 输出严格 JSON，不要 markdown 或解释。

【用户查询】
{user_query}

【可用上下文】
{context_text}

【输出格式】
{{
  "resolved_query":"消歧后的完整查询；无法唯一确定时保留中性表述",
  "entities":["明确的人物、称呼、地点、物件或事件"],
  "time_constraints":["明确或可核验的时间限制"],
  "ambiguities":["仍无法消除的歧义"],
  "search_queries":["用于记忆检索的高密度查询"]
}}"""


def build_query_plan_disambiguation_prompt(user_query: str, context_text: str) -> str:
    return QUERY_PLAN_DISAMBIGUATE_PROMPT.format(
        user_query=str(user_query or ""),
        context_text=str(context_text or "") or "（无可用上下文）",
    )


def _message_timestamp(msg) -> float:
    if not isinstance(msg, dict):
        return 0.0
    try:
        return float(msg.get("event_ts") or msg.get("recorded_ts") or msg.get("created_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _message_timezone(msg, fallback: str) -> str:
    if isinstance(msg, dict):
        return str(msg.get("event_timezone") or msg.get("timezone") or fallback)
    return fallback


def recorded_message_dates(contexts: list, timezone_name: str = "Asia/Shanghai") -> list[str]:
    """Return ordered local dates represented by a buffered conversation."""
    dates: list[str] = []
    for msg in contexts or []:
        ts = _message_timestamp(msg)
        if ts <= 0:
            continue
        date_text = _local_datetime(ts, _message_timezone(msg, timezone_name)).strftime("%Y-%m-%d")
        if date_text not in dates:
            dates.append(date_text)
    return dates


def recorded_time_labels_by_date(
    contexts: list,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, list[str]]:
    """Return ordered time-of-day labels observed on each persisted local date."""
    labels: dict[str, list[str]] = {}
    for msg in contexts or []:
        ts = _message_timestamp(msg)
        if ts <= 0:
            continue
        local = _local_datetime(ts, _message_timezone(msg, timezone_name))
        date_text = local.strftime("%Y-%m-%d")
        label = _time_label(local.hour)
        values = labels.setdefault(date_text, [])
        if label not in values:
            values.append(label)
    return labels


def recorded_time_context(contexts: list, timezone_name: str = "Asia/Shanghai") -> str:
    """Summarize the persisted per-turn time range for the compression model."""
    points = []
    for msg in contexts or []:
        ts = _message_timestamp(msg)
        if ts <= 0:
            continue
        zone = _message_timezone(msg, timezone_name)
        local = _local_datetime(ts, zone)
        points.append((ts, local, zone))
    if not points:
        return ""
    points.sort(key=lambda item: item[0])
    dates = recorded_message_dates(contexts, timezone_name)
    start = points[0][1]
    end = points[-1][1]
    zone_note = points[0][2] if len({p[2] for p in points}) == 1 else "按各轮记录时区"
    return (
        f"【对话实际记录时间范围】{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')}（{zone_note}）\n"
        f"【对话覆盖的公历日期】{', '.join(dates)}。这些日期来自持久化消息记录，不是压缩执行时间。"
    )


def format_messages_for_prompt(
    contexts: list,
    max_turns: int = 30,
    timezone_name: str = "Asia/Shanghai",
    max_chars_per_turn: int = 0,
) -> str:
    """Format chat contexts with a persisted real-time marker for every turn."""
    if not contexts:
        return ""
    recent = contexts[-(max_turns * 2):]
    lines: list[str] = []
    last_marker = None
    char_cap = max(0, int(max_chars_per_turn or 0))
    for turn_index, msg in enumerate(recent):
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
        content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        ts = _message_timestamp(msg)
        turn_timestamp = "未知"
        if ts > 0:
            zone = _message_timezone(msg, timezone_name)
            local = _local_datetime(ts, zone)
            turn_timestamp = f"{local.strftime('%Y-%m-%d %H:%M')} · {zone}"
            marker_key = (local.strftime("%Y-%m-%d %H:%M"), zone)
            if role == "user" or marker_key != last_marker:
                wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local.weekday()]
                lines.append(
                    f"[对话记录时间: {local.strftime('%Y-%m-%d %H:%M')} {wd} · {_time_label(local.hour)} · {zone}]"
                )
                last_marker = marker_key
        speaker = "用户" if role == "user" else "我"
        clean = " ".join(content.split())
        omitted_chars = 0
        if char_cap > 0 and len(clean) > char_cap:
            omitted_chars = len(clean) - char_cap
            clean = clean[:char_cap]
        lines.append(f"[turn:{turn_index}] {speaker}: {clean}")
        if omitted_chars:
            lines.append(
                f"[本轮视图已截断：省略 {omitted_chars} 字；完整原文见原文档案；"
                f"turn:{turn_index}; timestamp:{turn_timestamp}; "
                f"role:{role or 'unknown'}; omitted_chars:{omitted_chars}]"
            )
    return "\n".join(lines)


format_messages = format_messages_for_prompt
