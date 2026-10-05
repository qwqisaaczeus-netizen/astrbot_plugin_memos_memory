# AstrBot Memos Memory 6.0.0-test8 Gap Matrix（草案）

> 文档状态：test8 设计与实现前差距盘点，等待源码实现后由主代理合并。  
> 证据范围：`test0`—`test7` 实施文档、6.0 总纲、测试系列索引、`REPORT_600_TEST7.md`，以及当前工作区源码静态检查。  
> 本文只覆盖 `test0`—`test8`。不把后续阶段门槛写成当前完成条件。

## 1. 结论摘要

当前工作区已经出现 test8 的部分数据结构和算法骨架，但尚不能据此宣称 test8 完成：

- `consistency_guard.py` 有确定性检查、候选项和有限 LLM 仲裁校验的雏形；
- `consistency_service.py` 有请求快照 / 回答配对、后台线程、队列容量、TTL、超时和 fail-open 的雏形；
- `thread_store.py` / `episodic_store.py` 已有一致性观测表及读写代理；
- 但在当前静态检查中，没有确认主请求 / 回答生命周期已把 test7 的真实注入快照和最终回答提交到 `ConsistencyService`；
- `_conf_schema.json` 尚未暴露 test8 的专用配置；
- `thread_webui.py` / `webui.py` 尚未提供一致性观测、证据对照、标签反馈和匿名样本导出闭环；
- 现有 guard 规则仍偏向“回答复述注入文本”的狭窄条件，不能覆盖 test8 设计要求的全部关系、阶段、状态、前瞻和现实性判断；
- 尚无 test8 专项测试、真实错误样本集、人工标签数据或可据此计算的 precision / recall / 误报分类结果。

因此当前结论应为：**test8 处于实现草稿 / 部分骨架状态，不是已验收版本。**

## 2. 证据等级定义

| 等级 | 含义 | 本文使用方式 |
| --- | --- | --- |
| E0 | 设计意图或阶段契约 | 来自总纲、test0—test8 文档；不能证明源码已实现 |
| E1 | 源码静态可见 | 可定位到类、函数、表或配置；不能证明运行时链路已接通 |
| E2 | 隔离单元 / 确定性测试证据 | 需要实际运行 test8 专项测试并保存结果；当前不默认存在 |
| E3 | 本地只读副本或真实运行时证据 | 需要真实请求、真实快照、真实回答和持久观测回链；不能用源码或模拟数据替代 |
| E4 | 人工标注后的效果证据 | 需要真实错误 / 正确样本、误报分类和独立标注；当前没有 |

## 3. 按 test0—test7 前序依赖的缺口

| 前序阶段 | 已有证据 | 对 test8 的依赖 | 当前缺口 / 风险 | 需要补齐的证据 |
| --- | --- | --- | --- | --- |
| test0 数据地基 | E1：一致性表、请求观测表、线程派生表存在；E0：要求派生数据可删可重建、源数据不改 | 必须能以稳定 `request_id` 关联查询、注入快照、回答和发现 | 一致性表目前只保存部分 finding 字段；需要确认 snapshot 的来源、完整性、版本和过期策略 | E2：迁移 / 重启 / 幂等；E3：真实请求 ID 全链路回链、源数据未改 |
| test1 本地关系 | E1：线程边、来源和状态字段可被检索 | 守卫不能重新猜测关系，必须消费已冻结的关系与证据 | 尚未证明回答判断使用的是 test7 实际注入对象，而非二次检索或文本猜测 | E2：固定 snapshot fixture；E3：线上快照与注入内容一致 |
| test2 歧义仲裁 | E1：后台 LLM runtime / 线程仲裁存在；E0：一致性 LLM 只能处理少数高风险歧义 | 仲裁输入必须是回答片段、当前 claim、必要证据的最小集合 | `ConsistencyService` 虽有 callback，但当前未证明已配置到真实 Provider、共享 runtime 和活动事件循环 | E2：超时、无效 JSON、Provider 失败；E3：实际后台任务不延迟发送 |
| test3 Claim Ledger | E1：claims、transition、resolved 等表和查询存在 | 当前状态、历史状态、superseded / resolved / uncertain 必须区分 | guard 读取 `status` 的能力有限，不能证明能从 claim transition 判断“当时成立 / 现在失效” | E2：状态转换 fixture；E3：发现回链 claim → Episode → turn |
| test4 前瞻记忆 | E1：prospective 表、触发逻辑和 test7 查询结果存在 | 需识别已解决事项、未决事项、未来事项的错误使用 | 当前 guard 没有明确验证 prospective 对象、due window、cooldown 或“应浮现 / 不应浮现” | E2：resolved / pending / due / uncertain 样本；E3：从 test7 snapshot 回链 |
| test5 检索实验室 | E1：四路检索和最小子图代码存在 | 只检查回答是否使用实际注入线程 / claim / prospective，不检查未注入内容 | 尚未证明 snapshot 保存了最终注入的完整证据集合、裁剪原因和版本 | E2：检索实验室与守卫 snapshot 对照；E3：实际 ProviderRequest 旁路验证 |
| test6 同步与架构 | E1：共享 LLM runtime 和 thread integration 已拆出；E0：所有后台调用 fail-open | 回答守卫不能与前台 Provider 争抢或阻塞 | service 使用独立 worker；LLM callback 必须回活动 loop，但当前主插件没有可见的提交 / 配置 / 关闭闭环 | E2：生命周期、reload、close、loop 关闭；E3：Provider lease / 断路和发送延迟证据 |
| test7 Canary 注入 | E1：`_compose_thread_canary_for_request` 会记录 query observation，test7 报告给出 300 次只读副本结果 | test8 必须在模型回答完成后旁路读取同一 `request_id` 的 test7 快照 | 当前 `on_llm_response` 可见 ACCESS 回写与压缩流程，但静态检查未发现调用 `submit_response`；无法证明回答与 test7 快照已配对 | E2：Shadow / Canary / fail-open 配对测试；E3：真实回答后持久化 consistency observation |

## 4. test8 设计要求与当前源码差距

| test8 设计项 | 设计证据（E0） | 当前可见实现（E1） | 差距判断 | 合并前验收证据 |
| --- | --- | --- | --- | --- |
| 三层守卫 | test8 实施文档 §三层检查；总纲 §10 | `evaluate` 有本地规则、`candidates` 和 `validate_arbitration` | 部分实现；LLM 仲裁输入和结果未证明接入统一证据协议 | E2：三层分别可测；E3：高风险项异步完成且原回答不变 |
| 历史状态误作当前 | test8 检查对象 | `_HIST` / `_NOW` 只在共享文本条件下判断 | 规则容易漏掉无共同长短语、阶段变化写在 claim 而非文本的情况；也可能把“回顾历史”误判为当前冲突 | E2：引用历史、当前结论、阶段变化、否定和转述矩阵；E4：误报分类 |
| 日期 / 事件顺序 | test8 检查对象；test7 时间字段契约 | 仅有有限日期正则和兼容判断 | 不能完整判断相对时间、区间、时间精度、事件顺序及“先后不等于因果” | E2：明确日期、年月、模糊区间、相容精度；E3：回链原始 event time |
| 因果关系 | test8 检查对象 | `_CAUSAL` 找到回答中的因果词后生成 candidate | 仅能发现回答含因果词且证据文本不含因果词；不能验证证据图中的 `causes` / `responds_to` 及反证 | E2：明确因果、仅先后、反事实和无关相似；E4：因果 precision |
| parallel / unrelated_similar 不合并 | test8 检查对象 | guard 没有读取边类型的明确路径 | 不能从 test7 子图阻止两个相似事件被回答合并 | E2：同主题不同日期 / 人物样本；E3：边证据可追溯 |
| active 边界 / 身份 | test8 检查对象 | 当前代码主要按引用文本和否定词判断 | 没有稳定 claim slot、active 边界和作用域的直接对齐 | E2：边界、身份、昵称、作用域冲突；E3：claim → Episode → turn |
| resolved 事项误作 pending | test8 检查对象 | 有 `resolved` / `completed` 字符串判断 | 依赖文本字面状态，不能保证读取 claim transition 或 prospective 解决证据；状态枚举未统一 | E2：resolved / broken / superseded / pending 全组合；E3：真实解决证据回链 |
| 强相关前瞻事项 | test8 检查对象 | 当前 guard 没有 prospective 触发结果对齐 | 不能区分“回答忽略前瞻”与“该事项本轮不应浮现” | E2：触发路线和 cooldown fixture；E4：应浮现 / 不应浮现人工标签 |
| 梦境 / 假设 / 转述 / 角色台词 | test8 误报控制 | 有 `_REALITY`、`_SOFT` 和 status 排除 | 规则只检查有限标记，不能稳健区分嵌套引用、角色内台词和回答现实陈述 | E2：多层引号、角色扮演、假设、梦境、转述；E4：误报类型 |
| 只检查回答实际陈述 | test8 误报控制 | `_shared_specific` 和 soft 词过滤 | 共享文本长短语不是 claim 对齐；情绪表达 / 复述 / 引用边界仍可能误判 | E2：无事实回答、纯情绪、引用历史、条件句；E4：人工正确性 |
| 不确定性 | test8 要求 uncertain 不定罪 | `status in {uncertain,hypothetical,dream}` 直接跳过 | “跳过”丢失不确定性观测；不能展示分歧、来源质量和决定理由 | E2：uncertain 留痕；E3：WebUI 显示不确定而非 clean |
| 来源等级 | test0—test7 均要求 diary-derived 降级 | `_finding` 有 `category`，但来源质量未形成统一等级判断 | 低质量日记、Episode、原文来源的置信度上限未落实 | E2：A/B/C/D 或等价来源矩阵；E4：按来源分层指标 |
| 完整回答旁路 | test8 §Shadow 执行点 | `check_response` 可截断到 `max_chars` | 需要明确截断标记；不能把被截断回答当作完整 clean | E2：长回答 / 截断；E3：不打印完整回答且可审计摘要 |
| 同步发送零阻塞 | test8 §Shadow 执行点 | service 队列是异步 worker；LLM 使用 future timeout | 未证明提交点为非阻塞，也未证明本地超时后仍保存“跳过”观测 | E2：队列满、超时、关闭、Provider 失败；E3：发送 latency 分布 |
| 观测关联 | test7 §观测记录、test8 §记录 observation | `request_id` 配对机制存在 | 未证明 snapshot 和 response 必然到达、TTL 后如何处理、重复提交是否完全幂等 | E2：乱序、重复、缺一端、过期；E3：真实持久记录 |
| WebUI 证据三栏 | test8 WebUI 要求 | 现有 threads API 可读 query observation / feedback | 没有确认一致性 observation 专用 GET、三栏对照、筛选和趋势 | E2：真实 API route tests；E3：页面读取真实数据库 |
| 人工标签 | test8 要求真实错误 / 误报 / 不确定 / 无关 | 有通用 `thread_manual_feedback` | 尚未证明 action 枚举、目标类型、标签统计、版本筛选和可回滚语义满足一致性评测 | E2：四类标签 CRUD / 反馈审计；E4：标注一致性 |
| 匿名导出 | test8 WebUI / 隐私要求 | 现有 ACCESS 有导出代码，但未证明一致性导出 | 不能复制完整回答、Token 或旧报告；需要最小化匿名样本 schema | E2：敏感字段扫描；E3：导出内容来自真实观测 |

## 5. 配置与数据契约缺口

### 5.1 需要明确、但当前 schema 未确认的 test8 配置

建议由源码实现阶段统一增加并校验以下配置；名称可由主代理按现有命名约定调整，但不能只在代码中散落默认值：

- `consistency_mode`：`off` / `shadow`，默认 `shadow`；
- `consistency_timeout`：本地检查总预算；
- `consistency_queue_capacity`：有限队列容量；
- `consistency_ttl`：未配对 / 观测保留期限；
- `consistency_llm_enable`：默认关闭或仅后台启用；
- `consistency_llm_provider_id`：Provider 下拉选择；
- `consistency_llm_timeout` / `consistency_llm_daily_budget`；
- `consistency_max_response_chars`：截断但必须显式标记；
- `consistency_observation_keep` 或等价清理策略；
- `consistency_require_injected_snapshot`：没有 test7 快照时只记录 skip，不猜测；
- `consistency_exclude_commands`：指令、插件管理和系统消息排除。

当前 `E1` 只能确认 `EpisodicStore` 构造参数和 service 内部字段存在，不能确认这些键已从 AstrBot 配置加载、持久化和在 WebUI 中可见。

### 5.2 建议的最小快照协议

回答守卫输入应为不可变、版本化的 test7 实际快照，至少包括：

```text
request_id
scope_id / session_id
query_text（受留档上限保护）
query_plan
current_time_context（及有效性）
injected_references[]
  - stable source / episode / claim / prospective reference
  - event_time 与时间依据
  - status / relation / source_quality
  - text 或受限证据片段
injection_preview / content hashes
thread_used
composer / schema / plugin version
snapshot_complete
```

最终回答提交只需增加：

```text
request_id
answer_hash
answer_text（仅后台受限使用，不进普通日志）
response_complete / truncated
response_source（正常模型回答）
```

找不到完整 snapshot 时，必须产生可解释的 `skipped` 观测，而不是把“无证据”当成“回答正确”。

## 6. 真实效果限制（必须写进实现 / 报告）

1. **Shadow 发现不是用户效果。** 记录错误只说明守卫发现了候选，不证明模型因守卫而改进；因为 test8 不修改、重生成或拦截回答。
2. **规则 finding 不是真实错误标签。** 需要人工判断引用、条件句、梦境、角色台词、历史回顾和当前断言，否则 precision 会被高估。
3. **有注入证据只能测“已注入内容的一致性”。** 守卫不能证明模型是否看到了未注入的事实，也不能评价没有进入 snapshot 的记忆遗漏。
4. **无 finding 不等于回答正确。** 规则可能漏掉隐式关系、跨句因果、代词指代、相似事件合并和语义状态冲突。
5. **LLM 仲裁结果不能当金标准。** LLM 只能作为少量高风险候选的第三层证据，仍需人工标注和证据回链。
6. **test7 的 5.1 intermediate 限制会传导到 test8。** 当前数据只代表 5.1 中间主路下的注入分布，不能直接外推到未来完整 5.x 主路。
7. **小样本 Canary / 单一角色不代表长期效果。** 当前 test7 报告的 163 episodes、300 次查询和 5% 建议比例可支持链路观察，但不能支持回答错误率的稳定估计。
8. **首轮缓存、Provider、语言和角色扮演风格会影响误报。** 真实效果需按模型、线程类型、来源质量、版本和语言场景分层，而不能只报一个总数。
9. **回答文本截断会造成观测偏差。** 被截断的长回答只能标记为 partial observation，不得计入完整 clean 或完整 recall。
10. **观测采集本身可能丢样本。** 队列满、进程退出、快照与回答乱序、TTL 清理和 SQLite 短暂锁定都需要独立计数；不能用“未记录”冒充“无错误”。
11. **一致性错误与记忆错误要分开。** 如果注入 claim 本身错误，回答与注入一致不代表事实正确；报告必须同时保留 source quality、claim 状态和人工判定。
12. **不进入长期记忆。** test8 observation、候选、LLM 判断和人工反馈是评测派生数据，不应写回 Episode、日记、滚动状态或前瞻事实。

## 7. test8 实现合并清单

- [ ] 在 test7 注入完成处冻结并提交完整 snapshot，使用相同 `request_id`；
- [ ] 在回答完成旁路提交 response，提交动作不等待本地检查或 LLM；
- [ ] 处理 snapshot / response 乱序、重复、缺失、过期、队列满和关闭；
- [ ] 本地规则只使用注入证据和结构化状态，区分 finding、candidate、uncertain、skipped；
- [ ] 统一错误类型、风险等级、置信度、决定来源、证据级别和规则版本；
- [ ] 第三层 LLM 只接受最小输入，必须在共享 AstrBot 活跃事件循环中运行，并有独立预算 / 超时 / 断路；
- [ ] LLM 失败保留本地结果或 `incomplete`，不覆盖为 clean；
- [ ] 一致性 observation 保存可回链的回答片段、证据片段、来源 ID / hash、快照版本和时间；
- [ ] 观测表不保存超出隐私策略的完整回答，普通日志不打印回答全文；
- [ ] WebUI 提供真实 API：列表、详情、筛选、趋势、query / evidence / response 对照、标签和匿名导出；
- [ ] test8 专项测试覆盖设计文档列出的 8 类基础样本及异常路径；
- [ ] 只读副本验证至少证明：原回答不变、发送不等待、注入快照可回链、观测持久化且源数据不变；
- [ ] 报告区分 E0 / E1 / E2 / E3 / E4，不把设计、源码存在和用户效果混写。

## 8. 当前阶段判定

在源码补齐前，test8 只能标记为：

- **设计：明确**（E0）；
- **骨架：部分存在**（E1）；
- **请求 / 回答真实接线：未证实**；
- **WebUI 评测闭环：未证实**；
- **真实效果：无足够证据**（缺 E3 / E4）；
- **当前完成状态：未验收，不应进入任何扩大 Canary 或自动修复判断。**

本矩阵不定义后续阶段门槛；待源码实现和 test8 报告证据补齐后，再由主代理决定是否更新系列状态。
