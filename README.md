# astrbot_plugin_memos_memory

面向 AstrBot 角色扮演场景的 Memos 长期记忆插件。

当前版本：`4.6.2`

这个插件的目标不是做一个“聊天记录搜索器”，而是把长期 RP 对话沉淀成可检索、可塑形、可诊断的角色记忆系统：原始对话负责取证，Episode 负责事件结构，第一人称日记保留文学连续性，滚动状态持续融合“角色现在是谁”，相关记忆在对话前被召回并临时注入，AstrBot 原始上下文则被治理在可控范围内。

## 快速开始

### 安装后先做什么

| 情况 | 操作 |
| --- | --- |
| 全新安装，Memos 也是空的 | 配置 Memos、Embedding 和压缩 Provider 后直接使用，不需要先执行命令。 |
| Memos 已有日记，但本地还没有索引 | 执行一次 `/memos-reindex`，完整读取 Memos，并同时建立 3.x 兼容索引与 4.0 情景卡。 |
| 从 `4.6.1` 升级到 `4.6.2` | 覆盖并重载即可。只更新 WebUI 模型选择和预设保护，不改数据库、Memos 或记忆算法，不需要同步与重建索引。 |
| 从 `4.6.0` 升级到 `4.6.2` | 覆盖并重载即可。包含 4.6.1 的预设与原文按需展开更新；不改数据库或 Memos，不需要同步与重建索引。 |
| 从 `4.5.4` 或任一 `4.6.0-test` 升级到 `4.6.2` | 覆盖并重载即可。不合并数据库，不改写既有 Memos、日记、原文、状态或画像，也不需要重建索引。时间洞察会自动重建本地派生候选以接入原文追溯质量；默认每 14 天备份一次插件本地数据。 |
| 从 `4.5.x` 升级到 `4.5.4` | 直接覆盖并重载。无数据库结构变更、无需重建索引；4.5.4 默认隔离指令及其回复，4.5.2 的 Episode 保全修复继续有效。 |
| 从 `4.4.0` 升级到 `4.5.4` | 直接覆盖并重载。无数据库结构变更，无需任何迁移命令；安全网、软注入目标与放宽的选择参数立即生效，已有 Memos、情景卡、滚动状态和原文档案完全不动。 |
| 从 `4.3.x` 升级到 `4.5.4` | 直接覆盖并重载。已有 Memos、情景卡、滚动状态和原文档案不改写；后台只为已有原始轮次建立向量索引，完成前继续使用事件卡和日记路线。 |
| 从 `4.0–4.2` 升级到 `4.5.4` | 直接覆盖并重载。插件幂等补齐情景卡、滚动状态、纯正文 passage 和原始轮次索引；已有日记、画像历史及心潮数据不会清空。 |
| 从 `2.x/3.x` 升级到 `4.5.4` | 覆盖后重载插件。启动后从已有 Memos 建立旧日记推导情景卡、纯正文 passage 和滚动状态；旧数据没有原文时自动使用日记兼容层。 |
| 平时在 Memos 中增加、修改或删除了日记 | 执行 `/memos-sync`；后台对账也会按设置周期同步删除。 |
| 更换了 Embedding 模型或维度 | 执行 `/memos-reindex`，不能只用增量同步。 |
| 只想让旧索引获得可追溯段落 | 执行 `/memos-passage-rebuild`，不改写 Memos 原文。 |
| 想单独重建 4.0 情景卡 | 执行 `/memos-episodic-rebuild`；只重建本地机器视图，不改 Memos 正文。 |

### 最常用指令

| 指令 | 用途 |
| --- | --- |
| `/memos-sync` | 增量同步 Memos，并清理本地已删除的 memo。 |
| `/memos-reindex` | 从 Memos 全量重建本地检索索引。 |
| `/memos-episodic-rebuild` | 从全部 Memos 重建 4.0 情景卡与旧日记推导证据，不改原文。 |
| `/memos-state-status` | 查看滚动当前状态、版本、字数和待处理更新。 |
| `/memos-state-rebuild` | 用现有 4.0 情景卡与旧画像种子重新融合当前状态；历史版本保留。 |
| `/memos-search <关键词>` | 手动测试记忆检索。 |
| `/memos-buffer-status` | 查看尚未压缩的对话缓冲。 |
| `/memos-profile-update` | 手动融合并生成一次长期画像。 |
| `/memos-context-status` | 查看上下文治理和归档状态。 |
| `/memos-health` | 检查索引、时间、反馈和记忆质量。 |
| `/memos-cluster-rebuild` | 重新计算全部日记的相似簇。 |
| `/memos-insight-update` | 立即重建内置时间洞察候选。 |
| `/memos-insight-status` | 查看时间洞察版本、候选数、环境注入数和旧附属接管状态。 |

完整命令、参数和危险操作说明见后文的[命令列表](#命令列表)。召回反馈只在 WebUI 操作，没有聊天命令。

### WebUI 地址

默认 `webui_host=127.0.0.1`、`webui_port=8088`：

| 页面 | 地址 | 主要用途 |
| --- | --- | --- |
| 主 WebUI | `http://127.0.0.1:8088/` | 4.x 三层记忆、夜间检查点、原文证据、召回实验室、月份档案、反馈、画像、设置和上下文。 |
| 记忆生产 | `http://127.0.0.1:8088/production` | 独立查看逐篇 Episode、场景切分、原文回链、证据分层、日记质量、状态更新、重写预览与回滚。主页只保留入口。 |
| Console | `http://127.0.0.1:8088/console` | 注入构成、事件日志、缓存命中与前缀稳定性诊断。 |
| 心潮工作台 | `http://127.0.0.1:8088/xinchao` | 心理状态、身体节律、梦境、主动表达，以及独立的时间洞察页面和设置。 |

监听地址或端口改过时，请使用实际配置值。

### 四句话理解 4.x 链路

1. **原文档案**：压缩前把本批原始对话按会话、轮次和时间幂等保存；4.4 同时建立可重建的原始轮次向量，使日记漏写的细节仍能被找回。
2. **事件记忆**：先从原始对话提取可回指轮次的 Episode，再用 Episode 写第一人称文学日记。事件记忆负责事实、人物、日期、情绪变化、承诺和未解决事项，日记负责完整情感弧与文学连续性。
3. **滚动当前状态**：成功压缩后先把状态增量可靠排队；首次状态或显著承诺、边界、身份、关系转折立即融合，普通变化默认累计 3 个压缩批次，最长等待 72 小时。状态融合仍用“旧状态 + 待处理新情景”完整替换当前状态；日记、原文和检索入口不会等待它。
4. **全 MEMORY 融合召回注入**：同一个 query embedding 并行搜索事件卡、日记 passage、原始轮次和 BM25，时间问题再按需加入 temporal。入选结果融合为“事件核心 + 日记视角 + 必要一手证据”，普通问题默认目标 3 条，叙事问题默认最多 6 条；4.5 起使用软字符目标治理，超出时逐条紧凑化但不丢弃记忆，弱结果轮次自动触发安全网宽搜。

## 4.x 当前架构重点

- **不破坏 4.0 数据**：数据库迁移只新增表，重复启动幂等。已有 `source_batches`、`source_turns`、`episodes` 和 `episode_evidence` 原样保留；Memos 正文不改写。
- **原始对话成为底座**：新压缩批次先归档再生成。模型失败、解析失败、Memos 写入不完整或证据库写入失败时不清 buffer；成功后原始档案也不会随 buffer 清理而消失。
- **状态是覆盖，不是追加**：状态更新每次输出完整新文档，同义变化会合并，过期临时情绪可被新证据覆盖，承诺、边界和未解决事项不会因简单截字丢失。每版快照可在 WebUI 折叠查看。
- **状态 LLM 自适应调用**：普通请求只读取并临时注入已保存状态。首次状态、显著变化、累计达到批次阈值、最长等待到期、失败重试或手动重建时才调用；普通压缩批次只入队，不再每批都重写状态。
- **有序失败队列**：多会话同时压缩时，状态批次按归档顺序串行融合。旧批失败会留队重试，新批不会越过它制造时间倒流；状态失败不阻断日记和原始档案落盘。
- **旧数据自动衔接**：首次升级读取旧画像作为种子，并从已有情景卡中选择最近事件和高重要度长期锚点生成首版状态。旧日记没有原始聊天时仍诚实标记为 `diary_derived`。
- **全记忆精简主路**：一次查询 embedding 同时搜索情景卡、纯正文日记 passage 和原始对话轮次；三种表示相互救回，BM25 保护专名和原句，temporal 只在可定位时间问题触发；候选统一最多 rerank 一次。
- **证据覆盖而非相似去重**：最终选择保护不同日期、人物、关系阶段、承诺边界、状态变化和未解决事项。只有同日、同事实、同阶段的重复表达才受到硬重复阻止；主题相似不会让需要的日记消失。
- **两类无损索引迁移**：日记 passage 继续后台迁移为纯正文向量；4.4 另外为已有 `source_turns` 建立原始轮次向量。两者都只写本地派生索引，不读取或改写 Memos 正文，失败时回退其他表示。
- **旧复合链只作安全网**：精简主路正常且结果足够强时不执行 entity/relationship 多路 embedding、月份并行补搜、相似簇折叠或旧信息增益配额；只有主路为空/过弱、主路关闭或运行故障时才调用旧链，另保留为消融基线。
- **注意力友好融合注入**：常驻一份当前状态，本轮只取少量记忆；每条同时保留紧凑事件核心和日记文学视角。原始轮次直接命中时普通 RP 最多带一条短证据；日期、原话、承诺、边界或争议问题最多两条。证据不再作为第二个重复大包追加。
- **内置时间洞察 v4**：从有可靠日期的本地记忆离线建立纪念日、近期趋势和季节候选，并读取 `episodic_memory.db` 的证据来源、原文回链与 grounded 数量。原文可追溯证据获得克制加权，旧 `diary_derived` 日记不受惩罚；滚动状态只帮助后台审校“这段历史对现在是否仍有解释价值”，绝不充当日期证据。普通轮次最多给一条高置信环境回声，邻近纪念日和季节模式只在问题明确涉及时检索。每轮只查本地 SQLite，不调用洞察 LLM，也不占剧情召回数量。
- **23:45 自适应夜间检查点**：未达到正常压缩阈值的完整轮次也会归档。它按真实跨日、长时间停顿和对话密度计算情景容量，证据层再决定实际篇数；不再按固定轮数硬凑日记。提交成功后排队更新滚动状态并刷新时间洞察。
- **真实评测闭环**：WebUI 的“有帮助”和“关键记忆”会把真实问题与命中 memo 加入评测集；召回实验室可比较 `lean`、`no_source`、`direct`、`vector`、`legacy` 的 Hit@3/5/10、MRR 与平均延迟，单独量化原文档案的增益。

### 4.x 实际注入顺序

1. `CurrentTimeContext`：本轮唯一现实“现在”。
2. RP enhancer 的即时约束。
3. `CurrentSemanticState`：当前收敛状态，不是历史事件清单。
4. `HistoricalMemorySet`：普通问题目标 3 条，叙事或明确时间问题最多 6 条融合记忆；只注入通过资格线和证据覆盖选择的条目，每条包含事件核心、日记视角和最多 1–2 条必要一手证据。
5. `IntegratedHistoricalTimeInsight`：有日期证据且达到门槛时才出现；位于融合记忆之后，不冒充当前事实，也不占剧情记忆名额。
6. `DynamicMindState`：心潮对“这些历史怎样影响此刻”的即时心理表达，保持在临时内容末端。即时感知与回复后结算共享同一轮现实时间、身体节律和滚动状态；后路会根据实际回复确认、消解或衰减前路激活，整轮只落库一次。

所有动态块继续使用 `ProviderRequest.extra_user_content_parts`，不反复改写稳定 `system_prompt`。

### 4.x 核心设置

| 设置 | 默认 | 作用 |
| --- | ---: | --- |
| `raw_evidence_archive_enable` | `true` | 压缩前保留原始轮次；这是以后回原文取证的根。 |
| `semantic_state_enable` | `true` | 启用滚动当前状态。 |
| `semantic_state_target_chars` | `1800` | 融合目标，不是机械截断；范围 600–6000。 |
| `semantic_state_update_policy` | `adaptive` | `adaptive` 自适应累计；`every_batch` 保留旧版逐压缩批次更新。 |
| `semantic_state_batch_threshold` | `3` | 普通变化累计几个成功压缩批次后融合。 |
| `semantic_state_max_wait_hours` | `72` | 即使批次数不足，也不会让普通状态增量等待超过此时长。 |
| `semantic_state_significance_threshold` | `0.72` | 显著变化分数线；明确语义类型的承诺、边界、身份和关系转折可硬触发。 |
| `semantic_state_merge_max_batches` | `6` | 单次最多按时间顺序融合的排队批次数。 |
| `semantic_state_auto_bootstrap` | `true` | 用已有 4.0 情景卡和旧画像初始化首版状态。 |
| `semantic_state_bootstrap_episode_limit` | `120` | 首次融合最多读取的情景数，最近事件与高重要度优先。 |
| `semantic_state_replace_profile` | `true` | 状态就绪后接管旧画像注入；画像数据和历史仍保留。 |
| `lean_recall_enable` | `true` | 启用当前精简主路；关闭时回退 4.0 级联。 |
| `lean_recall_candidate_k` | `50` | 情景卡、纯正文 passage 与 BM25 的宽候选池。 |
| `lean_event_index_enable` | `true` | 同一个 query 向量并行检索事件级情景卡。 |
| `lean_source_evidence_enable` | `true` | 同一个 query 向量并行检索原始对话轮次，救回日记遗漏细节。 |
| `lean_coverage_selection_enable` | `true` | 保护不同日期、关系变化、承诺和未解决事项，只拦同日同事实重复。 |
| `lean_adaptive_evidence_enable` | `true` | 普通 RP 只为承诺、边界、未解决事项等必要记忆展开最多 1 组；日期、原话、争议等精确问题最多展开 2 组。每组内部条数由 `episodic_evidence_per_memory` 控制。 |
| `passage_vector_auto_migrate` | `true` | 后台把旧 passage 迁移为纯正文向量，不修改 Memos。 |
| `source_turn_vector_auto_migrate` | `true` | 后台为已有原文档案建立可重建向量索引。 |
| `lean_temporal_enable` | `true` | 仅具体时间意图增加 temporal 元数据候选，不额外 embedding。 |
| `lean_story_min_inject` | `1` | 有合格结果时的基础剧情数量。 |
| `lean_story_max_inject` | `6` | 叙事/明确时间问题的剧情硬上限（可到 10）；不会为了凑数突破资格线。 |
| `lean_story_normal_inject` | `3` | 普通问题的注入目标条数。 |
| `lean_relative_margin` | `0.24` | 普通问题相对分差容忍度；比最强命中低超过该值才被裁掉。 |
| `lean_relative_margin_broad` | `0.34` | 叙事/时间问题的相对分差容忍度。 |
| `recall_safety_net_enable` | `true` | 选择结果为空或过弱时自动触发旧复合链宽搜救回，保证不漏检。 |
| `inject_char_budget` | `10000` | 注入软字符目标；超出时按排名逐条紧凑化（全文→段落→紧凑摘录），不丢弃记忆且最高分记忆保持原形。必要时允许超出并在 Console 显示超出量；设 0 关闭。 |
| `lean_texture_enable` | `false` | 日常质感不在无关话题里主动抢位；被强语义、明确词面或日期直接问到时仍可救回注入。 |

WebUI 四档预设已按 4.6 全 MEMORY 架构重写。它们只修改数值、开关和预设自身的模式参数，不会覆盖任何手填项，包括 Memos Token、角色名、Provider ID、URL、数据库路径、端口、时区、Tier 词表或托管目录；召回实验室四档使用同一道后端保护。四档都保留原文档案、证据优先生成、文学日记、滚动状态、时间边界和 14 天数据备份。

主设置页、心潮和时间洞察中的 LLM 模型均从 AstrBot 当前已启用的 Chat Completion Provider 生成下拉列表。留空表示跟随当前或最近会话模型；已配置但暂时停用的 Provider 会保留为“当前不可用”，保存其他设置时不会丢失。`emb_provider_id` 与 `rerank_provider_id` 按能力类型和现有兼容约定继续手填，不混入聊天模型列表。

### Tier 重要度词表还有什么用

Tier 仍然有用，但它不是主检索路线，也不会强行覆盖可靠的 LLM 判断。日记生成结果已经包含合法 `importance=1-5` 时直接采用该值；只有重要度缺失或非法、旧日记重建、手动写入以及兼容恢复路径才调用 Tier 启发式。

- Tier 5 命中一次即可进入 5 级；Tier 4 命中两次为 4 级、三次可到 5 级。
- Tier 3 为普通情感与生活锚点；低重要词密集出现时会对日常流水适度降级。
- 强标点与较长正文只提供有限加分，最终始终限制在 `1-5`。
- 重要度继续参与加权排序、滚动状态初始化/更新来源、高重要记忆保护和诊断；它不会单独决定是否注入，相关性资格线仍然有效。

4.1.2 起默认词表扩充了关系定义、共同生活、信任变化、创伤安全、身份真相、长期习惯、喜好与生活锚点。若配置仍是旧版完整默认值，启动时自动换成新默认；用户自己编辑过的词表不会被覆盖。

## 4.0.0 兼容底座

- **记忆生成重构**：新增“证据提取 -> 文学渲染”双阶段生成。提取结果必须指向合法 `[turn:N]`，原话必须逐字存在；每个情景至少有一项真正落到原始轮次的证据。
- **完整性校验**：文学日记写完后检查已核验证据覆盖率。遗漏明显时只重写一次；仍不合格则回退原有单阶段流程，未成功持久化时保留缓冲。
- **原始证据归档**：已处理对话按内容哈希幂等写入独立 `episodic_memory.db`。不会把原始聊天塞进 Memos 正文，也不会直接读写 Memos 自己的 SQLite。
- **旧记忆无损对接**：已有 Memos 自动建立 `diary_derived` 情景卡，完整日记和段落照常参与检索。旧日记没有原始轮次，因此 WebUI 会诚实标记为“旧日记推导”，不会伪装成逐字证据。
- **编辑与删除一致性**：Memos 仍决定有效日记集合。编辑新日记后保留原始证据并标记为混合；删除 Memos 后，兼容索引与情景卡都会删除，原始归档不会再参与召回。
- **级联检索**：情景卡筛选、候选内段落精搜、单次 rerank 替代每轮多路向量查询与月份补搜。短指代问题才拼接有限上文，明确问题不拼接无关上下文。
- **信息增益保护**：4.0 强制在合格候选上做日期、人物、关系、事件阶段与状态变化覆盖；相似簇只作诊断，不能先折叠掉可能需要的日记。
- **注意力友好注入**：默认最多优先给 1 篇完整日记，其余使用可追溯段落；所有入选事件保留紧凑焦点，最相关或必要事件展开查询相关证据。
- **故障开放**：事件库空、迁移中、迁移失败、双阶段模型失败或 4.0 被关闭时，均回退已有 3.x 链路，不阻断 AstrBot 请求。
- **可视化**：主 WebUI 新增“情景证据”，展示证据等级、事件卡、状态变化、原始轮次、迁移状态和事件库后端；召回实验室与真实聊天使用同一 4.0 管线。

完整的时间与注入边界见[时间与注入架构报告](TIME_AND_INJECTION_REPORT.md)。

### 4.1 数据边界与备份

| 数据 | 位置 | 是否可从 Memos 重建 | 作用 |
| --- | --- | --- | --- |
| 可阅读完整日记 | Memos | 本身就是来源 | 用户浏览、编辑、增删以及完整正文回取。 |
| 兼容段落/关键词/月索引 | `memories.db` | 可以 | 3.x 回退、段落定位、反馈与其他既有功能。 |
| 情景卡 | `episodic_memory.db` | 可以，但旧日记只能推导 | 原始轮次与日记之间的证据视图；4.1 用于取证和状态融合，不单独跑一条召回路线。 |
| 新记忆原始轮次证据及派生向量/词面索引 | `episodic_memory.db` | 原文不可以，索引可以 | 主体、原话和事实的逐轮可追溯依据；4.4 参与独立召回。 |
| 当前滚动状态与版本 | `episodic_memory.db` | 可以部分重建 | 当前注入使用的收敛状态；版本用于审阅与回溯。 |
| 状态更新队列与召回评测集 | `episodic_memory.db` | 评测集不可以自动重建 | 失败重试、有序融合和真实查询消融。 |
| 心潮状态 | `xinchao_state.json` 等原文件 | 不适用 | 当前心理动态，4.0 未改变其数据边界。 |

因此，日常备份至少应包含 Memos 数据目录、`memories.db`、`episodic_memory.db` 和画像/心潮 JSON。只保留 Memos 不会丢完整日记，但新记忆会退化为“旧日记推导”，失去原始聊天轮次。

`4.6.0` 默认启用插件本地数据备份。它每 14 天使用 SQLite 在线快照创建一个 ZIP，完成数据库完整性检查后再原子落盘，默认保留最近 6 份。WebUI 概览保留“立即备份”快捷操作，左侧 `13 备份管理` 提供列表、完整性校验、下载、删除、恢复预约与取消预约。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `data_backup_enable` | `true` | 启用插件本地数据周期备份。 |
| `data_backup_interval_days` | `14` | 两次自动备份之间的天数。 |
| `data_backup_keep` | `6` | 最多保留多少个 `memory_backup_*.zip`。 |
| `data_backup_dir` | `./data/astrbot_plugin_memos_memory/data_backups` | 备份目录；修改后重载插件。 |

备份包含 `memories.db`、`episodic_memory.db` 以及同目录下存在的心潮状态和运行遥测 JSON；不包含 AstrBot 插件配置、Memos Token、外部或托管 Memos 服务数据库。Memos 自身的数据目录仍需单独备份。备份任务在工作线程中执行，失败只记录日志并等待重试，不阻断聊天请求。

恢复不是在线热替换。WebUI 先校验 ZIP、文件摘要与 SQLite 完整性，再自动创建一份当前数据的 `pre_restore_safety` 安全备份，并写入恢复预约。目标备份和安全备份在预约期间都受保护，不参与保留数清理。下次重载插件时，恢复会在向量库、原文库和心潮状态打开前执行；成功后写入 `last_restore.json`，失败则回滚已替换文件、保留当前可用数据并继续启动。恢复范围只包括两套插件数据库以及心潮状态/设置；AstrBot 配置、Token、Memos 服务数据、运行遥测和当前路径设置不会被旧快照回滚。

主页不再把“重要性分布”作为 4.6 架构的主要概览。旧 `importance` 字段、统计接口和检索兼容权重全部保留；可视卡片改为三种真实结构统计：记忆类型、Episode 证据来源、原文可追溯性。

## 核心能力一览

<details>
<summary>展开完整能力与历史演进速览</summary>

- 自动压缩聊天为第一人称日记，写入 Memos。
- 本地 sqlite 向量索引、BM25/关键词索引、相似簇索引、月份路由索引和查询相关反馈。
- 分层召回：人格塑形、剧情连续性、日常质感分开控制。
- 长期人格画像：支持手动更新、自动周期更新、历史版本保存。
- WebUI：三层记忆状态、记忆列表、原文证据、召回实验室、月份档案、查询反馈、健康检查、上下文浏览、画像历史和插件数据备份管理。
- Console：事件日志、注入构成统计、Provider token 缓存观察、前缀漂移和上下文占比观察。
- 内置 RP enhancer：现实时间、对话节奏、复读提醒、镜像动作提醒。
- AstrBot 上下文治理：请求级裁剪和可备份的持久归档。
- 心潮动态心智：十二维长期压力、当前激活、分级满足、闪念/执念、疲劳、睡眠、梦境、醒后余韵和行动意向。
- 身体节律：四阶段连续变化、四时段平滑修正、六维身体信号、主体体验、身心冲突协调与统一注入。
- 两阶段对话感知：规则、混合、LLM 三种模式；请求前即时评估本轮反应，回答后结合实际回复复核并持久结算。两路共享统一时间/身体快照和当前滚动状态，已被回复满足的激活不会再次累计；LLM 失败、残缺 JSON 或超时会回退规则。
- 心理倾向注入：使用临时 `extra_user_content_parts`，不改角色设定，不替代日记与画像，不污染稳定 system prompt。
- 独立心潮工作台：`/xinchao` 展示真实状态、心理轨迹、注入预览、模拟实验和全部心潮设置。
- 每晚记忆检查点：每天 `23:45` 对尚未落盘的完整轮次建立不可变快照，先保全原文，再按日期、时间间隔和内容密度生成最多 N 篇有证据支撑的日记；不会为了凑篇数硬拆同一场景。
- `2.2.4` 修复：压缩结果如果把 `#标签` 混进正文，会自动剥离并合并到结构化 tags 字段，再统一写到日记末尾标签行。
- `2.2.5` 新增：可选托管 Memos 模式。插件只管理你放进指定目录的 `memos.exe`，不扫描、不接管本机其他 Memos 实例。
- `2.2.6` 新增：规则版注入重排、动态注入数量、相似簇软折叠实验室。相似簇默认每簇 2 篇，真实注入应用默认关闭。
- `2.2.6` 调整：旧关系图谱不再参与召回治理，改为预计算相似簇。`/memos-graph-rebuild` 保留旧指令名但实际重建相似簇，同时新增 `/memos-cluster-rebuild`。
- `2.2.6` 修复：托管 Memos 自动端口会写入 `managed_state.json`，插件重启时优先复用上次端口；如果上次托管进程还在，会直接接入，不再跳到 `+1`。
- `2.2.6` 修复：请求期召回增加非阻塞保护。embedding / search / rerank 超时会跳过或回退，不再允许记忆召回卡住整个 AstrBot。
- `2.2.6` 后续修正：本地记忆索引为空时只跳过记忆检索；时间增强、KB cache、上下文治理和缓存诊断仍正常运行，避免新 bot 卡住同时保留请求保护。
- `2.2.6` 修复：空库时画像自动更新后台任务会忙循环的问题；空本地记忆索引不再触发自动画像生成。
- `2.2.6` 修复：async rerank provider 回到 AstrBot 当前事件循环中限时调用，避免线程新事件循环导致 `Timeout context manager should be used inside a task`。
- `2.2.6` 优化：相似簇重建改用平均 embedding，并加入文本重叠、标签/类型兜底边，避免旧日记全部变成孤岛簇。
- `2.2.7` 优化：当前时间改为高优先级 `CurrentTimeContext`，召回日记改为 `HistoricalMemory` 边界，避免 LLM 把历史日记日期误当成当前时间。
- `2.2.8` 优化：KB cache marker 识别、知识片段切分和包含式去重更稳；历史记忆边界进一步明确“不是刚刚/本轮刚发生”。
- `2.2.9` 优化：新增 system prompt 缓存友好守门，误入 system 的动态块会搬回临时 extra，并在 Console 显示 system hash/长度/动态残留。
- `2.2.10` 优化：当前时间块会清理旧块后重新生成，并插到临时 extra 的最前面；空记忆索引时仍执行时间/KB/context 治理；Console 新增 cache prefix drift，用于判断缓存不稳是 system 还是 history 在变化。
- `2.2.11` 新增：WebUI 增加 `KB 分析` 页签和概览卡片，用真实运行样本分别分析 KB 剥离/临时注入，以及独立的 provider cached token/prefix drift。
- `2.2.12` 修复：画像 LLM 调用使用独立 `profile_llm_timeout`，不再复用召回搜索 timeout；默认 90 秒，慢模型可调高。
- `2.3.0` 时间架构：严格拆分事件发生时间、Memos 创建/更新时间、本地索引时间和本轮当前时间；旧日记可在同步/重建时补全年份。
- `2.3.0` 压缩事务：压缩前固定 sqlite 缓冲快照边界，成功后只删除已处理消息；压缩期间新消息、失败残留和超过单批上限的旧消息不会被误删。
- `2.3.0` 召回优化：新近度按事件发生时间计算；普通“今天”不再触发旧日记同月日加权；上下文 query 去重并限制为最近 3 条、最多 900 字。
- `2.3.0` 数据安全：`/memos-sync` 正确识别 Memos camelCase 时间字段；`/memos-reindex` 保留反馈、锚点、画像和摘要等用户数据。
- `2.3.1` 运行观测修复：每次真实 LLM 请求都会记录注入构成，Console 事件与样本持久化到 `runtime_telemetry.json`；热更新时新版 WebUI 会接管遗留端口，不再出现 AstrBot 有日志而页面空白。
- `2.3.2` 历史会话修复：WebUI 不再只依赖本次进程见过的 session，而是直接枚举 AstrBot 当前会话数据库；插件重启、热更新或切换旧 bot 后仍能浏览现有历史对话和归档备份。
- `2.3.2` 接口修复：统一召回实验室反馈值、锚点默认类型及锚点列表字段，修复页面按钮能点击但后端拒绝或缺少展示数据的问题。
- `2.3.2` 兼容修复：会话历史同时兼容 AstrBot 当前 JSON 字符串格式及 list/dict 兼容格式；遥测恢复时会恢复已知 session，归档链路共用同一解析器。
- `2.3.2` 全链路复核：核对 AstrBot 4.25.5 请求钩子、ProviderRequest、TextPart 临时注入和 ConversationManager 接口，并模拟验证时间、召回、rerank、压缩、画像、重建、同步、上下文归档及 36 个 WebUI/Console 路由。
- `2.3.3` 动态数量修复：不再把 `recall_max_inject` 直接当作实际注入数量；每增加一篇都要求更强的语义、rerank 或触发证据，普通召回会自然停在较小数量，只有广泛且强相关时才达到最大值。
- `2.3.3` rerank 修复：保存 provider 的真实 `relevance_score`，只救回达到证据线的候选；不再把 rerank 前 20 篇全部绕过相关度硬门槛。
- `2.3.3` 关键词校准：关键词候选根据命中词数量和权重估算相关性，不再统一伪装成 `0.9` 高相关。
- `2.4.0` 多路召回：当前表达、最近上下文、人物/物件、关系/情绪、时间事件分别检索，再使用 RRF 融合；不增加额外 LLM 调用。
- `2.4.0` 必要日记保护：先锁定当前问题的直接答案、明确日期范围和事件阶段，再按边际信息增益补足候选。相似簇只做可视化诊断，不能压掉必要日记。
- `2.4.0` 段落索引：Memos 继续保存完整文学日记，本地记录每段所属 memo、段落编号、原文字符范围和正文哈希；旧日记可无损重建。
- `2.4.0` 混合注入：核心事件、完整情感弧、时间叙事和必要日记使用全文；仅提供局部事实的长日记可使用带来源日期的相关段落。
- `2.4.0` 写作质量：取消日记正文硬字数上限，完整保留动作、对话、称呼、物件、情感变化、关系因果和结果；机器检索字段不能替代文学正文。
- `2.4.0` 数据安全：增量同步替换派生索引时不再误删反馈和锚点；段落重建同样保留反馈、锚点、画像、摘要和 Memos 原文。
- `2.4.1` WebUI 设置：覆盖 `_conf_schema.json` 的全部插件设置，支持搜索、分类、类型化控件、恢复默认值、敏感字段单独清除和修改计数。
- `2.4.1` 一键预设：深度角色塑形、效果优先、均衡推荐、低成本四档；仅调整效果、场景和成本相关参数，应用前显示差异预览。
- `2.4.1` 配置安全：Token 不返回明文，空白输入不会覆盖旧 Token；写入校验同源请求，持久化失败会回滚内存配置。
- `3.0.0` 心潮核心：以 Python 原生方式移植确定性状态机，无需 Node、额外进程或额外端口；状态独立写入 `xinchao_state.json`。
- `3.0.0` 心理感知：每轮回复后在后台将自然对话转换为保守事件，支持零调用规则模式、按需混合模式和全量 LLM 模式。
- `3.0.0` 情绪表达：回复前注入私密心理倾向，仅微调注意力、情绪温度、犹豫、主动性和措辞，并明确禁止覆盖事实、角色边界和当前场景。
- `3.0.0` 梦境融合：睡眠期间可读取本地长期日记索引中的高重要性材料，生成梦境、余韵与内心理解；梦境始终标记为非现实事件。
- `3.0.0` 主动表达：复用 AstrBot 主动消息接口，带空闲门槛、冷却、每日上限和近似重复拦截；默认关闭。
- `3.0.0` 运行观测：Console 新增 `xinchao` 日志类别和注入占比；独立工作台提供十二维状态、闪念/执念、感知来源、梦境和模拟。
- `3.0.0` 数据隔离：心潮设置保存在 `xinchao_settings.json`，不进入主设置页，不会覆盖 Memos Token、角色名、Provider、路径或端口。
- `3.1.0` 本轮感知：修复心潮只能在回答后更新、当前回复只能读到上一轮状态的问题；即时评估不提前写状态，避免把尚未发生的结果当作已满足。
- `3.1.0` 原版能力补齐：加入紧凑梦境呼吸上下文、跨类型主动消息历史与去重重试、梦境余韵消息、白天记忆浮现、随机排期、时区窗口及每日上限。
- `3.1.0` Astr 适配：Bark 由 Astr 主动消息接口替代，Memory MCP 读取由本地 Memos/sqlite 索引替代，HTTP 状态 API 由独立心潮 WebUI/API 替代。
- `3.1.0` 可观测性：心潮工作台分别显示请求前即时评估、回答后持久结算、真实主动消息和紧凑梦境上下文。
- `3.1.1` 身体节律：吸收 Period 的周期计算思想，重写为心潮内部的瞬时身体底色；不移植其三次 LLM 情绪决策、冷暴力、已读不回或响应拦截。
- `3.1.1` 身心合成：身体信号与心潮共用一个 `<DynamicMindState>` 临时块，身体只影响节奏、感受和注意力，不自动制造情绪、欲望或关系结论。
- `3.1.1` 工作台：`/xinchao` 新增身体节律大页面、四阶段位置、六维信号、真实注入预览及全部身体设置。

</details>

## 工作机制

插件主要由七条链路组成。

### 0. 心潮动态心智链路

心潮与长期记忆是互补关系：

- 日记回答“过去发生过什么”。
- 画像回答“角色长期变成了怎样的人”。
- 心潮回答“此刻哪些欲求、牵挂、失落或犹豫更活跃”。

每轮请求前，插件先按现实时间结算当前状态，再对当前用户输入做即时评估。即时评估只用于本轮注入，不会提前满足驱动力或永久修改状态。随后插件把长期状态与本轮即时反应合成不超过独立上限的 `<DynamicMindState>` 临时块。该块追加到 `ProviderRequest.extra_user_content_parts`，不会写入稳定 system prompt，也不会修改 AstrBot 历史上下文。记忆召回完成后，心潮块会被移动到所有临时内容末端，让模型最后读到“过去与长期状态怎样影响此刻”。

十二维每一项都有三个不同概念：

- **积累**：长期未满足的需要，或已经发生并仍在消退的情绪余波。普通需要会随时间缓慢趋近上限；`grieve` 与 `anger` 不会凭空增长，而是分别按较慢和较快的半衰期自然消退。
- **激活**：某句话或近期事件把一个方向推到心理前景的程度。它可以快速升高，也会按维度自己的半衰期回落，不会因为聊天频率高就被结算次数机械磨掉。
- **满足**：本轮实际发生了多少满足，使用 `0.0-1.0` 分级强度，只缓解相应比例的长期积累和少量即时激活。普通回复不是“一次聊完就完全满足”。

注入和行动意向使用积累与激活形成的有界综合值，但注入文本会区分“持续积累”“当前仍在前景”和“情绪余波”。因此长期想分享和刚被一句话触发不是同一种心理紧迫度。工作台双轨条展示原始两层，右侧数字展示综合值；心理变化证据页记录每次显著变化的前后值、来源与原因。

配置有效的身体周期锚点后，同一控制器还会在请求时计算当日身体底色：

- 由公历锚点和周期参数确定经期、恢复期、活跃期和缓降期，只在 WebUI 展示阶段名与天数。
- 身体精力、不适、感官敏感、社交余量、安稳偏好和亲近感知按阶段连续变化。
- 每日个体变化使用角色、日期和周期日生成稳定种子，并在当天平滑衔接到次日种子，因此同一天不会每轮随机跳变，午夜也不会突然换一套身体数值。
- 每个阶段分别拥有早晨、午后、傍晚和深夜修正；时段结束前会渐变到下一时段，修正同时影响内部信号与主体感受，不是整点切换的通用时间模板。
- 心潮会把最显著的一组身心关系合成为自然约束。例如“仍想保持连接，但身体社交余量偏低”会导向更简短专注的靠近，而不是被误写成不在意。
- 均衡模式向模型提供一条阶段主体感受、一条当前时段修正和最多两条显著信号；沉浸模式保留最多三条显著信号。
- 模型看不到周期阶段、周期日、日期或内部数值，也不会收到“现在必须撒娇、发火、拒绝或挽留熬夜”一类强制行为。
- 身体状态不写入 `xinchao_state.json`，不会累积成长期情绪，也不会污染事实日记和画像。

身体层回答“此刻身体是什么底色”，心潮回答“这个角色怎样理解并表现这种底色”。身体敏感不能自动变成生气，身体不适不能自动变成拒绝交流，亲近感知也不能绕过人设、关系阶段、同意与当前场景。

每轮回复后，感知器再处理本轮用户输入与角色实际回复，把这次互动的结果持久结算：

- `rules`：只使用保守关键词规则，不调用 LLM。
- `hybrid`：普通短对话使用规则；较长或有明显情绪信号时，请求前即时评估和回答后结算都可调用 LLM。
- `llm`：请求前和回答后每轮各调用一次感知 LLM，适合心理效果优先且延迟、调用成本充足的场景。

因此，“每轮检测”分成两种准确性：规则模式每轮都会运行但只能识别明确线索；混合模式对强情绪、关系变化和长消息使用 LLM；全量 LLM 模式最细，但会在主回复前增加一次有超时上限的模型调用。无论哪种模式，回答后结算都不会反过来伪装成当前回复之前已经发生。LLM 达到置信度后负责补充细节，但不会覆盖规则已经确定的关系断裂、冲突余波、真实交流和角色自我表达等可观测证据。

### 原项目能力如何映射到 Astr

| 原项目组件 | Astr 插件中的实现 |
| --- | --- |
| 确定性状态机 | Python 原生移植并增强：12 维积累/激活/分级满足、情绪半衰、夜间倍率、清晨冻结、疲劳、睡眠、闪念、执念、意向 |
| `conversation-event` | Astr `on_llm_response` 回答后结算 |
| Memory heartbeat 文件 | Astr 请求/回复钩子直接更新最后互动时间，不需要轮询外部文件 |
| `GET /v1/intent` | `/xinchao` 状态 API 与 `/memos-mind-status` |
| ModelClient 梦境 | Astr Provider 生成梦境、余韵和醒后理解 |
| Memory MCP 读取 | 直接读取主插件本地 Memos/sqlite 长期记忆索引 |
| Memory MCP 写梦境 | 梦境保存在独立 `xinchao_state.json`，不混入事实日记召回，避免把梦当成真实经历 |
| Bark | Astr `context.send_message` 主动私聊 |
| 跨类型 Bark 去重 | 梦境消息、自主念头、白天浮现共享最近 8 条发送历史，并可重试改写 |
| 白天浮现 | 本地日记材料、随机间隔、当地时间窗口、独立每日上限、日记来源冷却与并发双发保护 |
| HTTP 服务和 Bearer 鉴权 | 插件内 `/xinchao` WebUI/API，同源写入检查 |
| Docker、Node 进程 | 不需要；由 Astr 插件生命周期托管 |

原项目本身不代理聊天模型请求，也不提供对话提示注入；它只暴露状态和意向给外部 Agent。`<DynamicMindState>` 与两阶段感知是 Astr 适配层，为了让状态真正影响角色回复而新增，不是简单复制 Node 文件。

白天记忆浮现有两层重复保护。第一层比较最近 8 条梦境消息、自主念头和白天浮现文案，过近时要求模型重写；第二层记录本次实际采用的 Memos 日记来源，默认 `72` 小时内不再把同一篇日记送入候选。模型按要求返回候选编号时只冷却实际采用的一篇；旧模型若只返回纯文本，则保守冷却本轮全部候选。来源记录保存在 `xinchao_state.json`，插件重启后继续生效。若所有日记都仍在冷却，系统跳过本次浮现并重新排期，不会为了凑次数重复发送。相同角色的并发到期检查也会串行执行，第二个任务会读取第一条刚写入的冷却记录。

原项目工作区约 2.13 MB，其中 `docs/cover.png` 单文件约 2.04 MB，`src/` 实际约 58 KB。发布包没有复制封面、Node/Docker 部署脚本和独立服务外壳，因此 ZIP 大小不能用于判断移植完整度；应以这张能力映射和自动测试为准。

感知结果只允许包含：本轮已经满足的驱动力、小幅情绪变化、最多两条闪念、短互动摘要和置信度。解析器接受 JSON 代码围栏、前后说明、尾逗号、Python 风格字典、常见下划线字段、百分比置信度以及中文驱动力名；所有内容最终仍经过固定字段白名单和强度边界。低置信度、超时、Provider 不可用或 JSON 不合法时自动回退规则。请求前即时评估只调用一次，不因重试延长首包等待；回答后感知允许在原总超时内快速重试一次，且始终在后台执行。

同一感知 Provider 连续失败 3 次后会熔断 180 秒，期间直接使用规则，避免每轮重复等满超时。成功调用会立即清零失败计数。心潮工作台“最近感知”会显示当前 Provider、通道健康、精确回退原因、重试次数和恢复倒计时；显式选择的 Provider 失效时不会悄悄改用当前会话模型。

十二维状态为：

| 键 | 内在方向 |
| --- | --- |
| `possess` | 亲密、占有与靠近 |
| `monitor` | 牵挂、想知道对方近况 |
| `crave` | 依恋与身体接近 |
| `share` | 分享自己的发现和感受 |
| `libido` | 身体吸引，始终服从角色和关系边界 |
| `curiosity` | 好奇与探索 |
| `boredom` | 打破停滞 |
| `social` | 交流与连接 |
| `duty` | 责任与推进 |
| `reflection` | 沉淀与自我理解 |
| `grieve` | 难过与失落 |
| `anger` | 生气与不满 |

闪念按真实经过时间衰减，不再按后台结算次数或聊天频率衰减。同一心理线索被多次强化，或在足够长时间后仍保持强度，才会进入执念池；执念自身缓慢消退，并对对应方向提供有限激活反馈。角色长时间未互动后进入睡眠，梦境可以读取本地日记索引中的重要材料，但提示词明确要求记忆是过去、梦不是现实，不得新增现实事实。醒来后的余韵只作为有限的心理背景注入。

自主念头和白天记忆浮现也读取分层心理状态。自主消息同时参考长期积累、即时激活、盘旋念头和身心协调结果；白天浮现用当前心理方向从候选旧记忆中判断是否真的有自然联系，没有联系必须 `SKIP`。这不会改变普通对话中的记忆召回，也不会突破已有冷却、每日上限和跨类型重复保护。

心潮默认按角色共享一份连续状态；可在 `/xinchao` 改为按 session 隔离。主动消息默认关闭，开启后仍受空闲时间、冷却、每日上限、驱动力阈值和近似重复检查约束。

### 1. 压缩链路

当用户和角色聊天时，插件会在 `on_llm_response` 后收集本轮用户输入和模型回复，并追加到会话缓冲。

默认：

- 每 `compress_every_n_turns = 30` 轮 assistant 回复触发一次压缩。
- 每次目标日记数 `diary_count = 2`。
- 压缩 LLM 可以单独指定 `compress_provider_id`，也可以留空使用当前对话 LLM。

压缩输出不是简单摘要，而是结构化记忆：

```json
{
  "event_date": "2026-07-16",
  "time_label": "深夜",
  "time_basis": "conversation_now",
  "scene_anchor": "他临走前叮嘱我吃饭",
  "content": "我记得他离开前仍惦记着我有没有吃饭...",
  "memory_type": "emotional_anchor",
  "long_effect": "这让我之后在被照顾和被留下之间更容易犹豫。",
  "trigger_hint": "当再次谈到吃饭、等待、离开时，我会先想起这份被惦记的感觉。",
  "retrieval_key": "他临走前仍叮嘱爱莉吃饭，让她在离别中确认自己被惦记。",
  "state_change": "从担心被留下，变成能短暂相信这次离开不是抛弃。",
  "entities": ["爱莉", "吃饭", "等待", "离开"],
  "tags": ["#爱莉", "#吃饭", "#等待"],
  "importance": 4
}
```

写入 Memos 后，日记格式大致是：

```text
2026年7月16日 · 深夜
我记得他离开前仍惦记着我有没有吃饭...
长期影响: 这让我之后在被照顾和被留下之间更容易犹豫。
触发线索: 当再次谈到吃饭、等待、离开时，我会先想起这份被惦记的感觉。
#爱莉 #吃饭 #等待
<!-- memos-memory:importance=4;manual=0;type=emotional_anchor;source=auto;occurred_at=2026-07-16;time_basis=conversation_now;meta64=... -->
```

最后一行 metadata 是插件内部使用的隐藏信息。`occurred_at` 是事件发生日期，`time_basis` 说明日期来源；`meta64` 保存场景锚点、检索句、状态变化和实体。正常浏览与召回会清理隐藏行，它不会作为日记正文显示或注入。

2.3.0 起，时间分为四类，互不混用：

| 时间 | 含义 | 用途 |
| --- | --- | --- |
| 事件发生时间 | 日记里的场景实际发生于何时 | 历史排序、新近度、注入时间边界 |
| Memos 创建/更新时间 | memo 何时被写入或编辑 | 增量同步、来源审计 |
| 本地索引时间 | sqlite 何时重建该条索引 | 诊断，不参与记忆新近度 |
| 当前现实时间 | 本轮 AstrBot 请求发生的时间 | `CurrentTimeContext`，唯一“现在” |

### 2. 同步和索引链路

Memos 是记忆真相源，本地 sqlite 是检索索引。

`/memos-sync` 会从 Memos 拉取所有 memo：

- 解析首行时间、正文、末尾 tag 行、隐藏 metadata。
- 读取 `createTime/updateTime/displayTime` 及兼容的 snake_case 字段。
- 为只有“7月3日”的旧标题结合 Memos 来源时间补全年份。
- 按完整句边界建立可追溯 passage，记录段落编号、原文起止位置和正文哈希。
- 生成 embedding。
- 写入本地 sqlite。
- 重建关键词、相似簇等辅助索引。
- 删除本地索引里已经从 Memos 删除的旧 memo。

`/memos-reindex` 用于换 embedding 模型、迁移大量历史记忆、或怀疑索引不准时全量重建。

`/memos-passage-rebuild` 专门让已有日记获得 2.4.0 段落索引。它读取完整 Memos 原文并重建本地派生索引，不调用分类 LLM，不改写 Memos，不删除反馈、锚点、画像或摘要。之后新写入和 `/memos-sync` 更新的日记会自动增量建立 passage。

### 3. 召回链路

每次对话前，插件在 `on_llm_request` 中执行召回。

query 由当前用户输入构成，并会拼接最近的有效 AstrBot 对话线索。默认最多 3 条、合计最多 900 字；system 消息、与当前用户输入重复的消息不会重复拼接。日志中可以看到：

```json
{
  "query_len": 628,
  "user_input": "...",
  "context_query_parts": 3,
  "contexts_total": 80
}
```

含义：

- `contexts_total`: 当前请求里 AstrBot 传来的上下文总条数。
- `context_query_parts`: 实际拼到检索 query 里的最近上下文条数。
- 如果 `context_query_parts = 0`，说明这次没有可用上下文文本被拼入。

2.4.0 会先建立多条互补检索路线：

- `direct`: 当前用户原句。
- `context`: 当前句加最近有效对话线索。
- `entity`: 人物、称呼、地点、物件和高信息关键词。
- `relationship`: 关系行为与情绪线索。
- `temporal`: 明确日期、时段和事件关系。

各路线独立进行向量、BM25 检索，再由 RRF 合并到 memo 级候选，最后只调用一次可选 rerank。每个候选仍保留命中的 passage 和路线证据，因此可以回到“哪一篇日记的哪一段”。

`3.2.5` 另外启动一个互不依赖的月份分支。它先用月份的多个主题向量和结构化线索定位最多 `recall_month_route_count` 个月，再只在这些月份中补搜原始日记。直接分支与月份分支同时执行；月份超时只产生诊断，不取消直接分支。合并规则如下：

1. 直接分支的候选和分数原样保留。
2. 月份分支只加入直接分支没有找到的 `memo_name`。
3. 同一篇日记被两路命中时只保留直接候选，并记录月份路线证据。
4. 原检索候选先独立进入原有分层、信息增益和动态数量判断，原注入名额不会减少。
5. 月份独有候选单独通过相同质量门槛，再按 `recall_month_route_inject_max` 追加；默认最多额外 2 篇。
6. 月份路线从不排除未命中月份；即使月份索引为空、失败或超时，原检索与原注入结果也完全可用。

#### 两路命中内容怎样合并

合并身份使用完整 `memo_name`，不是标题、日期、正文相似度或段落文本。

| 情况 | 处理方式 |
| --- | --- |
| 原检索和月份路线命中同一篇日记 | 保留原检索候选、原分数、原 rerank 结果和原段落证据；只追加一条月份路线诊断。不会拼接两份正文，也不会再次加分。 |
| 同一篇日记的不同段落被不同路线命中 | 段落证据可以合并用于定位，但最终仍只按 `memo_name` 拉取一次原文、生成一个注入块。 |
| 两条路线命中不同日记 | 原检索日记先使用原动态名额完成选择；月份独有日记再使用独立补充名额追加。 |
| 同一天存在多篇不同日记 | 它们拥有不同 `memo_name`，不会因为日期相同而合并；月份日历会全部展示，召回时也分别判断是否需要。 |
| 月份路线失败、超时或索引为空 | 丢弃月份支路结果，原检索候选、数量和注入流程不变。 |
| 原检索完全没有候选 | 月份路线转为开放失败回退，使用正常动态选择，而不是被“最多补 2 篇”限制。 |

因此“去重”只消除同一原始日记的重复路线副本，不会把内容相似但事件不同的日记合并或删除。

召回融合这些因素：

- embedding 相似度。
- BM25/关键词命中。
- rerank provider 二次排序。
- 记忆重要性 `importance`。
- 手动钉记忆 boost。
- 相似查询下的 WebUI 反馈先验。
- 并行月份路由补充候选；不作为月份硬过滤。
- 明确纪念日意图下的同月日加权；普通“今天/现在”不触发。
- 最近注入去重窗口。

### 4. 注入链路

最终注入内容会经过分层选择。

主要层：

- `persona`: 长期人格、关系变化、情绪锚点、行为倾向、承诺规则。
- `plot`: 剧情事实、身份、地点、事件结果、物品状态。
- `texture`: 日常氛围、生活质感、轻量语气影响。

默认每次最多：

- `persona_top_k = 3`
- `plot_top_k = 2`
- `texture_top_k = 1`

Console 会显示每次注入构成，例如：

```text
diary=4647 memory=7470 profile=2821 current_time=230 time=0 xinchao=520 enhancer=174 context=32794
```

这里：

- `diary`: 真正被选中的日记块字符数。
- `memory`: 包含包装说明后的记忆注入字符数。
- `profile`: 长期画像注入字符数。
- `current_time`: 本轮当前现实时间块字符数。
- `time`: 内置或兼容附属时间洞察的注入字符数。
- `xinchao`: 心潮和身体节律合成块字符数。
- `enhancer`: 内置 RP enhancer 中除当前时间外的复读/镜像等提醒字符数。
- `context`: AstrBot 原始上下文字符数。

如果 total 很大，通常先看 `context`，再看日记、画像和心潮各自占比。

### 5. 上下文治理链路

长期 RP 最大问题是：Memos 已经记住了内容，但 AstrBot 原始历史仍然一直塞给 LLM，导致 token 越来越大。

插件有两种治理：

请求级裁剪：

- 默认开启。
- 修改当次发给 LLM 的 `ProviderRequest.contexts`；**注意：AstrBot 会把 Agent 实际使用的消息保存回会话，因此裁剪可能持久缩短 AstrBot 原始历史**（4.4 事故报告根因三）。
- 4.5.0 起裁剪前强制把裁剪前完整历史写入本地 JSON 备份（`context_trim_backup_enable`，目录同 `context_archive_backup_dir`）；备份写入失败则本轮跳过裁剪。
- 裁剪实际发生后会把 `conversation.token_usage` 置 0，避免 AstrBot 用裁剪前的旧 token 统计触发二次压缩（4.4 事故报告根因二）。
- 默认超过 80 条时只保留最近 40 条普通消息。

持久归档：

- 默认关闭。
- 会写回 AstrBot 会话历史。
- 执行前会生成备份。
- 用于你确认 Memos 压缩稳定后，真正清理 AstrBot 持久历史。

### 6. 缓存诊断边界

`3.2.1` 起，插件不再处理 AstrBot 知识库内容。AstrBot 如何检索、组织和注入知识库结果，全部由 AstrBot 当前版本负责。

插件只保留三项通用诊断：

- Provider Cache：读取模型服务实际返回的 cached token 数据。
- Prefix drift：比较相邻请求的 system/history hash、长度和变化来源。
- System guard：只处理本插件可能误入 system 的 `CurrentTimeContext` 与 `HistoricalMemory` 动态块。

这些诊断不会移动或改写 AstrBot 知识库文本。

## 安装和升级

### 基础要求

- AstrBot 已正常运行。
- Memos 服务可访问。
- AstrBot 中配置了可用的 embedding provider。
- 推荐单独配置一个稳定、便宜、理解力足够的压缩 LLM。

### 安装

将压缩包放到 AstrBot 插件目录，或通过 AstrBot 插件管理安装。

本版本压缩包结构应为：

```text
astrbot_plugin_memos_memory/
  main.py
  compress.py
  vector_store.py
  memos_client.py
  webui.py
  dashboard.html
  time_enhancer.py
  temporal.py
  repetition_guard.py
  metadata.yaml
  _conf_schema.json
  requirements.txt
```

如果压缩包里多套了一层无关目录，AstrBot 可能无法识别。

### 升级建议

`4.6.0-test4` 安装/升级安全边界：首次打开新版数据库结构前会自动生成快照，并按 `db_snapshot_keep`（默认 `3`）保留最近副本；迁移只在各数据库内部增量执行，**不会合并数据库**，也不要手工把 `memories.db`、`episodic_memory.db` 或 Memos SQLite 拼接/覆盖成一个文件。升级后在独立工作台 `http://<webui_host>:<webui_port>/production` 检查场景、证据、日记质量与重写预览；默认地址为 `http://127.0.0.1:8088/production`。

从 `2.3.x` 升级到 `2.4.0` 后，推荐先执行一次：

```text
/memos-passage-rebuild
```

这会让全部旧日记立即获得可追溯 passage、正文哈希和可用于检索的启发式场景锚点。过程不改写 Memos，也不调用 LLM；原有 feedback、anchor、profile、summary 均保留。日记很多时需要重新生成 embedding，请等待命令返回完成结果。

换过 embedding 模型、怀疑旧索引不完整，或希望从零重建全部派生数据时使用：

```text
/memos-reindex
```

`/memos-reindex` 会重新读取 Memos 的来源时间，为旧日记补充 `event_ts/source_created_ts/source_updated_ts/indexed_ts`，并同时建立 2.4.0 passage。Memos 原文不会被改写；反馈、锚点、长期画像历史和月度摘要会保留。

如果暂时不方便全量重建，也可以先执行 `/memos-sync`。由于旧索引没有 `source_updated_ts`，第一次同步会补建时间字段；但换过 embedding 模型时仍必须使用 `/memos-reindex`。

重建结束后，相似簇会自动刷新；也可以手动再次重建观察结果：

```text
/memos-graph-rebuild
/memos-cluster-rebuild
```

若你还要让旧压缩里混进正文的 tag 重新解析成结构化 tag，推荐：

```text
/memos-reindex
```

注意：旧日记如果已经把 tag 写进 Memos 正文，插件不会自动改写 Memos 内容。你可以手动编辑旧 memo，或重新生成/迁移。

## Memos 配置

主要配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `memos_mode` | `external` | `external`=你自己启动 Memos；`managed`=插件托管指定目录里的 `memos.exe`。 |
| `memos_base_url` | `http://127.0.0.1:5230` | 外部 Memos 服务地址。managed 模式启动后会自动使用托管端口。 |
| `memos_token` | 空 | Memos access token。 |
| `memos_timeout` | `30` | 请求超时秒数。 |
| `managed_memos_dir` | `./data/astrbot_plugin_memos_memory/managed_memos` | 托管 Memos 工作目录。 |
| `managed_memos_exe` | `./data/astrbot_plugin_memos_memory/managed_memos/memos.exe` | 托管模式只启动这个明确配置的 exe。 |
| `managed_memos_data_dir` | `./data/astrbot_plugin_memos_memory/managed_memos/data` | 托管 Memos 的数据目录。 |
| `managed_memos_host` | `127.0.0.1` | 托管 Memos 监听地址。 |
| `managed_memos_port` | `0` | `0`=自动找空闲端口，并在后续重启时优先复用上次端口；非 0=使用指定端口。 |
| `managed_memos_port_start` | `5230` | 自动端口查找起点。 |

如果你本地开了多个 Memos，只要端口不同即可，例如：

```text
http://127.0.0.1:8081
http://127.0.0.1:8082
```

插件只会连接你配置的那个地址。

### 托管 Memos 模式

托管模式适合你想把 Memos 和 AstrBot 插件放在同一套目录里，但又不希望插件扫描或误碰本机其他 Memos。

推荐步骤：

1. 在 AstrBot 设置里把 `memos_mode` 改为 `managed`。
2. 把官方 `memos.exe` 放到 `managed_memos_dir`，默认是：

```text
./data/astrbot_plugin_memos_memory/managed_memos/memos.exe
```

3. `managed_memos_port = 0` 时，插件第一次会从 `managed_memos_port_start` 开始找空闲端口，并把端口写入 `managed_state.json`。之后重启插件会优先复用这个端口；如果上次托管进程仍在运行，会直接接入该端口。
4. 第一次启动后，打开 WebUI 概览查看托管 Memos 的实际 URL。
5. 进入该 Memos 页面创建 access token，再填入 `memos_token`。
6. 执行 `/memos-sync` 或 `/memos-reindex` 建立本地索引。

安全边界：

- 插件不会扫描系统里其他 `memos.exe`。
- 插件不会杀掉不是自己启动的 Memos 进程。
- 如果你指定的端口已经被占用，插件会标记为 `port_busy_existing`，不会抢占端口。
- data 目录默认在 AstrBot 目录下，也可以改到其他位置。
- 查看状态可用 `/memos-managed-status`，WebUI 概览也会显示 exe、data、端口和进程状态。

## Embedding 和索引配置

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `emb_provider_id` | 空 | embedding provider ID。留空时尝试自动选择第一个可用 embedding provider。 |
| `vec_db_path` | `./data/astrbot_plugin_memos_memory/memories.db` | 本地 sqlite 索引路径。 |
| `emb_cache_size` | `1000` | embedding LRU 缓存容量。0 表示关闭。 |
| `bm25_tokenizer` | `jieba` | BM25 分词器。`jieba` 更适合中文，缺依赖时会降级。 |

换 embedding 模型后必须运行：

```text
/memos-reindex
```

否则旧向量和新向量维度/语义空间不一致，召回会不准。

## 4.0 情景记忆配置

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `episodic_memory_enable` | `true` | 4.x 原文证据与情景底座总开关。关闭后不删除数据，滚动状态与当前精简主路不可用并回退 3.x。启停后建议重载。 |
| `episodic_db_path` | `./data/astrbot_plugin_memos_memory/episodic_memory.db` | 原始轮次、情景卡和证据链数据库。修改路径后重载。 |
| `evidence_first_generation_enable` | `true` | 双阶段生成开关。关闭后仍写完整日记和情景卡，但只能从日记推导证据。 |
| `raw_evidence_archive_enable` | `true` | 在日记生成前幂等归档当前压缩快照。归档失败时不清缓冲。 |
| `episode_extraction_provider_id` | 空 | 情景提取模型；空表示继承 `compress_provider_id` 或当前会话模型。 |
| `episode_extraction_timeout` | `120` | 情景提取及保底覆盖重试的单次超时。 |
| `diary_render_provider_id` | 空 | 文学日记模型；空表示继承压缩模型。 |
| `diary_render_timeout` | `150` | 日记渲染及完整性重写的单次超时。 |
| `episodic_auto_migrate` | `true` | 启动后后台对接所有已有 Memos。迁移未完成时不启用半成品事件检索。 |
| `episodic_candidate_pool` | `18` | 第一阶段最多保留多少张情景卡。普通目标数会确保候选至少为目标的 3 倍。 |
| `episodic_default_inject` | `5` | 普通话题的计划注入数，不是固定必塞数量；资格线仍会淘汰弱候选。 |
| `episodic_narrative_inject` | `8` | “以前、后来、为什么、完整过程”等叙事问题的计划数。 |
| `episodic_evidence_per_memory` | `3` | 每个展开事件最多选择几项与当前 query 最相关的证据。 |
| `episodic_full_diary_limit` | `1` | 默认优先给几篇完整日记；其余长日记使用相关段落。必要日记仍可得到完整上下文。 |
| `episodic_min_card_score` | `0.40` | 情景卡进入段落精搜的基础门槛；明确词面命中可救回语义分略低的卡。 |

Provider 的继承顺序是：情景/渲染专用 Provider -> `compress_provider_id` -> 当前会话 Provider。情景提取失败或首次日记渲染完全失败时会记录日志并回退原单阶段生成，不会把失败批次从缓冲删除。4.5.2 起，渲染只漏掉部分 Episode 或部分证据时先补写，再按 Episode 合并更完整版本；仍缺失的 Episode 使用已核验证据生成不虚构的保底日记，继续保留 `source_grounded` 原文链。

## 压缩配置

| 参数 | 推荐值 | 说明 |
| --- | --- | --- |
| `enable_auto_compress` | `true` | 是否自动压缩聊天。 |
| `compress_every_n_turns` | `30` | 每多少轮 assistant 回复触发正常压缩。 |
| `diary_count` | `2` | 正常压缩目标日记数。普通压缩仍允许少于目标。 |
| `compress_provider_id` | 空 | 指定压缩 LLM。留空则用当前对话 LLM。 |
| `compress_llm_timeout` | `120` | 日记压缩 LLM 超时。慢模型可调到 180。 |
| `compress_batch_max_messages` | `60` | 单批最多处理的持久消息；超出部分留给下一批。 |
| `eod_checkpoint_enable` | `true` | 启用每天 23:45 的自适应夜间记忆检查点。 |
| `eod_checkpoint_min_turns` | `1` | 至少一个完整轮次即可检查；没有长期价值时仍允许不写日记。 |
| `eod_checkpoint_max_diaries` | `6` | 单批独立情景容量，不是必须凑满的日记数；跨日覆盖可突破。 |

### 23:45 自适应夜间记忆检查点

每天本地时区 23:45 后，插件检查所有仍在 pending buffer 的会话。它仍然承担“没达到正常 30 轮也不丢掉当天点滴”的保底职责，但已经不再使用固定轮数表强制决定日记篇数。

处理顺序：

1. 对当前 buffer 取不可变快照；新消息拥有更高序号，不会被本次清理。
2. 从 1 个完整 assistant 轮次开始检查，并按真实日期数量、相邻有效轮次之间超过 2 小时的停顿、每约 8 轮的内容密度计算“最多可提取情景容量”。
3. 先把快照按内容哈希幂等归档到 `episodic_memory.db`，再让证据模型从原始轮次提取最多 N 个真实独立场景。N 是容量，不是篇数命令；同一完整情感弧可以只生成一篇。
4. 文学渲染必须覆盖每个已核验证据场景，再依次写入 Memos、段落索引和情景入口。跨日 buffer 至少给每个有价值日期一次覆盖机会，整理执行时间不能覆盖事件时间。
5. 全部日记持久化成功后才删除该快照的 pending 消息，并排队融合滚动状态；随后异步刷新内置时间洞察。任一步持久化不完整都会保留 buffer。

同一快照失败后 5 分钟再试，当晚最多 3 次。重试次数绑定快照序号；有新消息进入时会成为新快照，不会被旧快照的失败上限挡住。第一次检查后如果 23:45–23:59 又发生新对话，同一晚仍可以再次处理新增内容。

如果对话没有任何值得长期保留且可回指原文的情景，检查点可以不写日记并保留缓冲，等待后续对话形成完整事件；不会为了“每天必须有一篇”制造空泛记忆。

`2.2.4` 起，如果模型把 `#tag` 写到正文里，插件会把它们移到 tags 字段。

### tag 写入规则

正确结果：

```text
7月16日 · 深夜
我记得他临走前叮嘱我吃饭。
#爱莉 #吃饭 #等待
<!-- memos-memory:importance=4;manual=0;type=emotional_anchor;source=eod -->
```

错误输出如果来自 LLM：

```text
我记得他临走前叮嘱我吃饭。 #爱莉 #吃饭 #等待
```

插件会清洗为：

```text
我记得他临走前叮嘱我吃饭。

#爱莉 #吃饭 #等待
```

流程来源不会作为可见 tag 写入。来源记录在隐藏 metadata：

```text
source=auto
source=eod
```

## 召回配置与实际架构

默认实际运行的是 `lean_recall_enable=true` 的全 MEMORY 融合架构：`一次 query embedding -> 情景卡 + 日记 passage + 原始轮次 + BM25 -> 按需 temporal -> 最多一次 rerank -> 证据覆盖选择 -> 状态 + 融合记忆条目`。其他检索参数并不是同时叠加，而是按以下优先级回退：

1. **4.4 当前主路 `lean_full_memory_fusion`**：情景库就绪且 `lean_recall_enable=true`。
2. **4.0 回退 `episodic_cascade`**：主动关闭精简主路，但情景库仍然可用。
3. **3.x 最后兜底 `legacy_hybrid`**：情景库关闭、迁移未完成或不可用；此时多查询、月份路由和旧后处理参数才可能生效。

主 WebUI 概览会显示当前实际架构；设置页把它们分别放在“4.x 当前精简召回”和“兼容回退”分组。召回实验室中的 `lean/no_source/direct/vector/legacy` 只是消融对照，不会改变真实聊天配置。

| 参数 | 推荐值 | 说明 |
| --- | --- | --- |
| `enable_auto_recall` | `true` | 每次对话前自动召回记忆。 |
| `lean_recall_enable` | `true` | 当前精简主路总开关；推荐保持开启。 |
| `lean_recall_candidate_k` | `50` | 事件卡、日记 passage、原始轮次和 BM25 的宽候选池。 |
| `lean_event_index_enable` | `true` | 事件级召回；与 passage 检索复用一个 query embedding。 |
| `lean_source_evidence_enable` | `true` | 原始对话轮次召回；同样复用 query embedding，可独立救回遗漏细节。 |
| `lean_coverage_selection_enable` | `true` | 按日期、人物、关系阶段、承诺和状态变化选择互补证据。 |
| `lean_adaptive_evidence_enable` | `true` | 普通 RP 不展开重复证据；精确取证问题最多展开两组。 |
| `passage_vector_auto_migrate` | `true` | 升级后后台迁移旧 passage；关闭后可用 `/memos-passage-rebuild` 手动完成。 |
| `source_turn_vector_auto_migrate` | `true` | 升级后后台为已有原文档案建索引；不修改原始轮次或 Memos。 |
| `lean_temporal_enable` | `true` | 只有明确日期/月份/去年/昨天等时间问题才增加结构化时间候选。 |
| `lean_story_min_inject` | `1` | 有合格结果时的基础剧情数。 |
| `lean_story_max_inject` | `6` | 普通问题默认目标 3 条，叙事和明确时间问题最多 6 条；总量由 `inject_char_budget` 治理。 |
| `min_similarity_to_inject` | `0.52` | 相似度硬门槛。 |
| `recall_dedup_window` | `6` | 最近 N 轮出现过的 memo 轻微降权；没有同样强的替代、或用户继续追问时仍允许再次注入。 |
| `recall_context_query_messages` | `3` | query 最多拼接的最近对话消息数；0 表示只用当前输入。 |
| `recall_context_query_max_chars` | `900` | query 上下文线索总字符上限。 |
| `passage_index_enable` | `true` | 对完整 Memos 日记建立可追溯本地段落索引。 |
| `passage_max_chars` | `280` | passage 目标长度，只影响检索粒度，不截断 Memos 正文。 |
| `passage_overlap_chars` | `60` | 相邻 passage 的句界上下文重叠。 |
| `mixed_injection_enable` | `true` | 允许核心日记全文、辅助长日记局部段落混合注入。 |
| `full_diary_top_n` | `2` | 至少完整注入排名最前的核心日记数。 |
| `passage_expand_chars` | `100` | 命中段落前后扩展量，并尽量扩到完整句界。 |
| `recall_rerank_enable` | `true` | 启用最终注入重排。配置了原生 rerank Provider 时先做模型重排。 |
| `recall_injection_min_score` | `0.62` | 注入资格线，避免为了凑数量注入弱相关记忆。 |

`recall_top_k`、`persona_top_k`、`plot_top_k`、`texture_top_k`、`recall_candidate_pool`、`recall_multi_query_enable`、`recall_rrf_k`、月份路由、旧信息增益和旧动态数量参数都保留在“兼容回退”分组。精简主路开启时，它们不会改变真实聊天的候选路线或注入名额。

### 兼容回退参数（默认不运行）

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `recall_dynamic_count_enable` | `true` | 启用动态注入数量。 |
| `recall_min_inject` | `1` | 动态注入最少条数；无合格候选仍不会硬凑。 |
| `recall_max_inject` | `9` | 动态注入最多条数。 |
| `recall_cluster_fold_enable` | `true` | 启用相似簇折叠分析。 |
| `recall_cluster_fold_apply` | `false` | 是否把相似簇折叠应用到真实注入；默认只在实验室观察。 |
| `recall_cluster_similarity` | `0.88` | 相似簇阈值。 |
| `recall_cluster_base_per_group` | `2` | 每个相似簇默认保留篇数。 |
| `recall_cluster_allow_protected` | `true` | 手动、锚点、高反馈、importance 5、强触发命中可突破簇限制。 |

排序权重：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `w_relevance` | `0.72` | 语义相关度权重。 |
| `w_importance` | `0.23` | 长期重要性权重。 |
| `w_recency` | `0.03` | 新近度轻微加权。 |
| `pin_boost` | `0.18` | 手动钉记忆加权。 |
| `time_boost` | `0.07` | 只在纪念日、周年、去年今天等明确意图下启用同月日加权。 |

`min_similarity_to_inject` 是硬门槛。重要性高不会让完全不相关的记忆强行注入。

在兼容回退路线中，`recall_top_k` 不等于“固定注入 6 篇”。插件会先取 `recall_candidate_pool` 候选，再根据触发线索、长期影响、标签、反馈和近期重复惩罚计算 `injection_score`。最终注入数量由合格候选决定，弱相关时可能只注入 1-3 篇，强相关时最多到 `recall_max_inject`。

相似簇来自预计算的 `memo_similarity_clusters` / `memo_similarity_edges`。它不是删除或内容去重。2.4.0 开启 `recall_information_gain_enable` 时，相似簇只保留为可视化和诊断信息，不能在必要性判断前隐藏任何日记；同簇里若有两篇分别提供承诺和后续关系变化，两篇都可以注入。真正减少候选的是“当前问题是否需要它”和“它是否增加新证据”。

### 4.0/3.x 回退中的必要日记与信息增益

最终选择分两步：

1. **必要项锁定**：明确日期范围、直接人物/物件、关系/情绪问题和事件前后阶段先找可回答证据。明确问“2026 年 7 月 3 日都发生了什么”时，同日多篇不同事件可一起突破普通动态数量。
2. **边际补充**：剩余位置按综合相关性、尚未覆盖的查询线索、不同状态变化和命中 passage 数量选择。只有事件时间、场景锚点和状态变化都相同的候选才受到轻微冗余惩罚。

这不是日记去重，也不会合并、删除或替换相似日记。`recall_necessary_hard_cap` 只防止异常候选爆炸，不是日常 top-k。

### 是否推荐时间洞察

推荐开启，4.4.0 默认也是开启状态：`enable_time_insight_affiliate=true`。这个旧键名为兼容已有配置而保留，当前实际控制的是主插件内置时间洞察。

- 每轮只读本地 SQLite，不调用 LLM，不占普通剧情召回名额。
- 环境模式默认最多一条，只接受精确纪念日或强近期趋势，并有 180 分钟重复冷却。
- 邻近纪念日和季节规律只有问题明确相关时才补充，避免把泛化模式硬塞进每轮。
- LLM 精炼只在周期、手动更新或夜间新记忆提交后的异步刷新中运行；失败时保留确定性结果。

旧 `astrbot_plugin_memos_memory_insight` 仍在运行时，内置引擎会自动让出。要使用主插件的完整时间洞察和心潮工作台设置，建议停用旧附属。

### 全文与段落注入

默认使用完整日记的情况：

- 双粒度主路排名第一的核心记忆（由 `episodic_full_diary_limit` 控制）。
- 日记本身较短。
- 同一日记命中多个不同 passage。
- 必要记忆涉及关系变化、情绪锚点、承诺规则、行为变化或 `state_change`，且完整上下文确有必要。

排名靠后的长日记默认注入扩展后的相关 passage；叙事和时间问题可以同时选择多篇不同日期/阶段的 passage，而不是把多篇全文一起塞入。段落块始终带来源 memo、事件日期、时间依据和“这是旧日记局部，不是刚刚发生”的边界。Memos 中的完整日记从不被截断或替换。

## 记忆类型

插件使用这些 `memory_type`：

| 类型 | 说明 |
| --- | --- |
| `plot_fact` | 剧情事实、身份、地点、物品状态、事件结果。 |
| `relationship_shift` | 关系位置变化，更亲近、更信任、更警惕、更依赖。 |
| `emotional_anchor` | 强烈情绪锚点，害怕分离、被记住、被安抚等。 |
| `behavior_bias` | 之后更可能表现出的反应方式。 |
| `promise_or_rule` | 承诺、边界、禁忌、称呼规则。 |
| `daily_texture` | 日常氛围和生活质感。 |

分层召回会根据类型把记忆放入 persona、plot、texture 层。

## 长期人格画像

开启：

```text
enable_affiliate_profile = true
```

默认不再依赖单独 affiliate 插件。主插件会在本地 sqlite 中维护：

- 当前画像。
- 稳定事实。
- 历史画像版本。
- 更新记录。

画像更新读取：

- 当前旧画像。
- 最近人格类记忆。
- importance 4-5 的高重要度长期记忆。
- 手动钉记忆。
- 得到“有帮助/关键记忆”反馈的记忆。

关键配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `profile_auto_update_days` | `14` | 自动更新周期。0 表示关闭。 |
| `profile_recent_persona_limit` | `120` | 最近人格类记忆读取数量。 |
| `profile_anchor_limit` | `40` | 高重要度长期记忆读取数量；保留旧配置键名兼容升级。 |
| `profile_manual_limit` | `30` | 手动钉记忆读取数量。 |
| `profile_feedback_limit` | `30` | 高反馈记忆读取数量。 |
| `profile_llm_timeout` | `90` | 画像 LLM 调用超时秒数。慢模型或大画像可调到 `120-180`。 |
| `profile_target_chars` | `2600` | 画像目标字数，不是硬截断。 |
| `profile_facts_target_count` | `40` | 稳定事实锚点目标条数。 |
| `affiliate_profile_max_age_days` | `45` | 画像最大可用天数。 |

手动更新：

```text
/memos-profile-update
```

查看状态：

```text
/memos-profile-status
```

## 内置 RP enhancer

配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `rp_enhancer_enable` | `true` | 总开关。 |
| `rp_time_enable` | `true` | 注入现实时间和对话节奏。 |
| `rp_time_strip_default` | `true` | 劫持 AstrBot 默认简单时间注入。 |
| `rp_time_timezone` | `Asia/Shanghai` | 时区。 |
| `rp_time_show_lunar` | `false` | 是否显示农历。默认关闭，避免阴历/阳历混乱。 |
| `rp_time_show_festival` | `true` | 是否显示节日/节气。 |
| `rp_time_show_rhythm` | `true` | 是否显示今日轮次和上次对话间隔。 |
| `rp_repetition_enable` | `true` | 复读提醒。 |
| `rp_mirror_enable` | `true` | 镜像动作提醒。 |
| `rp_inject_max_chars` | `1400` | enhancer 总注入预算。 |

现实时间使用 `CurrentTimeContext priority="critical" role="only_current_now"` 单独临时注入，第一行是完整公历日期和分钟级时间。它是本轮唯一“现在”。`3.2.1` 起，时间块与心潮身体节律共用同一个请求级 UTC 快照，再统一转换到各自配置的时区；同一轮不会因为两个模块先后取时而跨日错位。

每篇召回日记使用独立 `HistoricalMemory`，包含：

- `occurred_at`：事件发生日期，不是 memo 创建日期。
- `time_basis`：明确日期、本次对话推定、来源日期补全或未知。
- 与本轮当前时间的大致距离。
- 明确规则：即使旧事件和今天同月同日，也不等于本轮刚刚发生。

长期画像会包装为 `LongTermCharacterState`；内置时间洞察使用 `IntegratedHistoricalTimeInsight`，只表达带日期证据的历史回声。两者都不能提供或覆盖当前现实日期。

如果你仍安装旧 `astrbot_plugin_rp_enhancer`，建议停用旧插件，避免重复注入时间和复读提示。

## Provider 缓存与前缀诊断

可用配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `cache_prefix_drift_enable` | `true` | 记录相邻请求的 system/history hash、字数、轮数和变化来源。 |
| `cache_friendly_system_guard_enable` | `true` | 把误入 `system_prompt` 的本插件动态时间或历史记忆块搬回临时 extra 注入。 |

Console 的 `Provider Cache` 来自模型服务真实返回的 cached token 字段；Provider 不回报时，插件不会猜测命中率。`Prefix drift` 用于判断不稳定更可能来自 system 变化还是 Astr 原始上下文变化。

缓存友好结构：

```text
system_prompt = 固定角色设定 / 固定平台规则 / Astr 自己管理的知识库内容
extra_user_content = 当前时间 + 召回日记 + 画像 + 心潮身体状态
contexts = 最近 N 条原始对话
```

`3.2.1` 的 system guard 只识别 `CurrentTimeContext` 与 `HistoricalMemory`。它不会识别或搬运 Astr 知识库 marker，也不会生成 `CharacterKnowledgeReference`。

## 上下文治理

请求级治理配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `context_governance_enable` | `true` | 启用请求级上下文治理。 |
| `context_exclude_command_turns` | `true` | 把 AstrBot 已识别的指令及其回复视为控制回合：不进入记忆缓冲与心潮，发送后从持久上下文精确剔除，并有下一请求并发兜底。 |
| `context_keep_recent_messages` | `40` | 当次请求最多保留最近多少条原始消息。 |
| `context_min_messages_before_trim` | `80` | 原始历史达到多少条后开始裁剪。 |
| `context_preserve_system_messages` | `true` | 裁剪时保留 system/developer 消息。 |
| `context_trim_backup_enable` | `true` | 裁剪前强制本地 JSON 备份；备份失败则本轮不裁剪。 |

判断是否生效，看日志：

```text
[memos-memory][system] context request trim: 1765->80
```

如果看到类似日志，说明请求级治理已经生效。

如果 total estimate 仍然很大，看注入构成：

- `context` 大：继续降低 `context_keep_recent_messages`。
- `diary/memory` 大：降低分层 top_k 或改用 `inject_format=summary`。
- `xinchao` 大：降低心潮注入上限或改用更克制的身体呈现模式。
- `profile` 大：降低 `profile_target_chars`。

持久归档配置：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `context_archive_enable` | `false` | 是否启用周期性持久归档。 |
| `context_archive_interval_days` | `7` | 周期归档间隔。 |
| `context_archive_min_total_messages` | `240` | 总历史达到多少条才允许归档。 |
| `context_archive_keep_recent_messages` | `120` | 归档后保留最近多少条。 |
| `context_archive_backup_dir` | `./data/astrbot_plugin_memos_memory/context_backups` | 备份目录。 |

建议先手动：

```text
/memos-context-status
/memos-context-preview
/memos-context-archive exec
```

确认备份和效果都正常后，再考虑打开周期归档。

## WebUI 和 Console

默认：

```text
webui_enable = true
webui_host = 127.0.0.1
webui_port = 8088
```

访问：

```text
http://127.0.0.1:8088/
http://127.0.0.1:8088/console
```

WebUI 页面包括：

- Overview：三层记忆状态、当前 4.x 请求路径、连接状态、注入构成和最近记忆日。
- Memories：本地索引中的记忆列表。
- Evidence：原文档案、情景证据、滚动状态及其历史版本。
- Recall Lab：模拟当前 query 的 4.4 全 MEMORY 融合召回、证据覆盖选择与最终注入，并维护真实评测集和运行消融。
- Timeline：月份日历、单日原始日记与月份索引线索，仅作档案浏览，不占主召回配额。
- Feedback：真实注入记录、查询相关反馈和撤销操作。
- Profile：当前画像、稳定事实、历史画像版本。
- Context：当前 AstrBot 上下文和归档备份浏览。
- Health：记忆健康检查。
- Activity：插件事件日志。
- Settings：AstrBot 插件设置、场景预设和生效状态。
- Debug：API 调试。

Console 更适合运行时观察：

- 最近注入占比。
- 最近注入样本表。
- 各类日志计数。
- `xinchao`、`recall`、`inject`、`cache`、`enhancer`、`compress`、`sync`、`system` 事件流。

### 心潮工作台

打开 `http://<webui_host>:<webui_port>/xinchao`。这是独立于主 WebUI 和 Console 的大页面，心潮设置不会混入主插件设置。

- 状态总览：意识节律、意向、疲劳、revision、十二维驱动力和真实注入预览。
- 身体节律：当前阶段与周期位置、六维身体信号、阶段主体感受、时段修正、显著信号、真实身体注入和全部身体参数。
- 心理轨迹：短互动摘要、梦境/余韵、感知来源和运行事件。
- 模拟实验：预览若干小时后的结算，以及一次满足、情绪变化或闪念会怎样改变注入；模拟不写入状态。
- 人工反馈：明确增减一个驱动力，或手动加入一条闪念。
- 心潮设置：核心、感知、注入、梦境、主动表达和诊断分组配置。

心潮与身体节律设置单独保存在向量数据库同目录下的 `xinchao_settings.json`，持久心理状态保存在 `xinchao_state.json`。身体状态按日期实时计算，不另建长期状态文件。升级插件不会删除已有设置或状态；旧设置缺少身体字段时会自动补默认值，锚点默认留空，因此升级后不会突然改变角色表现。设置页不会读取或修改 Memos Token、角色名、主插件 Provider、路径和端口。

感知、梦境和主动消息模型使用 AstrBot `get_all_providers()` 提供的已启用 LLM 列表，在心潮设置中直接下拉选择。选择“跟随当前会话模型”会保留空 Provider ID：感知使用当前会话模型，梦境和主动消息使用角色最后一次会话对应的模型。旧设置中的 Provider ID 如果当前已停用，页面会保留并标注“当前不可用”，不会悄悄替换。

身体节律启用步骤：

1. 打开 `/xinchao`，进入“身体节律”。
2. 填写最近一次周期首日，例如 `2026-07-01`。
3. 选择身体呈现模式，并按角色需要调整周期、影响强度和四类个体倍率。
4. 保存后立即生效，不需要重建日记、重新同步或重启插件。

三档身体呈现模式：

| 模式 | 注入内容 | 适用场景 |
| --- | --- | --- |
| `significant` | 只在信号跨过阈值时注入，最多三条 | 最克制、接近 3.1.1 行为 |
| `balanced` | 阶段主体感受 + 当前时段修正 + 最多两条显著信号 | 默认，连续感与角色稳定性平衡 |
| `immersive` | 阶段主体感受 + 当前时段修正 + 最多三条显著信号 | 身体表现优先、上下文预算充足 |

时段按心潮时区划分：`05:00-11:59` 早晨、`12:00-17:59` 午后、`18:00-22:59` 傍晚、`23:00-04:59` 深夜。它们不是统一的精力加减，而是与当前阶段组合。例如低负荷阶段早晨更容易察觉沉重与局部不适，午后会体现适应后的困倦；恢复阶段午后更连贯；活跃阶段傍晚仍保留一些余量；缓降阶段深夜更容易积累疲劳和感官负担。

推荐保留默认 `body_expression_mode = balanced`、`body_effect_strength = 0.72`、`body_daily_variation = 0.22` 和 `body_closeness_scale = 0.65`。同一角色在不同会话中使用一致的当日身体底色；`body_effect_strength = 0`、锚点留空或关闭 `body_rhythm_enable` 时，身体层完全跳过。旧版设置文件会自动补入 `balanced`，不会删除任何已有字段或状态。

### WebUI 设置中心

Settings 页直接读取插件 `_conf_schema.json`，当前覆盖全部主插件设置。它和 AstrBot 插件管理页操作的是同一份配置，不会维护第二套配置副本。页面提供：

- 按连接、压缩、召回、段落、画像、上下文、角色增强、KB、维护和重要性规则分类。
- 按参数名、中文说明和提示全文搜索。
- 布尔开关、数值输入、枚举菜单和文本输入等类型化控件。
- 单项恢复默认值、待保存计数和保存后的生效状态提示。
- Token 等敏感字段只显示“已配置/未配置”，后端不会把明文发送给浏览器。
- 敏感输入留空表示保留原值；只有点击“清除”并确认才会清空。

多数纯运行参数会在保存后立即应用。以下设置涉及已经初始化的对象、后台任务或监听端口，保存后需要在 AstrBot 中重载插件：

- Memos 地址、模式、Token、超时和全部托管 Memos 设置。
- Embedding、rerank、画像 Provider 和向量数据库路径。
- WebUI 开关、地址、端口。
- 时区、自动对账、画像周期和上下文周期归档间隔。

保存先执行类型、范围和未知字段校验，再调用 AstrBot 的 `save_config()` 持久化。持久化失败时会回滚本次内存修改。WebUI 写接口还会核对浏览器 `Origin` 与当前服务地址，降低其他网页误操作本地配置的风险。

### 一键场景预设

预设只修改参数类设置，不会改动 Memos 连接、Token、Provider、路径、WebUI/托管端口或数据目录。应用前会列出差异预览；之后仍可逐项微调。

| 预设 | 压缩轮数 | 候选池 | 普通目标 / 长线上限 | rerank | 适合场景 |
| --- | ---: | ---: | ---: | --- | --- |
| 长线灵魂塑形 | 30 | 80 | 4 / 10 | 开 | 年月级 RP、人格收敛、情感弧与跨阶段剧情覆盖最大化 |
| 全维效果优先（推荐） | 30 | 70 | 4 / 9 | 开 | 自动兼顾日常、精确取证和长线叙事，效果与延迟仍可控 |
| 均衡推荐 | 30 | 50 | 3 / 7 | 开 | 保留全部核心机制，适合作为长期日常默认 |
| 低成本长记忆 | 45 | 40 | 2 / 5 | 关 | 保留原文与证据质量，通过降低频率和 LLM 精炼节省成本 |

这里的压缩轮数不是“越小越好”。4.6 有原文档案和场景切分，效果档恢复到 30 轮，避免过于频繁地写日记和融合滚动状态；长批次里的独立场景由证据提取器拆分，不靠降低轮数硬切。低成本档提高到 45 轮，并同步扩大单批消息容量。

### 召回实验室四档

召回实验室预设只修改当前 4.x 检索与选择参数，不改变写日记、Token、角色、Provider、路径或服务配置：

| 预设 | 候选池 | 普通目标 / 叙事上限 | 特点 |
| --- | ---: | ---: | --- |
| 全场景最优（推荐） | 80 | 4 / 10 | 普通问题保持正文密度；叙事意图自动扩展到 10 条并放宽跨日期覆盖，本身已经兼顾长线。 |
| 精确取证 | 60 | 3 / 6 | 更高资格线，优先日期、原话、承诺、边界与专名的一手证据。 |
| 长线宽召回（专项） | 100 | 4 / 10 | 与综合最优使用相同长线上限，但进一步扩大候选与跨阶段分差，适合集中回顾跨月剧情，噪声和延迟略高。 |
| 日常均衡 | 50 | 3 / 7 | 保留日记、Episode、原始轮次、BM25、rerank 与安全网，使用默认级开销。 |

“全场景最优”和“长线宽召回”不是能力有无的区别。两者都会按问题意图动态选择数量；专项档只是更愿意把分数稍低但来自不同日期、人物或关系阶段的候选带进最终覆盖。

## 命令列表

### 同步和索引

```text
/memos-sync
/memos-reindex
/memos-passage-rebuild
/memos-buffer-status
```

- `/memos-sync`: 从 Memos 同步到本地索引，并清理已删除 memo。
- `/memos-reindex`: 全量重建兼容索引、段落和 4.0 情景卡。
- `/memos-episodic-rebuild`: 只重建 4.0 情景卡与旧日记推导证据；不改 Memos 正文。
- `/memos-passage-rebuild`: 为全部旧日记无损重建可追溯 passage；不改写 Memos，不调用分类 LLM。
- `/memos-buffer-status`: 查看当前未压缩缓冲。

### 记忆操作

```text
/memos-search <关键词>
/memos-remember <内容> [#标签]
/memos-retag
```

- `/memos-search`: 手动检索测试。
- `/memos-remember`: 手动钉一条高优先级记忆。
- `/memos-retag`: 用 LLM 对旧记忆重分类。

### 画像

```text
/memos-profile-update
/memos-profile-status
```

### 上下文治理

```text
/memos-context-status
/memos-context-preview
/memos-context-archive exec
```

### 心潮

```text
/memos-mind-status
/memos-mind-settle
/memos-mind-feedback <drive> <delta>
/memos-mind-thought <drive> <0.1-1.0> <念头>
/memos-mind-reset CONFIRM
```

- `/memos-mind-status`: 查看当前意识、主要倾向、意向和最近感知来源。
- `/memos-mind-settle`: 立即执行一次时间结算，不触发主动消息。
- `/memos-mind-feedback`: 人工增减一个驱动力，`delta` 范围会限制在 `-0.5` 到 `0.5`。
- `/memos-mind-thought`: 加入一条闪念，之后会按状态机规则衰减或形成执念。
- `/memos-mind-reset CONFIRM`: 只重置当前心潮状态，不删除日记、画像、查询反馈或 AstrBot 历史。

### 时间洞察

```text
/memos-insight-update
/memos-insight-preview
/memos-insight-status
```

- `/memos-insight-update`: 立即从本地日记索引重建 v3 候选；可能在后台重建阶段调用所选 LLM 精炼，聊天请求阶段不会调用。
- `/memos-insight-preview`: 无写入生成候选预览，用于调阈值。
- `/memos-insight-status`: 查看候选、环境选中数、最近运行和旧附属兼容状态。

同一组设置位于心潮工作台的“时间洞察”子页。旧 `astrbot_plugin_memos_memory_insight` 仍在运行时，内置引擎会自动让出注入和后台写入，避免同一轮出现两份洞察；要使用新页面的完整内置链路，建议停用旧附属插件。

### 相似簇与健康

```text
/memos-graph-rebuild
/memos-cluster-rebuild
/memos-health
```

召回反馈只在 WebUI 的“真实注入记录”和“召回实验室”操作，不提供聊天命令。月份索引自动维护，也不需要生成摘要或手动重建命令。

## 推荐参数

通用长期 RP 推荐：

| 参数 | 推荐值 |
| --- | --- |
| `compress_every_n_turns` | `30` |
| `diary_count` | `2` |
| `evidence_first_generation_enable` | `true` |
| `raw_evidence_archive_enable` | `true` |
| `episodic_candidate_pool` | `18` |
| `episodic_default_inject` | `5` |
| `episodic_narrative_inject` | `8` |
| `episodic_evidence_per_memory` | `3` |
| `episodic_full_diary_limit` | `1` |
| `episodic_min_card_score` | `0.40` |
| `recall_top_k` | `6` |
| `persona_top_k` | `3` |
| `plot_top_k` | `2` |
| `texture_top_k` | `1` |
| `min_similarity_to_inject` | `0.52` |
| `w_relevance` | `0.72` |
| `w_importance` | `0.23` |
| `w_recency` | `0.03` |
| `pin_boost` | `0.18` |
| `profile_auto_update_days` | `14` |
| `recall_multi_query_enable` | `true` |
| `recall_month_route_enable` | `true` |
| `recall_month_route_count` | `2` |
| `recall_month_route_candidate_k` | `8` |
| `recall_month_route_inject_max` | `2` |
| `recall_month_route_timeout` | `6` |
| `recall_information_gain_enable` | `true` |
| `recall_necessary_can_exceed_max` | `true` |
| `recall_necessary_hard_cap` | `14` |
| `passage_index_enable` | `true` |
| `passage_max_chars` | `280` |
| `passage_overlap_chars` | `60` |
| `mixed_injection_enable` | `true` |
| `full_diary_top_n` | `2` |
| `passage_expand_chars` | `100` |
| `context_keep_recent_messages` | `40` |
| `context_min_messages_before_trim` | `80` |
| `context_archive_keep_recent_messages` | `120` |

如果输入规模仍然偏大：

| 大头 | 调整 |
| --- | --- |
| `context` | `context_keep_recent_messages` 改为 `30`。 |
| `diary/memory` | 降低 `persona_top_k/plot_top_k/texture_top_k`，或 `inject_format=summary`。 |
| `profile` | 降低 `profile_target_chars`。 |
| `enhancer` | 降低 `rp_inject_max_chars`。 |

## 日志解读

### 压缩

```text
[memos-memory][compress] 开始压缩 34条 -> 2篇
[memos-memory][compress] 压缩完成: 2篇日记
```

如果是保底压缩：

```text
[memos-memory][compress] 保底压缩: 17轮 -> 2篇日记
```

`2.2.3` 起，如果第一次只解析出 1 篇，会重试一次。

### 召回

```text
[memos-memory][recall] query: ...
{"query_len":1628,"context_query_parts":4,"contexts_total":80}
```

说明最近 3 条上下文参与了检索 query；具体条数和总字数分别由 `recall_context_query_messages`、`recall_context_query_max_chars` 控制。

```text
[memos-memory][recall] after dedup: 65 candidates
```

表示按 `memo_name` 去重后还有 65 篇不同候选。它不是最终注入数量。

### 注入

```text
[memos-memory][inject] 注入 6条 diary=4647字 memory=7470字 profile=2821 current_time=230 time=0 xinchao=520 enhancer=434 context=32794
```

这条最适合判断输入规模哪里过大。

### Provider cache

```text
[memos-memory][cache] provider cache tokens 23296/37433 (62.2%)
```

这只表示 Provider 回报的请求缓存情况，不代表插件处理过 Astr 知识库。

### 上下文治理

```text
[memos-memory][system] context request trim: 1765->80
```

说明请求级裁剪有效。

## 常见问题

### `/memos-sync` 后 Astr 卡住

首次同步大量 Memos 时需要 embedding、写 sqlite、建索引，可能会慢。完成后再执行 `/memos-reindex` 可以让索引更干净。

### 为什么 `after dedup` 很大，但最终只注入几条？

`after dedup` 是候选池，不是注入数。后面还要经过资格线、证据覆盖选择、去重窗口、软字符目标和注入格式处理。

### 为什么保底压缩说 2 篇，最后仍可能少于 2 篇？

`2.2.3` 起会强提示并重试一次，但如果 LLM 判断信息不足，或输出仍不合格，最终仍可能少于目标。日志会记录“保底压缩少于目标”。

### 为什么保底压缩曾经每分钟重复显示 `3轮 -> 1篇`？

旧逻辑在定时层允许3轮触发，但底层仍要求普通压缩的至少8条消息；3个完整轮次通常只有6条，因此会在调用 LLM 前返回0，一分钟后再次触发。`3.2.5` 已让 `eod` 使用独立的3轮门槛，并加入5分钟失败退避和当晚3次上限。正常轮数自动压缩仍保留原消息门槛。

### 为什么 Memos 页面里旧日记正文还有 `#tag`？

`2.2.4` 只保证新压缩结果清洗。旧日记如果已经把 tag 写进正文，插件不会自动改写 Memos 内容。你可以手动编辑旧 memo，或重新生成/迁移。

### 为什么 WebUI 显示的数据和 Memos 页面不完全一致？

WebUI 主要读本地 sqlite 索引。Memos 是真相源。手动改了 Memos 后，需要 `/memos-sync` 同步。

### 为什么换 embedding 后召回变差？

需要 `/memos-reindex`。旧向量不能和新模型混用。

### 为什么 total estimate 还是很高？

看注入构成。通常最大头是 `context`。先检查是否已经触发上下文治理，再按占比分别调整日记、画像、心潮或 enhancer 参数。

### 可以同时使用旧 `rp_enhancer` 或旧 `kb_cache_optimizer` 吗？

旧 `rp_enhancer` 不建议同时启用，因为会重复注入时间、复读或镜像提醒。`3.2.1` 已移除内置 KB optimizer；是否使用独立 KB 插件由你决定，但本插件不会再与它联动或修改它的知识库内容。

## 数据边界

Memos 中的数据是“哪些日记当前有效”以及“完整可阅读正文”的真相源。

`memories.db` 的段落和检索派生索引可以重建：

```text
/memos-reindex
```

`episodic_memory.db` 中的情景卡可从 Memos 重建，但已有日记只能恢复为 `diary_derived`；新记忆的原始聊天轮次、用户沉淀的召回评测集和完整状态版本历史不能从文学日记反推。因此该文件需要随 Memos 一起备份。

请求级上下文治理和持久归档都可能让缩短后的历史被 AstrBot 保存回会话；4.5.0 起两者都会先生成本地 JSON 备份（请求级裁剪备份失败时直接跳过裁剪）。

插件不会自动删除 Memos 中的记忆；删除记忆应在 Memos 中操作，再由 `/memos-sync` 或后台对账同步到本地索引。

## 升级记录

### 4.6.2

统一 WebUI 的 LLM Provider 选择。主设置页现在识别 AstrBot schema 的 `select_provider` 标记，压缩、查询规划、情景提取、文学日记渲染、滚动状态和历史画像模型均改为当前已启用 Chat Completion Provider 的下拉列表；心潮即时评估、后台结算、梦境、主动消息和时间洞察审校继续使用同一来源。空值保持自动跟随会话模型，旧 Provider 暂时不可用时仍保留当前值。Embedding 与 rerank 仍按要求手填。

全局四档和召回实验室四档新增统一后端保护。即使以后误把手填字段加入预设定义，应用预设时也会自动排除所有 `*_provider_id`、Token/密钥、角色名、Memos 模式与地址、主机、端口、路径、目录、可执行文件、时区和 Tier 词表。预设只调整数值、开关及档位自身的模式参数，不会覆盖用户连接与身份配置。

本版没有数据库 schema 变化，不改 Memos、日记、原文档案、Episode、滚动状态、时间、心潮或检索注入算法；覆盖并重载即可，无需执行同步或重建命令。

### 4.6.1

重写主 WebUI 的四档全局预设。四档统一使用 4.6 安全底座，不再混入已经退出主路的旧复合检索参数：原文完整归档、证据优先生成、文学日记校验、滚动状态、全 MEMORY 融合召回、时间边界、上下文治理和 14 天数据备份保持开启。长线灵魂塑形、全维效果优先、均衡推荐和低成本长记忆只在压缩/状态融合频率、候选宽度、动态剧情上限、全文与证据展开、时间洞察 LLM 精炼和上下文规模上区分。低成本档不再关闭证据优先生成，避免为省一次调用永久失去一手证据结构。预设仍不修改 Token、角色名、Provider、URL、端口或数据路径。

召回实验室四档改为全场景最优、精确取证、长线宽召回和日常均衡。全场景最优本身已经兼顾长线：普通问题目标 4 条，识别到叙事意图后自动使用 10 条上限和更宽的跨日期覆盖；长线专项使用同样上限，只进一步扩大候选池和叙事分差容忍度。原文按需展开控制正式接入精简主路：普通 RP 仅为承诺、边界、时间约束或未解决事项展开最多 1 个记忆组，日期、原话、争议和精确细节问题最多展开 2 组；每组内部由 `episodic_evidence_per_memory` 控制证据条数。原始轮次仍独立参与候选救回，未展开原文的入选记忆仍保留事件核心和日记/段落。

本版没有数据库 schema 变化，不改 Memos、日记正文、原始轮次、Episode、滚动状态、心潮状态或时间记录，升级后无需执行命令。

### 4.6.0

主 WebUI 左侧新增 `13 备份管理`，完整接入插件本地快照服务：可查看自动策略与备份清单，逐份校验 ZIP、SHA-256 和 SQLite 完整性，查看数据库业务表数量，并可下载或删除未受保护的备份。概览里的“立即备份”仍作为快捷入口；上下文页的对话裁剪归档使用独立标识，不再与插件数据快照混用同一个前端元素。

新增冷启动安全恢复流程。选择备份后先创建当前数据安全快照，恢复只预约到下次插件重载，并在数据库和心潮服务初始化之前单次执行；目标备份与安全备份在预约期间均不会被轮换清理。文件摘要变化、ZIP 异常、SQLite 损坏或替换失败时保持 fail-open，并回滚已替换文件。恢复不覆盖配置、Token、Memos 服务数据库、运行遥测或数据路径。没有数据库 schema 变更，不改现有日记、召回、注入、时间、心潮、身体节律、画像与上下文算法，升级后无需执行同步或重建命令。

正式版进一步重构心潮双通道协作。回答前即时评估先取得本轮唯一现实时间与身体节律快照，并读取 4.6 滚动人格状态；回答后结算同时读取这份暂定判断和模型实际回复，对每项激活标记“确认、已消解或衰减延续”，避免两路重复累计。后台模型仍独立超时、熔断和合并队列，不阻塞回复；结构化输出验收会拒绝只有一项感知字段的残缺 JSON，并在总超时内重试或回退规则。心潮 WebUI 显示两路对齐结果。

内置时间洞察升级到 v4。候选生成会把 `memories.db` 的日期日记与 `episodic_memory.db` 的 Episode 证据质量、原文轮次链接和 grounded 证据合并；可回原文只增加证据可信权重，不修改历史日期，也不降低旧日记资格。后台 LLM 审校可读取滚动状态作为解释背景，但所有 claim 仍必须绑定候选 evidence ID。每轮使用与 `CurrentTimeContext`、身体节律完全相同的请求时间快照，并明确区分当前现实时间、历史日记发生时间和当前节律时间。时间洞察页面增加原文可回数量与真实运行诊断。

正式版不改 Memos 正文、4.6 原文档案、Episode、滚动状态、心潮状态或普通召回预算。升级无需执行命令；时间洞察引擎版本变化会让旧候选自动失效并在后台重新生成，属于可重建派生数据。

### 4.6.0-test7

新增默认 14 天一次的插件本地数据备份，使用 SQLite 在线快照而非直接复制正在写入的数据库；ZIP 在写入后校验结构和清单，再以原子替换落盘，默认轮换保留 6 份。备份不读取或保存 AstrBot 配置、Memos Token 与 Memos 服务数据库，失败保持 fail-open。WebUI 设置页增加备份开关、周期、保留数与目录，概览连接状态显示最近备份，并提供真实可用的“立即备份”操作。

主页“重要性分布”替换为可切换的记忆结构图：记忆类型来自 `memories.db`，证据来源与可回原文统计来自 `episodic_memory.db`。旧重要性字段和 `/api/stats.importance_dist` 继续保留，既有日记、检索排序及兼容调用不受影响。本版没有修改当前时间、日记事件时间、时间洞察、身体节律、心潮、日记生成、召回和注入算法；升级后无需同步或重建。

### 4.6.0-test6

滚动当前状态改为效果优先的自适应节奏。日记、Episode、原文档案和检索索引仍在本批成功后立即持久化；只有“当前状态完整文档”的 LLM 重写可以延后。首次状态、失败重试、明确承诺/边界/身份/关系转折立即融合，普通变化默认累计 3 个成功压缩批次，最长等待 72 小时，单次最多顺序合并 6 批。真实数据复制件校准发现旧硬触发会把普通“约定买菜/学字”误判为重大承诺，因此新增“记忆类型 + 触发语义”双门槛；10 篇新架构样本的立即触发由 8 篇降为 4 篇，保留下来的均为 `relationship_shift` 或 `promise_or_rule`。WebUI 与记忆生产页显示待融合数量和当前决策原因；设置可切回 `every_batch`。

召回主路保持 test5 的跨层一致性、时间约束、意图层权重与安全网，不改候选名额和注入结构。时间查询规划与 temporal 检索统一使用同一个日期/月份/季节解析器，避免前端判断为时间问题而后端生成另一套键。WebUI 对安全网观测点“有帮助/救错了”后，会同时形成带原查询作用域的记忆级反馈，之后相似查询才能真正受益；写入失败保持 fail-open。相似簇在“只诊断、不实际折叠”时不再进入每轮请求路径计算。

清理 `EpisodicStore` 被后定义覆盖的重复实现和不可达的旧 Console 整页模板；当前 Console 页面、旧数据表、旧配置键与回退算法仍保留。设置页把旧 `inject_format/summary_chars`、旧分层风格、原文截断废弃键和画像生成项明确归入兼容区，避免误以为会改变当前 4.x 主路。没有数据库 schema 变更，没有 Memos 写入迁移，升级后无需执行命令或重建索引。

### 4.6.0-test5

以 test4 为底座，只增强召回排序、评测和可观测性，不改日记生产、心潮、时间洞察、身体节律、画像、上下文治理与 Memos 同步。精简主路在原有“日记段落 + Episode + 原始轮次 + BM25 + 单次 rerank”之后增加三组可解释信号：跨层证据共同支持时加分，明确日期/月份/季节及第一次、上一次、最近一次等时间约束加分，按具体事件、当前状态、情绪延续、时间事件和叙事回顾调整记忆层偏好与动态目标。缺少原文的旧日记不扣分，时间约束也不硬删其他日期候选。

召回实验室新增四套只修改数值参数的预设：效果优先、精准保守、长线剧情、成本均衡；它们不会修改 Token、角色名、Provider、URL、路径、端口或 Memos。可一键从人工正反馈、真实注入回放和 Episode 证据生成本地评测集，并直接比较 `test5 优化`、`同路关闭优化`、`关闭原文`、`日记直搜` 与 `纯向量` 的 Hit@3/5/10、MRR 和延迟。新增安全网长期观测，记录宽搜是否真正让记忆进入最终结果，并可在 WebUI 标记“有帮助/救错了”。数据库 schema 升为 6，仅新增 `recall_observations`；升级前自动快照，迁移幂等。

升级后无需运行命令。要校准当前角色的数据，进入“召回实验室”，先点“从真实记忆生成”，再点“运行消融”；真实 embedding/rerank 消融会产生对应 Provider 调用。自动样本中的 `positive_feedback` 是人工正标注，`telemetry_regression` 只代表旧版曾经能找到，`episode_holdout` 是从现有证据构造的结构评测，三者含义不同。

### 4.6.0-test4

继续以 test2/test3 的 4.6 底层为基础，不改心潮、时间洞察、身体节律、上下文治理和检索注入权重。记忆生产从主 WebUI 页签拆成独立 `/production` 工作台，增加逐篇 Episode 审计，可查看场景轮次、证据三档、有效原文链接、日记覆盖率、复刻风险、压缩比、渲染重试/保底以及滚动状态队列结果；长日记预览、确认、丢弃、回滚、数据库快照与向量代际切换仍为真实操作。

“可回原文”统一为唯一口径：活跃 Episode 必须存在指向实际 `source_turns` 的精确轮次链接。它不再等同于 `source_grounded` 标签，因此用户编辑后的 `mixed_user_edited` 记忆只要链接仍在，也会在主页、原文页和生产工作台中一致计数；原文存在但 Episode 无精确回链时，健康灯明确显示黄色提示。修正日记 `compression_ratio` 方向错误，并把原文重叠率、直接复刻率、压缩比、渲染回退持久化，旧记录以“历史数据”展示，新生成记录自动获得完整指标。升级幂等新增字段，不清空原文、Episode、状态或 Memos。

### 4.6.0-test3

以 `4.6.0-test2` 为优化底座，保持心潮、时间、身体节律、检索权重和 WebUI 信息架构不变，修复四项可复现的底层缺口：最长公共子串改为正确的前缀滚动哈希并用原文精确复核，能够识别位于不同偏移处的逐字转录；日记渲染唯一一次重试后仍不满足证据覆盖、第一人称或防转录要求时，强制改用已核验证据保底文本，不再把已知不合格版本写入 Memos；原文库路径选择会比较 UUID 与原文/情景数量，并在外部位置记录最后成功打开的库，避免升级或工作目录变化后静默切到同名空库；QueryPlanner 改为词性、专名、引号实体与关系/情绪线索提取，不再把中文句子拆成大量重叠双字噪声。新增对应回归测试，无数据库表结构变更，无需重建索引。

### 4.6.0-test2

`test2` 是以 `4.5.4` 为兼容基线的独立重新实现，不是对 `4.6.0-test1` 的补丁叠加。相较 test1，本版将情景库拆为 `SourceArchive` / `EpisodeRepo` 等 Repository 分层，引入 `VectorGeneration` 影子代际状态机与 `ArchiveGuard` 自动快照守门；`DiaryPipeline` 使用 rolling hash 做证据覆盖、第一人称与逐字稿检查，召回前增加 `QueryPlanner`，`SceneSplitter` 以跨日/停顿等 lead boundary 提供场景候选；长日记重写由 `PreviewStore` 按条目独立事务确认、失败项可单独重试；主 WebUI 根路径新增独立“记忆生产”页签，集中查看生成链与预览状态。升级会自动快照，不执行数据库合并。

### 4.5.4

适配 Memos **0.30.0**（从 0.29.1 升级）；无数据库结构变更、无配置迁移，直接覆盖重载即可。Memos 0.30 的破坏性变更经核验**不触及本插件的 API 面**：插件不使用 shares 路由、不使用 Memos filter 表达式（全量拉取本地过滤）、不使用 MCP；`/api/v1/memos*` CRUD 与 Bearer PAT 认证在 0.30 实测全部 200。本版增强如下：

- **memos 0.30 私有模式适配**：0.30 未设置 `--instance-url` 时匿名 API 仅限 setup/auth/shared 路由。`health_check` 现在先带 token 探测 `/api/v1/memos`（200 = 认证可用），401/403 = 服务在线但未认证，再回退免认证的 `/healthz` 区分"服务在线"与"完全不可达"。
- **新增连接诊断**：`MemosClient.diagnose()` 返回 connected/authenticated/note；`/api/status` 新增 `memos_diag` 字段，WebUI 概览可直接看到"memos 在线但未认证：请创建管理员并生成 Access Token"。
- **401 错误提示**：所有 memos 写操作在 401/403 时附带 0.30 私有模式指引（"请在 memos 设置页生成 Access Token 并填入 memos_token"）。
- **托管模式（managed）已兼容**：`ManagedMemosSidecar` 启动命令本就是 `--data --port`（0.30 已移除 `--mode`），无需改动；就绪判定用 TCP 端口探测，版本无关。
- **升级 Memos 到 0.30 的操作提示**：先备份 memos 数据库；升级后首次访问按官方流程创建管理员（0.30 移除了公开 signup API，首用户通过 `POST /api/v1/users` 或 Web UI 创建），在设置页生成 Access Token 填入 `memos_token`；随后执行一次 `/memos-sync` 验证读写。

### 4.5.3

新增指令控制回合隔离，无数据库结构变更，直接覆盖重载即可。

- **不进入 AstrBot 持久上下文**：以 AstrBot 实际激活的 `CommandFilter` / `CommandGroupFilter` 为主判断，不仅依赖文本前缀。LLM 命令回复发送后，按“请求前历史长度 + 本次命令指纹”只删除本次新增的 user/assistant 区间。
- **并发兜底**：短时间保留最近命令指纹；若下一条普通消息与发送后清理并发，它会在请求进入模型前仅扫描历史尾部并清掉残留命令回合，同时重置过期的 `token_usage`。
- **不污染长期记忆和心理状态**：命令事件不进入插件压缩缓冲，不触发心潮回答后感知，也不执行普通记忆、时间和心理注入链。
- **不做粗暴批量删除**：普通旧消息不会按斜杠全库扫描；没有经过 LLM 会话保存的普通命令回复不会触碰历史。Console 会记录“指令回合隔离”和实际剔除条数。
- **4.5.2 修复保留**：Episode 渲染漏项仍会补写或生成已核验证据保底日记，不会退回旧单阶段结果。

### 4.5.2

修复证据优先生成中“Episode 已提取成功，但文学日记漏掉整个 `episode_key`”时错误回退旧单阶段流程的问题；无数据库结构变更，覆盖即用。

- **部分渲染不再整批降级**：首轮缺失的 Episode 与低覆盖证据一起进入完整性补写；补写结果按 Episode 独立合并，不再要求模型把所有已经正确的日记重复输出一遍。
- **已核验证据最终保全**：补写仍漏掉某个 Episode 时，由该 Episode 的日期、场景、证据、原话、情绪变化、长期影响与未解决事项生成第一人称保底日记。不会虚构新事实，也不会丢掉原始轮次和 `source_grounded` 证据链。
- **覆盖判定减少误报**：机器 detail 与逐字 quote 任一被文学正文可靠覆盖即视为命中，避免模型保留原话却因没有复述机器化摘要而被错误判为遗漏。
- **边界不变**：首次文学渲染完全失败时仍可回退兼容日记流程；检索、注入、上下文治理、心潮、身体节律和时间洞察未改动。

### 4.5.1

这是 4.5.0 的兼容修复版，无数据库结构变更、无数据迁移，也没有改动已经有效的上下文治理策略。

- **启动与卸载生命周期修复**：激活预热和核心懒初始化通过同一个异步入口启动 WebUI，并发时只创建一个服务；WebUI 启动中断会清理半启动实例。卸载会取消并等待预热、迁移、状态和归档任务后再关闭 WebUI 与数据库，避免重载残留端口或任务。
- **同名候选也能被安全网救回**：旧宽搜命中主路已经出现过的 memo 时，不再直接跳过；会合并更强的向量/BM25/rerank 分数、路线证据、命中段落和原文轮次，再重新执行资格与覆盖选择，修复“主路弱命中先占位，安全网反而无法增强”的漏洞。
- **效果优先软目标**：`inject_char_budget` 明确为软目标，新安装默认 `10000`。最高分记忆保持原形、所有已入选记忆都保留；无法压到目标时 Console 和事件日志显示实际超出字数，不再把软约束描述成硬预算。四档预设依次使用 `12000 / 10000 / 8000 / 5000`。
- **WebUI 与文档校准**：架构页、Console、设置说明和快速文档统一显示“软目标”、降级次数与超出量；修正旧复合链安全网用途和默认 3 条/最多 6 条的描述。

### 4.5.0

围绕“不漏检索、不漏注入、细节不丢”三条主线，在 4.4 精简主路之上加安全网与预算治理；无数据库结构变更，覆盖即用。

- **不漏检安全网**：精简主路选择结果为空或过弱（条数与最高分双门槛）时，自动用 3.x 多路检索 + 月份分支补一轮候选并重新选择；救回的候选在资格线与覆盖选择中享有保护，不会被相对分差再次砍掉。只有弱结果轮次才产生额外 embedding 开销，正常轮次仍是一次向量。
- **注入预算制**：新增 `inject_char_budget`（默认 7000 字）。超预算时按排名从低到高逐条降级——完整日记→相关段落→紧凑摘录（事件核心完整保留）——绝不静默丢弃已入选记忆；最高分记忆始终保持原始形态。旧的 1–4 条硬上限放宽为默认 1–6 条（可到 10），普通问题目标条数独立可配（默认 3）。
- **相对分差可配且默认更宽**：普通 0.16→0.24、叙事/时间 0.25→0.34，减少长线剧情被“比最强命中低一点”误伤。
- **生成端细节保全**：事件卡 card_text 现在携带逐字原话引语与事件日期，且日记渲染遗漏的证据优先写入卡面，保证文学正文没写到的细节仍可被词面与向量检索命中；渲染覆盖率过低时不再抛弃整个已核验的证据结构回退旧单阶段，而是保留双阶段结果并在卡面保全遗漏项。
- **热路径性能**：反馈先验从逐候选一次 SQL 改为整池一次批量查询（数百次→1 次往返）；事件卡词面救回从 Python 全表扫描改为 SQLite 内过滤（含 LIKE 通配符转义），召回语义完全不变。
- **4.4 事故报告三项修复**：
  - 首轮初始化竞态：初始化加 `asyncio.Lock` 防重复；插件激活时后台预热核心索引与旧记忆桥接；请求遇到仍在运行的情景迁移时最多等待 `episode_migration_wait_seconds`（默认 6 秒，`asyncio.shield` 保护，超时不取消迁移），避免重载后首轮错误回退旧 multi-query 注入 14 篇全文。
  - 裁剪后旧 token 统计失效：请求上下文实际被裁剪后将 `conversation.token_usage` 置 0，AstrBot 按当前消息重新估算，不再用裁剪前的旧值触发二次压缩。
  - 请求级裁剪写回防护：如实承认修改 `req.contexts` 会被 AstrBot 持久保存回会话（报告根因三）；4.5.0 在裁剪前强制写本地 JSON 备份，备份失败则本轮跳过裁剪，并同步修正了 WebUI 提示与文档中的错误描述。
- **WebUI**：架构面板显示安全网与预算状态、请求路径加入“弱结果安全网 / 预算降级注入”节点；Console 注入表新增预算列（降级/压缩计数）；四档预设与设置页覆盖全部 4.5 新参数。

### 4.4.0

- 将 4.x 已存在但未完全参与主路的原文档案接入真实检索：为 `source_turns` 新增可重建向量索引，与事件卡、日记 passage、BM25 共用同一个 query embedding。
- 原始轮次可以独立救回日记和事件卡都遗漏的细节，再通过批次和证据中的 `turn_indexes` 映射回正确 Episode；只有批次、没有精确指针时采用降权映射。
- 最终注入从“日记主体 + 独立证据包”改为单条融合结构：事件核心负责事实和状态变化，日记视角保留文学性与完整情感弧，一手证据校正具体原话、物件状态、承诺和日期。
- 普通 RP 仅在原始轮次直接相关时加入一条短证据；精确日期、原话、承诺、边界和争议问题最多两条。证据在对应记忆内部出现，不再追加重复大包。
- 滚动状态继续独立常驻，不与候选抢名额；事件卡、日记、原始证据和状态四层全部参与最终效果，但不会把整库一次性塞入上下文。
- 数据库 schema 升级到 v3，仅为原始轮次增加派生向量；旧 Memos、Episode、Evidence、状态版本和心潮数据原样保留，迁移失败时请求继续使用事件卡与日记路线。
- WebUI 与 Console 新增原始轮次索引进度、原文命中/救回诊断，以及日记视角、事件核心、原文证据和记忆结构的独立注入占比。

### 4.3.0

- 当前主路升级为单 query 双粒度召回：同一个 query embedding 并行搜索事件级情景卡和纯正文 passage，叠加 BM25；明确时间问题再补结构化 temporal，最终仍只调用一次 rerank。
- 事件卡和 passage 不再互为前置门槛。passage 漏掉时事件卡可以救回整篇，事件描述宽泛时 passage 可以直接命中局部细节；诊断新增双命中、事件救回和 passage 迁移状态。
- passage embedding 不再重复混入整篇日记的 `retrieval_key/scene_anchor/state_change`。升级后后台从本地 `chunk_text` 分批重算纯正文向量，Memos 正文不改；迁移失败或未完成时旧向量继续可用。
- 最终选择改为证据覆盖：保护不同日期、人物、关系阶段、承诺边界、状态变化、查询事实和未解决事项；只有同日同事实且没有新证据的候选才作为硬重复停止，不使用相似簇删除候选。
- 最近注入去重从硬排除改成 `0.08` 软降权。连续追问同一事件、没有同样强的替代或再次明确命中时，允许重新注入临时记忆块。
- `lean_texture_enable=false` 只禁止无关日常碎片抢位；用户明确询问某个生活细节，或强语义、词面、日期直接命中时，日常记忆仍可进入最终注入。
- 原文证据改为按需展开：普通 RP 只用日记/段落；日期、原话、承诺、边界、争议和未解决事项最多展开 1–2 组证据，减少同一内容在日记与证据包中重复。
- 长省略型情绪表达新增上下文焦点继承，例如“你都记得，不用我说了”“我们没有可能了”，无需额外 LLM 或第二次 query embedding。

### 4.2.0

- 将固定轮数的每晚保底重构为 23:45 自适应夜间记忆检查点。默认从 1 个完整轮次开始，按真实跨日、两小时以上停顿和对话密度计算独立情景容量，实际篇数由可回指原文的证据场景决定，不硬拆完整情感弧。
- 夜间链路完整接入三层记忆：先归档原始快照，再写 Memos 文学日记与情景入口，全部持久化成功后才清理对应序号的 buffer，并按序排队更新滚动状态。
- 失败退避绑定 buffer 快照；同一快照 5 分钟重试、当晚最多 3 次，新消息产生的新快照不会被旧失败上限阻断。23:45 后新增的对话同一晚仍可再次检查。
- 夜间提交成功后异步合并刷新内置时间洞察，不阻塞日记写入；自动更新时间设为 0 或旧附属仍在运行时尊重关闭/让出设置。
- 新增三个夜间检查点设置和 WebUI 状态诊断；四档预设同步调整情景容量。时间洞察继续默认开启，并在 README 明确推荐使用边界。
- 设置与概览明确区分 4.x 当前精简主路、4.0 情景级联回退和 3.x 最后兜底；旧多查询、月份路由和信息增益参数不会与精简主路同时工作。

### 4.1.2

- Tier 关键词默认集扩充为身份与称呼、关系边界、承诺与长期计划、创伤与安全、人格变化、生活锚点等更完整词族。旧版原始默认值会自动迁移；用户自定义词表保持原样。
- Tier 继续作为重要度兜底：LLM 已给出合法 `1-5` 时不覆盖模型判断；缺失、非法、旧日记重建和手动路径才使用关键词推定。其结果继续参与加权排序、滚动状态来源选择和高重要记忆保护。
- 时间洞察 v3 集成进主插件，复用现有本地 SQLite 数据，不改 Memos 正文；每轮只做有界本地查询，LLM 仅用于周期/手动重建精炼且失败时保留确定性候选。
- 普通环境注入默认只允许精确纪念日或强近期趋势，最多 1 条；邻近纪念日与季节规律只在相关问题中触发。新增证据门槛、重复冷却、查询旁路和独立注入统计。
- 心潮工作台新增“时间洞察”独立子页，包含状态、证据候选、无写入预览、立即重建、Provider 选择及全部专用设置；设置保存后热应用。
- 检测到旧时间洞察附属插件时自动让出注入与后台写入，避免重复；保留旧配置键和数据库表，升级不清空既有洞察数据。

### 4.1.1

- 修复滚动状态已作为独立临时块成功注入后，旧画像仍可能被后续剧情格式化路径重复注入的问题。状态注入失败时仍保留旧画像回退，不会因接管逻辑丢失人格信息。
- 精简主路默认剧情硬上限由 3 调整为 4：普通问题仍以 2 条为目标，叙事或明确时间问题才允许扩展到 4 条；绝对资格线、相对分差、近期去重和 texture 排除继续生效，不机械凑满。
- “深度角色塑形”和“效果优先”预设同步为最多 4 条，“均衡推荐”调整为最多 3 条；低成本预设保持 2 条。
- README 明确滚动状态 LLM 只在成功压缩、初始化或重建时调用，每轮聊天只读取已保存状态。

### 4.1.0

- 在原有 4.0 SQLite 上增量增加滚动状态、状态版本、有序更新队列、召回评测样本与消融记录；迁移可重复执行，不删除 4.0 表和记录。
- 日记持久化后异步融合当前状态，状态失败不阻断日记/原文档案；失败批次按创建时间重试，新批次不能越过失败旧批。
- 已有 4.0 数据自动初始化首版状态；旧画像作为种子和历史保留，状态就绪后默认不再重复注入旧画像。
- 真实召回主路改为一次 direct embedding + 向量/BM25 宽候选；时间意图只查结构化时间元数据；候选统一最多 rerank 一次。
- 主路不执行旧 multi-query、月份并行、情景卡级联、相似簇折叠和信息增益配额；4.0/3.x 路线保留为可关闭新主路后的回退与消融基线。
- 默认注入 1–3 条剧情记忆，texture 默认不进主注入；历史时间边界合并到集合头，减少重复规则壳。
- WebUI 新增当前状态、折叠版本历史、真实召回评测集和四路线消融；反馈中的有帮助/关键记忆可直接沉淀评测样本。
- 主 WebUI 围绕三层记忆重排：移除旧搜索、月份路由实验与相似簇可见页；兼容算法仍保留在后端回退和消融中，设置集中到“兼容回退”分组。
- Console 注入构成新增滚动状态与原文证据，版本、主路名称和相关配置说明全部同步到 4.1.0。

### 4.0.0

- 新增独立 `episodic_memory.db`，保存内容哈希幂等的原始对话批次、逐轮内容、情景卡和证据链；不修改 AstrBot core，也不直接读写 Memos SQLite。
- 自动压缩升级为证据提取与文学渲染双阶段：校验轮次范围、主体、逐字原话和至少一项可落地证据，并对成品日记做证据覆盖率检查。
- 保底压缩在情景提取阶段补做一次目标篇数覆盖检查；跨日和错误 `conversation_now` 校验继续生效。
- 全部旧 Memos 自动迁移为 `diary_derived` 情景卡。迁移幂等，模型换维后自动补缺失事件向量；迁移未完成时整轮保持 3.x 检索。
- Memos 用户编辑新日记后，原始证据不会被旧日记推导覆盖，事件标记为 `mixed_user_edited`；删除 Memos 会同步移除可召回情景卡。
- 真实召回改为一次 query embedding 的级联管线：情景卡缩圈、候选内段落精搜、至多一次 rerank。明确问题不拼接上文，短指代问题才使用有限上下文。
- 4.0 候选强制执行信息增益选择与必要事件保护；日期、人物、关系、承诺和不同事件阶段不会被相似簇先折叠。
- 注入新增 `<RecalledMemoryEvidence>` 查询证据包。核心记忆可给全文，辅助记忆给相关段落，所有历史内容继续使用临时 user content 并明确不得冒充当前事件。
- 召回实验室与真实请求统一为同一检索、后处理、全文/段落判断和证据注入预览。
- 主 WebUI 新增“情景证据”页面和 15 项 4.0 设置；四档预设同步控制新参数，但仍不会修改 Memos Token、角色名、Provider、路径或端口。
- 新增 `/memos-episodic-rebuild`；`/memos-sync` 与 `/memos-reindex` 同时维护情景卡。所有新链路失败开放，旧 3.x 检索和单阶段日记生成继续作为回退。

### 3.2.6

- 新增 `live_perception_provider_id` 与 `live_perception_timeout_seconds`，只控制请求前即时评估；默认跟随当前会话模型并限制为 10 秒。
- 新增 `post_perception_timeout_seconds`，只控制回答后持久结算；默认 60 秒，后台执行且不阻塞本轮回复。原 `perception_provider_id` 现在明确只用于后台结算模型。
- 即时与后台通道使用独立连续失败计数和 180 秒熔断。即时模型超时不会禁止后台模型继续结算，后台慢模型失败也不会关闭当前回复前的即时判断。
- 兼容旧 `xinchao_settings.json`：旧共享超时为默认 20 秒时后台迁移到 60 秒；非默认自定义值原样继承。心潮状态、驱动力、梦境和历史不重置。
- 心潮工作台设置和最近感知面板同步拆分两条通道，可分别选择 Provider，并显示各自错误原因、健康状态和恢复倒计时。
- 本版不修改 Memos、日记、画像、检索、月份路由、AstrBot 上下文、持久 buffer 和身体节律算法；升级无需执行任何重建命令。

### 3.2.5

- 修复晚间保底压缩3轮触发与底层8消息门槛冲突。`eod` 现在以至少3个助手轮次为最低条件；失败后5分钟重试、当晚最多3次，普通自动压缩门槛不变。
- 心潮两阶段 LLM 感知改为结构化容错管线：加入稳定 JSON-only system prompt、围栏/尾逗号/单引号/字段别名/中文驱动力名/百分比置信度归一化；confidence 明确表示证据确定性，不再与情绪强度混淆。
- 回答后感知遇到快速 Provider 或格式失败时，可在同一个总超时预算内重试一次；请求前即时评估仍只调用一次。连续 3 次失败会熔断 180 秒，成功后立即恢复；每一种回退均保留先行规则证据并记录精确原因。
- 新增派生表 `memo_month_index` 与 `memo_month_routes`。每个月按日记向量自动形成最多 6 个主题中心，并保存人物、标签、检索句、场景线索、状态变化、记忆类型和原始日记引用；它不是 LLM 摘要，不会压缩掉日期或细节。
- 全库多路检索和月份路由使用两个独立异步任务并行运行。月份分支有独立开关、展开月份数、候选数和超时；失败、空索引或超时均开放失败，只放弃月份补充路线。
- 两路候选按完整 `memo_name` 合并。直接路线的分数保持不变；重复项只追加路线诊断，不叠加分数，同一篇原始日记最终最多注入一次。原检索候选先使用原分层、信息增益和动态名额完成选择，月份独有候选再使用 `recall_month_route_inject_max` 独立补充名额追加，绝不挤占原检索篇数；直接路线完全无候选时，月份路线自动作为开放失败的正常回退。
- 月份索引是本地派生数据：同步、增量替换或删除日记时只标记索引过期，下一次真实召回或打开月份时间线时自动重建。升级旧库不需要 `/memos-sync`、`/memos-reindex` 或 LLM 重分类。
- WebUI 时间线替换为月份日历。页面按星期排列完整月份，有日记的日期使用绿色强度和篇数标记；点击日期可查看当天日记，右侧显示检索线索、人物实体、标签和记忆类型。
- 月份时间线增加独立路由实验输入，可查看问题命中的月份、综合分、语义分、线索分和月份日记数；月份未命中时原全库检索仍照常运行。
- 反馈改为查询相关事件，保存原始查询、请求编号、日记、动作、来源和可选查询向量。相似查询才读取该反馈，影响限制在 `-0.22` 到 `+0.18`，不会把一篇日记永久变成全局高权重或全局低权重。
- 真实注入记录和召回实验室均提供“有帮助、关键记忆、无关、事实错误、已过时、太频繁”；反馈可在 WebUI 撤销。旧 `memo_feedback` 数据保留但不再参与排序。
- 独立锚点页面、锚点命令、锚点排序 boost 和画像锚点输入已停止。旧 `memory_anchors` 表不删除、不迁移，升级和重建均保留，便于回退。
- `/memos-summarize`、`/memos-feedback`、`/memos-anchor` 不再注册；旧 `memo_summaries` 和 `memory_anchors` 数据保持原样但不参与运行。

### 3.2.4

- 心潮状态 schema 升级到 v6：十二维拆分为长期积累 `drives`、当前激活 `driveActivations`、变化元数据 `driveMeta` 和有界历史 `driveHistory`；旧 `xinchao_state.json` 自动补字段并保留原数值。
- 统一综合值：注入筛选、最强意向、梦境材料、主动表达门槛和 WebUI 排序全部使用积累与激活形成的综合心理值，不再有功能仍暗中只读旧单层数值。
- 分级满足：回答后感知支持 `satisfactionLevels`，普通交流只部分缓解 `social`；只有角色实际分享自己的经历、发现或内心内容时才满足 `share`，用户分享不会代替角色表达。
- 情绪余波：`grieve` 与 `anger` 改为只由事件触发，并使用不同半衰期自然消退；不会再长期停在某个值，也不会和普通需要一样凭空增长。
- 即时激活：每个维度使用独立衰减速度；关系断裂、牵挂、冲突、亲密、提问和普通问候会产生不同强度。LLM 感知负责细化，但模型漏字段时保留确定性关系/冲突、交流和自我表达证据。
- 闪念/执念：衰减依据真实经过小时，不再依据结算调用次数；相似闪念会合并强化，重复三次或长时间保持高强度后才进入执念，执念缓慢反馈对应激活。
- 精神负荷：长对话和高情绪轮次增加克制的实际负荷，睡眠中恢复；清醒且总体压力较低时可缓慢回落。
- 心潮注入：每条倾向标记为“持续积累”“当前仍在前景”“背景倾向”或“情绪余波”；即时反应按激活强度排序，仍保持单个临时 `DynamicMindState`，不改 system prompt。
- 身心合成：显式处理想连接但社交余量低、想推进但精力不足、靠近与安稳偏好并存、不满与高敏感叠加四类冲突，只改变表达方式，不生成事实或强制行为。
- 身体连续性：阶段基线与稳定日波动平滑衔接到次日，四时段在边界前渐变；修复跨午夜的深夜坐标错误，避免整点和日期切换造成身体信号突跳。
- 分布式出口：自主念头读取分层驱动力、盘旋念头和身心合成；白天记忆浮现用当前心理方向选择候选，无自然联系必须 `SKIP`。现有来源冷却、重复拦截、并发锁和每日上限不变。
- 心潮工作台：十二维改为积累/激活双轨显示，保留综合值；新增心理变化证据流，展示前后值、来源、原因、时间，以及回答后的满足、激活、余波和负荷。
- 本版没有新增命令或配置项，不修改日记、画像、Memos、向量、段落索引和 AstrBot 历史；升级后无需执行同步或重建。

### 3.2.3

- `pending_messages` 新增兼容字段 `event_ts` 与 `event_timezone`；新对话按请求开始时间记录，user 与对应 assistant 回答使用同一时间锚点。
- 旧缓冲无需迁移命令：没有新字段值的行自动回退原 `created_ts`，内容、顺序和待压缩状态均保留。
- 压缩输入在每轮对话前加入持久化的公历日期、分钟、星期、时段和时区，并提供整个缓冲的实际时间范围及日期清单。
- 压缩执行时间和最近请求时间降为整理参考，不再覆盖逐轮记录时间；用户明确给出的剧情日期/时段仍拥有最高优先级。
- 跨日缓冲的目标日记数至少覆盖记录日期数；独立日期场景必须分开，不可把前一天归到夜间执行日，连续跨午夜场景可保持完整但必须写明跨日。
- 增加压缩结果时间校验：单日期 `conversation_now` 错日可确定性纠正；跨日缺失或错误会带日期清单重试一次；仍无法安全确定时降为未知，不持久化已知错误日期。
- `/memos-buffer-status` 现在显示待压缩对话的实际时间范围，WebUI 活动日志显示 `recorded_dates` 与缺失日期诊断。
- WebUI 历史画像改为紧凑版本列表，默认不渲染正文；点击版本后才展开对应画像和事实锚点，并自动收起其他版本。历史保存和画像注入逻辑不变。
- 升级后不需要 `/memos-sync`、`/memos-reindex`、段落重建或相似簇重建；首次打开本地索引时自动补齐缓冲表字段。

### 3.2.2

- 新增随包发布的 `TIME_AND_INJECTION_REPORT.md`，完整说明现实时间、剧情时间、日记事件时间、Memos 来源时间、本地索引时间、画像、心潮、身体节律和压缩任务的关系。
- 历史日记“距现在多久”改为复用本轮唯一请求快照，避免跨分钟或跨午夜时与 `CurrentTimeContext` 出现边界偏差。
- 自动压缩仅复用 30 分钟内的会话请求快照；每晚保底和后台压缩始终按任务执行时刻重新取时，不再继承早先会话时段。
- 主 WebUI 重构视觉层和响应式布局：桌面侧栏、平板横向导航、移动端统计区和双栏工作区均重新适配，保留全部原页面、接口与操作。
- 主 WebUI 增加页面切换、状态点、注入比例和刷新反馈动画，并支持 `prefers-reduced-motion` 无动画偏好。
- 概览页增加“时间模型”卡片和连接状态明细，直接显示同轮快照、RP 时区、身体时区与最近捕获时间。
- 本版不迁移数据，不需要执行同步、向量重建、段落重建或相似簇重建命令。

### 3.2.1

- 每轮请求只生成一个 UTC 时间快照，现实时间块、日内时段与身体节律共享该时刻，再按配置时区转换。
- 修复跨分钟或跨午夜时，`CurrentTimeContext` 与身体状态可能分别落在两个日期或时段的问题。
- 精简重复时间规则，但保留完整公历日期、分钟、星期、时令、可选节日和对话节奏。
- 日记 `HistoricalMemory.occurred_at`、Memos 来源时间和本地索引时间均未改写；旧日记仍明确属于历史。
- 用户明确设定剧情时间时按剧情叙事，但现实时间和身体节律仍由本轮时间快照决定。
- 完整移除内置 KB cache optimizer、8 个配置项、请求改写链、专属 API、WebUI 页面、Console 分类和注入占比。
- 不再识别、剥离、截断、去重、包装或重新注入 AstrBot 知识库内容；Astr 原生知识库功能不受本插件干预。
- 保留 Provider cached-token 统计、Prefix drift 与 System guard；三者是通用缓存诊断，不接管知识库。
- 升级无需执行同步或重建命令，Memos、sqlite、向量、段落索引、画像、心潮状态和上下文备份均不迁移。

### 3.2.0

- 身体节律从单层阈值提示升级为“阶段主体感受、当日稳定波动、阶段化时段修正、显著信号强化”四层合成。
- 四个阶段都增加阶段内部的早期、中段和后段体验，变化由连续进度驱动，不在边界日突然切换全部表现。
- 增加早晨、午后、傍晚和深夜四个时段；每个阶段都有独立的身体修正文本和信号偏移，共 16 组组合。
- 新增 `body_expression_mode`：`significant` 保留旧式克制注入，`balanced` 为默认完整体验，`immersive` 保留更多显著细节。
- 均衡和沉浸模式即使信号接近中性，也会保留短主体底色，避免 WebUI 显示周期在运行但模型完全收不到身体感受。
- 身体提示明确限定为动作幅度、注意力、耐力、停顿和措辞的微调；不移植原 Period 中强制撒娇、发牢骚、维持兴奋或挽留熬夜等行为指令。
- 身体体验继续合并在唯一的 `<DynamicMindState>` 临时块中，并在记忆注入后移动到临时内容末端；不增加请求钩子、system prompt 动态内容或持久数据。
- 梦境材料现在同时读取阶段主体感受、当前时段修正和显著信号，但仍明确标记为当下感受而非剧情事实。
- `/xinchao` 身体页面按层展示主体感受、时段修正和显著信号；真实请求后单独显示实际身体注入文本、模式、时段和字符数。
- 旧 `xinchao_settings.json` 无需迁移命令，读取时自动补齐均衡模式；日记、画像、向量索引和心潮持久状态均不改写。

### 3.1.1

- 新增纯 Python 身体节律引擎：按公历锚点、周期长度、经期长度、活跃中心日和窗口计算四阶段。
- 新增身体精力、不适、感官敏感、社交余量、安稳偏好和亲近感知六维信号。
- 每日变化由角色和日期生成稳定种子，同一天不会随请求随机跳变；时段修正复用心潮时区，不重复时间提示。
- 同一角色跨会话保持一致的当日身体底色；接近中性时不注入空壳状态，只在工作台保留可视化。
- 身体底色由心潮统一合入现有 `<DynamicMindState>`，不新增 system prompt、不新增第二个请求钩子或临时注入块。
- 身体只影响当前表达，不写入长期驱动力、Memos 日记、画像、反馈、锚点或 AstrBot 会话历史。
- 梦境结算可以读取当前身体倾向作为感受材料，但明确禁止把身体底色或梦写成现实事件。
- `/xinchao` 新增身体节律页面、四阶段位置、六维信号、当日倾向、真实注入预览和 14 项设置。
- 旧版 `xinchao_settings.json` 自动补齐身体默认值；锚点默认留空，升级后保持无行为变化。
- 不移植 Period 实验性情绪系统的三次 LLM 决策、冷暴力、已读不回、敷衍工具和响应清空逻辑，避免与心潮重复控制回答。
- 增补 `astrbot_plugin_period` MIT 许可声明，详见 `THIRD_PARTY_NOTICES.md`。
- 白天记忆浮现新增来源级去重：记录模型采用的 Memos 日记，默认冷却 72 小时，插件重启后仍有效。
- 主动发送按角色串行执行，避免两个到期任务读取同一旧状态后重复发送；文案去重窗口同时扩展到最近 8 条完整跨类型历史。
- 心潮的感知、梦境和主动消息 Provider ID 改为 AstrBot 已启用 LLM 下拉选择，保留“跟随当前会话模型”和旧失效值提示。

### 3.1.0

- 对话感知改为请求前即时评估、回答后持久结算。当前强情绪或关系变化可以影响当前回复，不再固定滞后一轮。
- 即时评估只生成临时反应方向，不提前写入满足或情绪结果；回答后才根据角色实际回答更新驱动力、闪念和摘要。
- 补齐原项目 schema v4 的跨类型主动消息状态、最近 8 条历史、梦境呼吸上下文与旧状态自动迁移。
- 补齐梦境余韵主动消息；每个新梦只尝试一次，并受离线时长、冷却和主动消息每日总上限保护。
- 补齐白天记忆浮现；直接读取本地 Memos 日记索引，按当地时区窗口、随机间隔和独立每日上限运行。
- 主动消息去重扩展为梦境余韵、自主念头和白天浮现跨类型统一比较，近似候选可按配置重试。
- 心潮工作台新增即时评估诊断、真实主动消息记录、梦境呼吸上下文和全部新增设置。
- 新增原项目到 Astr 的能力映射说明；不把 2 MB 封面图、Node/Docker 部署壳或 Bark 客户端当作插件功能代码打包。

### 3.0.0

- 主插件内置心潮动态心智层，无需 Node、额外服务或额外端口。
- 保留十二维驱动力、闪念/执念、疲劳、睡眠、梦境、意向、清晨静默和主动表达门控。
- 新增 AstrBot 自然对话感知适配，支持规则、混合和 LLM 三种模式；后台执行、限时、低置信度回退。
- 当前心理倾向通过临时 `extra_user_content_parts` 注入，不改 system prompt，不直接改写模型回答。
- 心潮注入只在存在显著状态时生成，并在历史记忆块之后排列，突出“过去如何影响此刻”。
- 唤醒动作前移到请求阶段，第一次叫醒角色的回复即可获得梦境余韵和内心理解。
- 梦境可选读取本地长期日记索引的重要材料，完整保存梦境、余韵、内心理解、来源和是否使用记忆。
- 新增独立 `/xinchao` 工作台、9 个读写 API、Console `xinchao` 日志分类和注入构成统计。
- 新增 5 个心潮命令；主动消息默认关闭，具备空闲、冷却、每日上限、强度和重复门控。
- 心潮状态与设置独立持久化，升级不改写 Memos、向量索引、画像、锚点、反馈或 AstrBot 会话历史。
- 包含 Xinchao Dynamic Mind 的 MIT 许可声明，详见 `THIRD_PARTY_NOTICES.md`。

### 2.2.0

- 重构 Console。
- 新增 `/api/console/stats`。
- 注入构成统计拆分为画像、当前时间、时间洞察、日记、enhancer、Astr 上下文、KB cache 等。

### 2.2.1

- 合入 KB cache optimizer。
- AstrBot 默认知识库 RAG 从 `system_prompt` 剥离，改为临时 extra 注入。
- Console 增加 `kb_cache` 事件和 cached token 观察。

### 2.2.2

- KB cache 从 beta 能力转为核心能力。
- 修复关闭农历时，时间解析把空 `农历:` 跨行误读为“对话节奏”。
- 请求级上下文治理默认值调整为 `80/40`。
- recall 日志保留 `contexts_total` 和 `context_query_parts`。

### 2.2.3

- 每晚保底压缩使用“目标 N 篇”提示。
- 保底压缩少于目标时自动重试一次。
- 保底来源写入隐藏 metadata：`source=eod`。
- 过滤流程来源 tag。

### 2.2.4

- 压缩解析阶段剥离正文里的内联 `#标签`。
- 内联 tag 合并进结构化 `tags` 字段。
- Memos 写入时统一把 tags 放到日记末尾标签行。
- 普通压缩和每晚保底压缩都生效。

### 2.2.5

- 新增可选 `managed` Memos 托管模式。
- 插件只启动配置目录里的 `memos.exe`，不扫描本机其他实例。
- 支持自动端口和自定义固定端口。
- 托管 Memos data 目录默认放在 AstrBot data 下，可配置修改。
- WebUI 概览和 `/memos-managed-status` 可查看托管状态、exe、data、端口和日志路径。

### 2.2.6

- 相似簇重建使用每篇日记的平均 embedding，不再只看第一块 chunk。
- 有 embedding 时也会计算文本重叠兜底，避免语义边过严导致全是孤岛。
- 相似边来源会标记为 `embedding_avg`、`text_overlap` 或 `tag_type_overlap`。
- 真实折叠开关不变，仍可先在 WebUI 观察。

### 2.2.7

- 当前时间块升级为 `CurrentTimeContext priority="high"`，明确它是本轮对话的“现在”。
- 召回日记改用 `HistoricalMemory` 包装，强调日记日期是历史记忆发生时间，不是当前时间。
- 注入构成统计新增 `current_time`，Console 表格新增 `now` 列。

### 2.2.8

- KB cache 支持更多 marker 变体，包括无冒号的 `[Related Knowledge Base Results]` 和中文相关知识标记。
- KB 知识片段按标题行分组，避免把 `[Knowledge 1]` 切坏。
- 包含式去重会保留更完整的知识条目，而不是保留较短旧条目。
- 历史记忆提示进一步强调：召回日记不是刚刚发生，也不是本轮刚发生。

### 2.4.1

- WebUI 新增 Settings 页，直接从插件 schema 构建全部设置项，支持分类、搜索、类型化输入、恢复默认值和修改计数。
- 新增深度角色塑形、效果优先、均衡推荐、低成本四套参数预设；应用前显示差异，不修改连接、Token、Provider、路径或端口。
- 设置保存直接复用 AstrBot 配置对象和 `save_config()`，不会生成独立配置副本；运行参数热应用，初始化类参数明确提示重载。
- Token 等敏感值只返回配置状态，空白输入保留原值，显式确认才能清除。
- 增加未知字段、类型、范围、请求体大小和同源写入校验；持久化失败时回滚本次内存修改。
- 新增真实 HTTP 回归测试，覆盖 131 项 schema 映射、敏感值遮蔽、配置落盘、热应用、预设边界、跨来源拒绝和失败回滚。
- 修复相似簇 `tag_type_overlap` 分数始终低于门槛、实际永远无法单独成边的问题；标签与类型现在必须同时获得最低正文证据，临界 embedding 也可由独立证据融合越过门槛。
- 全量重建和新日记增量建簇统一使用同一套证据评分，避免重建后与后续新增日记的相似边标准不一致。

### 2.4.0

- 新增多路规则 Query：`direct/context/entity/relationship/temporal` 分别检索，再以 RRF 融合到 memo 级候选；不增加额外 LLM 延迟。
- 每个候选保留检索路线、命中 passage、字符范围和融合证据，召回实验室可解释候选来自哪里。
- 新增必要日记保护：精确公历日期会先规范化比较，允许同日多篇不同事件突破动态数量；专名、关系、情绪和叙事阶段按证据保护。
- 修复日期格式不一致可能把 `2026年7月3日` 退化成普通 `2026` 关键词的问题；日期数字碎片不再产生错误必要项。
- 新增边际信息增益选择。相似簇退出真实注入裁决，只用于可视化和诊断；同簇多篇互补日记不会被簇规则压掉。
- sqlite `chunks` 自动迁移新增 `passage_index/char_start/char_end/content_hash/scene_anchor/retrieval_key/state_change/entities`。
- 新写入、同步和全量重建都会按完整句边界建立 passage；embedding 使用检索字段增强，数据库仍保存原始 passage，Memos 仍是完整原文真相源。
- 新增 `/memos-passage-rebuild`，无损为旧日记重建段落索引，不改写 Memos，不调用分类 LLM，并保留反馈、锚点、画像和摘要。
- 修复增量同步更新 memo 时调用完整删除接口，可能连带删除该 memo feedback/anchor 的问题；现在只替换可重建的派生索引。
- 新增全文/段落混合注入。核心、短日记、多段命中、时间叙事、完整情感弧和必要人格记忆使用全文，局部辅助事实可使用带来源边界的扩展 passage。
- 压缩 prompt 取消日记正文 `150-450` 一类硬上限，明确完整性和文学性优先，保留动作、对话、称呼、物件、情绪变化、关系因果与结果。
- 压缩结构新增 `retrieval_key/state_change/entities`；字段从完整正文中后提取，以隐藏 metadata 保存，不能替代文学正文。
- WebUI 新增 `/api/passages/status`；记忆详情返回 passage 和机器检索字段；召回实验室显示必要原因、全文/片段模式、命中段落和检索路线。

### 2.3.3

- 修复“动态注入”实际上总是把合格候选填满到 `recall_max_inject` 的问题。
- 新增渐进证据门槛：第 1 篇使用基础相关线，越靠后的篇目需要越强的向量、rerank、触发线索、标签或长期影响证据。
- 重要度、手动钉、锚点和反馈继续影响排序，但不再仅凭“重要”就把本轮注入数量推到最大值。
- rerank 保存真实 `relevance_score`；只有达到救回线的候选可以绕过原始相似度门槛，替代旧版“rerank 池前 20 篇全部救回”的宽松行为。
- 纯关键词候选的相关性改为按命中数量和累计权重估算，单个普通关键词不能伪装成 `0.9` 强语义命中。
- `recall_postprocess` 诊断新增 `dynamic_target` 与逐位 `strength/required/passed`，召回实验室可以解释为什么最终是 2、4、6 或 9 篇。

### 2.3.2

- WebUI 历史对话改为调用 AstrBot `ConversationManager.get_conversations()` 枚举持久会话，不再依赖插件进程内的 `_seen_context_sessions`。
- 会话选择以 `unified_msg_origin + conversation_id` 定位，可查看当前会话和同一 bot 的旧会话；标题、消息数、创建/更新时间和当前状态均来自 AstrBot 数据。
- 统一处理 JSON 字符串、list/tuple 和 dict 包装的 conversation history；请求归档、周期归档和 WebUI 浏览共用兼容解析逻辑。
- 修复召回实验室 `up/down` 反馈与后端动作名不一致；修复锚点按钮缺少类型参数以及锚点页缺少记忆正文、类型、重要度和日期字段。
- 历史备份接口继续限制在配置的备份目录内，并在 WebUI 显示备份消息及归档前后数量。
- 使用 AstrBot 4.25.5 实际源码/运行环境核对插件钩子和会话接口；完成核心请求、时间边界、异步 rerank、两篇保底压缩、缓冲快照、画像历史、分页重建、增量同步、上下文归档及全部页面 API 回归。

### 2.3.1

- 注入构成从“仅成功召回日记时记录”改为“每个 LLM 请求都记录”，空库、关闭召回、无候选、超时和注入失败也有真实样本与结果状态。
- 同一次请求先记录基础构成，召回结束后按 `request_id` 原位更新，避免生成两条重复样本。
- Console 事件和最近 120 条请求样本持久化到向量数据库同目录的 `runtime_telemetry.json`，插件重载或 AstrBot 重启后仍可查看近期运行情况。
- WebUI 启动时会识别并关闭同进程内占用相同端口的旧插件服务器，解决热更新后 AstrBot 日志属于新实例、WebUI 却仍读取旧实例的断链。
- Console 增加请求结果列、持久化诊断和明确的 API 错误显示；清空日志会同步清理持久化状态。

### 2.3.0

- 新增 `temporal.py`，统一当前时间、事件发生时间、Memos 来源时间和本地索引时间的解析与展示。
- 日记压缩输出升级为完整 `event_date=YYYY-MM-DD` 和 `time_basis`；隐藏 metadata 持久化 `occurred_at/time_basis`。
- sqlite `chunks` 自动迁移新增 `occurred_at`、`event_ts`、`time_basis`、`source_created_ts`、`source_updated_ts`、`indexed_ts`。
- `/memos-sync` 和 `/memos-reindex` 支持 Memos camelCase/snake_case 时间字段，并结合来源时间为旧月日标题补全年份。
- 新近度、时间线、月份统计、画像最近来源和月度摘要改按事件时间排序，索引重建时间不再伪装成记忆新近度。
- 当前时间块升级为唯一当前时刻；历史日记、画像和时间洞察分别使用清晰的时间角色边界。
- 纪念日同月日 boost 只在明确周年/纪念日意图下生效。
- query 上下文拼接排除当前句重复，默认最多 3 条、900 字。
- 持久缓冲采用有界快照提交，压缩成功只删除快照边界内消息；失败、并发新消息和超批消息都会保留。
- `/memos-reindex` 不再清空用户反馈、锚点、画像历史和月度摘要。
- 新增 `compress_llm_timeout`、`compress_batch_max_messages`、`recall_context_query_messages`、`recall_context_query_max_chars`。
- 修复通用单行 Memos 被误把首行当日期而丢失正文、同步分页 token 重复导致潜在死循环、托管 Memos 日志句柄未释放等问题。
- WebUI 记忆列表、详情、时间线、搜索和概览增加时间依据、来源时间及时间质量显示；概览注入构成补回当前时间项。

### 2.2.12

- 新增 `profile_llm_timeout` 配置，默认 90 秒。
- 画像 LLM 调用不再复用 `recall_search_timeout`，避免画像融合在 20 秒左右被过早切断。
- 这只影响手动/自动画像更新，不改变聊天前召回、KB、时间注入和上下文治理。

### 2.2.11

- WebUI 新增 `KB 分析` 页签。
- 概览页新增 `KB 作用及分析` 卡片，用运行样本直接显示 KB optimizer 当前是否有效，并单独展示 provider 缓存指标。
- 新增 `/api/kb-cache/analysis`，汇总 KB 剥离字符数、临时注入字符数、去重/截断情况，并把 provider cached token 命中率、prefix drift 变化来源作为独立缓存诊断返回。
- KB 分析页会给出具体建议，例如是否应调整 `kb_cache_max_chars` 或 `context_keep_recent_messages`。

### 2.2.10

- 当前时间注入改为先清理旧 `CurrentTimeContext`，再生成本轮唯一时间块，并插入临时 extra 的最前面。
- 空本地记忆索引时仍执行时间增强、KB cache 剥离和请求级上下文治理，只跳过记忆检索初始化，避免新 bot 既卡住又失去时间提示。
- KB cache 注入模式默认改为 `auto`，DeepSeek/OpenAI/Anthropic/Moonshot 走 `extra_user_content`，Gemini 走 `user_message_before`；新增 `kb_cache_provider_mode` 手动覆盖。
- 新增 cache prefix drift 诊断，Console 可查看 system/history hash、字数、轮数和变化来源。

### 2.2.9

- 新增 system prompt 缓存友好守门。
- 误入 system 的 `CurrentTimeContext`、`HistoricalMemory`、`CharacterKnowledgeReference` 会搬回临时 extra 注入。
- Console 增加 `System guard` 状态，显示 system 长度、hash、搬运数量和动态标记残留。
- 目标结构是 system 固定，时间、记忆、画像、KB 走临时 extra，原始上下文只保留最近 N 条。

### 2.2.6 候选修复 7

- 修复 async rerank provider 在线程新事件循环中调用失败的问题。
- rerank / embedding 这类 async provider 会在 AstrBot 当前任务中 `asyncio.wait_for` 限时执行。
- 仍保留 `rerank_timeout`，超时后回退粗排。

### 2.2.6 候选修复 6

- 修复画像自动循环在空库时可能忙循环的问题。
- 空本地记忆索引不再触发自动画像生成。
- 画像 LLM 调用增加超时保护。

### 2.2.6 候选修复 5

- 修复新 bot / 空本地记忆索引时仍执行请求改写的问题。
- 当本地 sqlite 中 `distinct_memo_count == 0` 时，`on_llm_request` 在完整初始化前直接透传，不再启动 Memos/WebUI/provider/后台任务，也不执行 context governance、KB cache optimizer、RP enhancer、recall。
- 日志会显示 `request pass-through: local memory index empty`，用于区分“插件已放行”和“召回跳过”。

### 2.2.6 候选修复 4

- 修复请求期记忆召回可能卡住整个 AstrBot 的问题。
- query embedding 在请求链路中受 `recall_embed_timeout` 保护。
- vector/BM25/rerank 检索受 `recall_search_timeout` 保护。
- rerank provider 受 `rerank_timeout` 保护，超时后回退粗排。
- 本地记忆索引为空时直接跳过召回，新 bot 第一轮不会无意义调用 embedding。

### 2.2.6 候选修复 3

- 修复托管 Memos 自动端口重启漂移问题。
- `managed_memos_port=0` 时，第一次选中的端口会写入 `managed_state.json`。
- 插件重启时优先复用上次端口；如果该端口已经打开，会标记为 `adopted_existing` 并直接接入。
- `/memos-managed-status` 现在会显示 `state` 文件路径，方便确认端口记录位置。

### 2.2.6 候选修复 2

- 旧关系图谱退出召回治理，新增独立的预计算相似簇索引。
- `/memos-graph-rebuild` 保留旧指令名，但现在重建相似簇图；新增 `/memos-cluster-rebuild`。
- WebUI Graph 页面改为相似簇图，显示簇 ID、簇大小、相似边、相似来源和簇内详情。
- 真实召回折叠优先读取预计算簇，缺失时只对本轮候选做轻量临时分组，避免每次聊天全库计算。

### 2.2.6 候选功能

- 新增规则版注入重排器，生成 `injection_score`。
- 新增动态注入数量：不再为了凑固定 top_k 注入弱相关记忆。
- 新增相似簇软折叠实验室，默认每簇 2 篇。
- `recall_cluster_fold_apply` 默认关闭，先在 WebUI 召回实验室观察。
- 召回实验室显示候选池、合格数、折叠数、最终入选数、簇编号和入选/淘汰原因。
