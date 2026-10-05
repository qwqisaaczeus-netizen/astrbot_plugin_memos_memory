# AstrBot Memos Memory 6.0.0-test8 实施草稿

> 状态：等待源码完成后由主代理合并。本文是实现契约，不是完成报告。  
> 范围：只覆盖 `test0`—`test8`。不以 `test9` 作为当前完成条件，也不提前定义后续阶段放行。

## 1. 阶段定位与设计升级

`test8` 在 test7 的真实注入快照之后增加**回答一致性 Shadow 守卫**。守卫只观察模型已经生成的回答：

```text
冻结 test7 snapshot
  -> 正常发送原回答
  -> 后台配对 response
  -> 本地确定性检查
  -> 少量高风险 LLM 仲裁
  -> 持久化 observation / candidate / feedback
```

它不修改 `ProviderRequest`，不重生成回答，不拦截发送，不把 observation 写进长期记忆、Episode、日记、滚动状态或 prospective 状态。

本版必须明确的设计升级：

1. 从“代码中有规则”升级为“规则只针对本轮实际注入的证据”；
2. 从单一风险分数升级为 `finding / candidate / uncertain / skipped / incomplete` 的可解释结果；
3. 从回答钩子单点调用升级为 request snapshot 与 response 的乱序可配对后台服务；
4. 从结果落库升级为 query、注入证据、回答片段三方可回链；
5. 从展示风险升级为可人工标注真实错误、误报、不确定和无关，并区分来源质量与决定路线；
6. 从“无报错”升级为可测的零发送阻塞、fail-open、队列丢弃和观测缺失统计；
7. 从单一 LLM 评判升级为本地硬证据优先、异步少量 LLM 仲裁，LLM 不作金标准。

## 2. 前序契约

### 2.1 test7 输入契约

守卫不得重新运行 5.1 召回、embedding、rerank 或线程检索来猜测模型可能看到的内容。它只消费 test7 在同一 `request_id` 下冻结的快照：

- `scope_id`、`session_id`、`request_id`；
- query 和 QueryPlan（按留档上限）；
- 当前时间块及有效性；
- 实际注入的 thread / claim / prospective / Episode / passage / source evidence；
- 每个证据的稳定来源、事件时间、时间依据、状态、来源质量和文本片段或 hash；
- 去重结果、裁剪 / 保护原因、composer / schema / plugin 版本；
- `thread_used`、`snapshot_complete`。

快照不完整时必须是可解释的 `skipped`，不能把没有证据当成回答正确。

### 2.2 response 输入契约

回答完成后只提交：

- `request_id`；
- 回答文本的受限后台副本或受限片段；
- `answer_hash`、字符数、`response_complete` / `truncated`；
- 正常模型回答来源和时间。

提交动作不得等待守卫检查、SQLite 写入或 LLM 仲裁。

### 2.3 生命周期契约

- 指令、插件管理消息和系统提示不进入回答一致性评测；
- snapshot / response 可以任意顺序到达，重复提交幂等；
- 缺少一端、TTL 到期、队列满、服务关闭和 SQLite 异常都单独计数；
- 服务终止前先停止接收、排空已接受任务并安全关闭，不能使用已关闭的 SQLite；
- 外部 Provider 调用必须经共享 AstrBot 活跃事件循环、超时和后台 lane；
- 任一异常 fail-open，不改变用户可见回答。

## 3. 三层检查协议

### 3.1 第一层：确定性检查

确定性规则只报告能够由快照证据明确支持的冲突：

- 历史阶段被回答表述为当前状态；
- 明确日期或事件顺序冲突；
- `resolved / completed` 被说成 pending，或明确状态被反向断言；
- active 身份、边界、称呼或关系位置被违背；
- 注入边标记为 `parallel` / `unrelated_similar`，回答却把事件合并；
- 回答声称因果，但快照只有先后关系或没有 `causes` / `responds_to` 证据；
- 梦境、假设、转述、引用或角色台词被回答当成现实事实。

规则要求：

- 只比较注入证据，不能仅因回答与未注入记忆不一致就报警；
- 先判断回答是否真正陈述该事实；纯情绪、复述、条件句和历史回顾不能直接作为当前断言；
- 保留否定、条件、引号和时态边界；
- 时间精度不同但区间相容时不判冲突；
- 证据 `uncertain`、低质量 diary-derived 或状态冲突时降低置信度或返回 uncertain；
- 每条结果带 `error_type`、`severity`、`confidence`、`decision_source=rule`、`rule_version`、证据引用和回答片段。

### 3.2 第二层：证据一致性

将回答中的候选 claim 与本轮实际注入的：

- current claim；
- historical / superseded / resolved claim；
- claim transition；
- Episode 关系边；
- prospective 状态、due window、cooldown 和解决证据；
- source quality 与 source turn；

进行对齐。候选 claim 不是开放式回答质量评价，而是回答是否使用并改写了特定注入事实的可审计比较。

输出状态至少包括：

- `clean`：在检查范围内没有发现冲突，不代表回答事实完整正确；
- `flagged`：存在可回链冲突；
- `review`：有高风险候选但规则不能定案；
- `uncertain`：证据本身或当前状态不足；
- `skipped`：没有完整快照、回答为空、被排除或超预算；
- `incomplete`：本地结果存在但异步 LLM 失败 / 超时。

### 3.3 第三层：LLM 高风险仲裁

只处理强冲突且本地规则无法判断、同时确认回答使用了该线程的少量候选。输入最小化为：

- 回答相关片段；
- 当前候选 finding；
- current / historical claim；
- 必要 Episode / source evidence；
- relation / status / time metadata；
- 允许 verdict 枚举和证据引用规则。

不发送完整对话、完整日记合集或无关历史。LLM 输出必须经过本地二次校验：

- JSON 结构、枚举和置信度合法；
- `response_quote` 确实存在于回答；
- evidence ID 确实存在于本轮 snapshot；
- 不突破作用域、时间和人工锁硬约束；
- 无法验证时保留本地结果并标记 `incomplete` / `uncertain`，不得覆盖为 clean。

默认第三层后台运行、独立 timeout、每日预算、有限重试和断路；不得延迟发送。

## 4. 观测与存储契约

### 4.1 记录内容

`thread_consistency_observations` 至少能回链：

- `request_id`、`scope_id`、创建时间；
- `error_type`、描述、风险等级、置信度；
- `decision_source`：rule / evidence / llm / manual；
- `rule_version`、snapshot / composer / plugin 版本；
- 回答受限片段或 hash；
- evidence / claim / Episode / source turn 的稳定标识与受限片段；
- 来源质量、时间依据、是否 uncertain；
- `response_truncated`、`observation_status` 和 fail-open / skip 原因。

若为隐私或存储策略不允许，应保存不可逆 hash 与最小化片段，但不能失去证据关系。

### 4.2 request observation 回填

同一 request 的 `thread_request_observations` 应回填：

- answer hash、字符数和截断状态；
- response status；
- consistency status；
- 本地耗时、LLM 是否使用、LLM 状态；
- snapshot 是否完整、是否配对、是否过期或丢弃。

### 4.3 观测生命周期

观测是评测派生数据。保留期可配置并支持清理；清理不得删除 Episode、日记、原文或 claim。队列满和过期丢样本必须作为计数呈现。

## 5. 配置草案

配置名称可由主代理按现有约定调整，但必须有 schema、运行时读取、边界校验和 WebUI 可见性：

| 配置 | 默认建议 | 约束 |
| --- | --- | --- |
| `consistency_mode` | `shadow` | 仅 `off` / `shadow`；本版不开放修复 |
| `consistency_timeout` | `0.25s` | 本地检查总预算，超时跳过 |
| `consistency_queue_capacity` | `64` | 有界，满时不阻塞提交 |
| `consistency_ttl` | `900s` | 配对和观测的保留期限 |
| `consistency_llm_enable` | `false` | 开启也只能后台仲裁 |
| `consistency_llm_provider_id` | 空 | 空值按既有 Provider fallback |
| `consistency_llm_timeout` | 小于等于后台预算 | 不得阻塞回复 |
| `consistency_llm_daily_budget` | `0` 或受控小值 | 预算耗尽保留候选 |
| `consistency_max_response_chars` | 受限值 | 超限必须标记 truncated |
| `consistency_observation_keep` | 受控值 | 仅清理观测派生数据 |
| `consistency_require_injected_snapshot` | `true` | 无快照只 skip，不猜测 |
| `consistency_exclude_commands` | `true` | 与 test7 命令排除契约一致 |

## 6. WebUI 契约

一致性页面必须是工作台，不是静态装饰：

1. 概览：总观测、checked / clean / flagged / review / skipped、风险等级、LLM 状态、队列丢弃和趋势；
2. 三栏详情：query / 实际注入证据 / 回答片段；
3. 证据抽屉：Episode、claim、transition、relation、prospective、source quality、时间依据和决定来源；
4. 筛选：模型、线程类型、来源质量、版本、error type、severity、decision source、标签；
5. 人工反馈：`true_error`、`false_positive`、`uncertain`、`irrelevant`，写入 `thread_manual_feedback`，带 operator、时间、目标和可回滚记录；
6. 匿名导出：只导出最小化回答片段 / hash、证据类型、风险和版本，不导出 Token、Provider 凭据、完整会话、完整日记或运行数据包；
7. 所有数量与详情必须来自真实 API；空库显示空状态，不显示硬编码样本。

建议 API：

- `GET /api/threads/consistency/overview`；
- `GET /api/threads/consistency/observations`；
- `GET /api/threads/consistency/detail?id=...`；
- `POST /api/threads/consistency/feedback`；
- `POST /api/threads/consistency/export` 或等价的受控下载接口。

## 7. 测试矩阵

### 7.1 规则与证据

1. 明确旧状态被回答说成当前；
2. 正确引用旧状态并保留历史边界；
3. 日期相反、年月级精度差异、相容区间；
4. 事件先后但无因果，以及有明确因果；
5. `parallel` / `unrelated_similar` 两个事件不能合并；
6. active 边界、身份、昵称和作用域冲突；
7. resolved / completed 事项被说成 pending；
8. pending / due / uncertain prospective 的应浮现与不应浮现；
9. 梦境、假设、转述、角色台词、引号和现实陈述；
10. 纯情绪、条件句、问题句和未使用线程的回答不误报；
11. 有原文、Episode 无原文、只有日记、日期不完整的来源分级；
12. 长回答、截断、空回答和不完整快照。

### 7.2 服务与生命周期

1. snapshot 先到、response 先到、重复提交和 request_id 冲突；
2. 缺少一端、TTL 过期、队列满、服务关闭；
3. 本地 evaluator 超时；
4. Provider 超时、无效 JSON、异常、预算耗尽；
5. SQLite 短暂锁定和观测写入失败；
6. reload / terminate 后无线程、任务或连接泄漏；
7. 指令、插件管理和系统提示被排除；
8. Shadow 不修改原回答、ProviderRequest 或长期记忆。

### 7.3 WebUI 与隐私

1. GET / POST 使用真实数据库；
2. 四类人工标签可写入、读取和审计；
3. 三栏详情可从 request_id 回链；
4. 筛选和趋势不混淆版本、模型和来源质量；
5. 匿名导出不含 Token、完整回答、完整日记和临时运行数据；
6. 空库和数据库损坏返回结构化空状态 / 错误。

## 8. 证据与报告规则

报告必须逐项标记：

- E0：设计文档要求；
- E1：源码静态存在；
- E2：隔离单元 / 回归测试；
- E3：只读副本或真实运行时的请求—快照—回答—观测回链；
- E4：人工标注后的 precision、recall、误报类型和校准。

禁止以下替换：

- 用表存在替代真实数据回链；
- 用 `ConsistencyGuard` 类存在替代 hook 接线；
- 用规则 finding 数量替代真实错误数量；
- 用 LLM verdict 替代人工金标准；
- 用 test7 的注入延迟 / 召回数据替代 test8 的回答错误效果；
- 用静态页面或硬编码 JSON 替代真实 WebUI API。

## 9. 完成定义（仅 test8）

在 test8 报告中，至少需要证明：

1. test7 实际注入快照和最终回答以同一 request_id 配对；
2. 原回答在 Shadow 下字节 / 语义不变，发送不等待检查和 LLM；
3. 三层检查、uncertain、skipped、incomplete 和来源等级可审计；
4. 高风险发现能回链到本轮注入证据，且不把未注入事实当证据；
5. 队列、TTL、超时、Provider 失败、关闭和 SQLite 失败均 fail-open；
6. WebUI 三栏、筛选、反馈和匿名导出使用真实 API；
7. test8 专项测试覆盖本草案矩阵；
8. 报告明确区分“观测到风险”和“人工确认真实错误”；
9. 没有宣称真实用户效果已改善，除非有 E3 / E4 支撑；
10. 源数据、Memos、Episode、日记和长期状态未被 test8 观测流程改写。

未满足的项目必须列入 gap matrix，并标明证据等级和下一步，不得用“代码已存在”作为完成理由。
