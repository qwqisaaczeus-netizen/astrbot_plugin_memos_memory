# AstrBot Memos Memory 6.0.0-test8 设计 / 实施报告草稿

> 报告状态：草稿，等待源码完成、主代理合并和 test8 验证后更新。  
> 当前范围：`test0`—`test8`。不把 `test9` 门槛写成当前完成条件。  
> 当前判定：**未验收，不应宣称 test8 已完成或真实效果已改善。**

## 1. 摘要

test8 的设计升级是在 test7 的 Canary 注入之后增加回答一致性 Shadow 守卫。它只检查模型已经生成的回答是否误用历史状态、时间、承诺、关系、事件顺序、前瞻事项或现实性边界；不修改回答、不重生成、不拦截发送。

当前工作区可见部分 test8 骨架：

- `consistency_guard.py`：本地规则、候选 finding、有限仲裁结果校验；
- `consistency_service.py`：snapshot / response 配对、后台 worker、有限队列、TTL、超时和 fail-open 结构；
- `thread_store.py` / `episodic_store.py`：一致性观测表和读写代理。

但静态检查尚未证明：

- test7 实际注入快照和最终回答已在真实生命周期中提交并配对；
- 一致性配置已接入 `_conf_schema.json` 和主插件运行时；
- WebUI 已提供一致性列表、三栏证据对照、筛选、趋势、人工标签和匿名导出；
- 三层检查覆盖 test8 设计的全部错误类型；
- 有真实运行时、人工标注或用户效果证据。

所以本稿只记录可验证事实、缺口和实施后报告模板，不把设计意图写成完成结果。

## 2. 范围与非目标

### 覆盖

- test0 的派生数据、观测、幂等和 fail-open 基础；
- test1 的本地关系证据与 `parallel` / `unrelated_similar` 区分；
- test2 的异步歧义仲裁边界；
- test3 的 Claim Ledger、状态有效期和不确定性；
- test4 的前瞻记忆与冷却 / 解决状态；
- test5 的四路线检索与最小充分子图；
- test6 的 5.1 intermediate 同步、共享 Runtime、架构拆分；
- test7 的稳定 Canary、实际注入快照、跨层去重和时间边界；
- test8 的回答一致性 Shadow 检查、观测和人工评测准备。

### 非目标

- 不改变 5.1 主召回或 test7 ProviderRequest；
- 不自动修复、重生成或拦截回答；
- 不把 test8 observation 写入长期记忆、Episode、日记、滚动状态或 prospective；
- 不复制数据库、运行数据、旧报告或真实回答到插件目录；
- 不以 `test9` 的真实数据校准、年月级性能拟合或任何后续阶段门槛作为本版完成条件。

## 3. 设计升级核对

| 升级点 | 设计要求 | 当前证据 | 状态 |
| --- | --- | --- | --- |
| 证据范围 | 只对 test7 实际注入内容检查 | E0；E1 有 `_references` / `thread_text` 路径 | 需 E3 证明真实 snapshot 来源 |
| 三层检查 | 确定性 → 证据一致性 → 少量异步 LLM | E1：guard / service 雏形 | 部分存在，需专项测试与接线 |
| 发送旁路 | 检查和仲裁不延迟发送 | E1：后台队列结构 | 未证明回答 hook 非阻塞 |
| 状态语义 | current / historical / superseded / resolved / uncertain 分开 | E1：claim / transition 表存在 | guard 对结构状态读取不足 |
| 前瞻语义 | pending / due / resolved / cooldown 可解释 | E1：prospective 模块存在 | 未接入回答检查对齐 |
| 现实性边界 | 梦境、假设、转述、角色台词不等于现实 | E1：少量正则 / status 排除 | 需覆盖嵌套引用与误报 |
| 跨层证据 | query / evidence / response 三栏回链 | E1：request observation 与 finding 表存在 | WebUI 闭环未证实 |
| 人工校准 | true error / false positive / uncertain / irrelevant | E1：通用 feedback 代理存在 | 一致性目标类型与统计未证实 |
| 隐私 | 不打印完整回答，不导出 Token / 完整会话 | E0；代码路径有限截断 | 需敏感字段测试 |

## 4. 证据等级与当前证据

| 等级 | 定义 | 当前 test8 证据 |
| --- | --- | --- |
| E0 | 文档设计或阶段契约 | test8 实施文档、总纲 §10、test0—test7 契约 |
| E1 | 源码静态可见 | `consistency_guard.py`、`consistency_service.py`、一致性表 / 代理 |
| E2 | 隔离测试实际通过 | 当前稿不填数字；待 test8 专项测试运行后填入 |
| E3 | 真实运行时 / 只读副本端到端证据 | 当前未确认；必须有真实 request snapshot → response → observation 回链 |
| E4 | 人工标注效果证据 | 当前没有；必须有人工标签、混淆矩阵、precision / recall 和误报分类 |

源码静态存在不等于生命周期接通，表存在不等于真实观测写入，finding 数量不等于真实错误数量，LLM 判定不等于人工金标准。

## 5. 前序缺口对 test8 的影响

### test0—test2：证据和关系

test8 必须读取已有 Episode、关系边和证据，而不是重新用文本猜测。若 test7 snapshot 没有保存关系类型、来源质量、时间依据和证据 ID，`parallel`、`causes`、`retells` 等错误无法可靠判断。

### test3：当前事实

回答与注入内容一致，仍不代表注入 claim 本身正确。报告必须同时保存 claim 的状态、转换、来源等级和人工标签，避免把“与错误记忆一致”计为事实正确。

### test4：前瞻

未浮现不是必然错误；只有在本轮触发门槛满足、事项仍 pending / due 且应在 snapshot 中出现时，才能把回答忽略列为候选。resolved 或 cooldown 状态必须排除。

### test5：最小充分子图

回答守卫只评价实际注入子图，不评价未注入的全库事实。漏召回仍是检索问题，不能伪装成回答一致性问题。

### test6：Runtime 与生命周期

LLM 仲裁必须让出前台 Provider lease，回到 AstrBot 活跃事件循环，并有独立预算、超时和断路。worker、event loop、SQLite 的关闭次序也必须在测试中证明。

### test7：Canary 与上游限制

test7 报告证明了 163 episodes、300 次查询的连续性组合和主路保持，但这不是回答错误效果证据。当前上游是 `5.1 intermediate`；test8 样本分布不能直接外推为未来完整 5.x 的用户效果。

## 6. 实现后应填充的测试结果

以下表格在源码完成并实际运行后填写；当前不虚构数字。

| 测试组 | 目标 | 结果 | 证据等级 | 备注 |
| --- | --- | ---: | --- | --- |
| guard 基础规则 | 日期、状态、边界、关系和现实性样本 | 待运行 | E2 | 需正例 / 反例 / 误报样本 |
| claim / transition | current、historical、resolved、uncertain | 待运行 | E2 | 需证明不静默覆盖 |
| prospective | pending、due、resolved、cooldown | 待运行 | E2 | 区分应浮现和不应浮现 |
| 长回答 / 截断 | response_truncated 观测 | 待运行 | E2 | partial 不计完整 clean |
| snapshot / response 配对 | 乱序、重复、缺一端、TTL | 待运行 | E2 | request_id 幂等 |
| 非阻塞 | queue full、local timeout、LLM timeout | 待运行 | E2 | 发送不等待 |
| Provider 失败 | 无效 JSON、异常、预算耗尽 | 待运行 | E2 | 保留候选或 incomplete |
| 生命周期 | reload、terminate、SQLite 关闭 | 待运行 | E2 | 无任务 / 连接泄漏 |
| 命令排除 | 指令、插件管理、系统提示 | 待运行 | E2 | 不进入评测 |
| WebUI | GET / POST / 三栏 / 筛选 / 反馈 / 导出 | 待运行 | E2 | 数据来自真实 API |
| 只读副本回链 | 真实 snapshot → response → observation | 待运行 | E3 | 不写源数据 |
| 人工标注效果 | precision、recall、误报分类 | 待运行 | E4 | 需独立标注协议 |

## 7. 真实效果限制

1. Shadow 只能观察，不能证明用户回答因此变好。
2. 规则 finding 是候选风险，不是人工确认错误。
3. `clean` 只表示在受限证据和规则范围内没有发现冲突，不表示回答完整或事实正确。
4. 没有 snapshot 的回答不能评价为正确；应记 `skipped` / `missing_evidence`。
5. 守卫看不到未注入内容，不能替代召回 Recall 或前瞻 Recall 评测。
6. LLM 仲裁是第三层辅助，不是金标准，也不能替代人工标签。
7. 只有日记或日期不完整的旧数据只能降低置信度，不能制造精确事实。
8. 5.1 intermediate、Canary 小样本、单角色、单语言和单 Provider 结果不能外推全部用户。
9. 队列丢弃、TTL、进程退出和配对失败会形成观测缺口，缺失样本不能计为 clean。
10. 截断回答会形成 partial observation，不能计入完整正确率。
11. 回答与错误 claim 一致不代表真实世界正确，必须保留来源质量和人工判定。
12. test8 不提供自动修复，因此任何“错误率下降”都必须来自可比的观察 / 标注研究，而非守卫本身的存在。

## 8. 待主代理合并的阻塞项

- [ ] 接入 test7 snapshot 提交点；
- [ ] 接入最终回答提交点，并保持非阻塞；
- [ ] 统一配置 schema、运行时参数、WebUI 配置展示；
- [ ] 完善错误类型、风险等级、来源质量、版本和快照字段；
- [ ] 完成第三层 LLM 的共享 Runtime / timeout / budget / circuit；
- [ ] 增加一致性 WebUI 真实 API 和四类人工标签；
- [ ] 增加匿名导出并扫描敏感字段；
- [ ] 编写并运行 test8 专项测试；
- [ ] 在只读副本做端到端回链，记录发送延迟、丢弃和 fail-open；
- [ ] 以 E4 人工标签替换“源码 finding 数量”的效果表述；
- [ ] 同步 gap matrix 后再判断 test8 是否可标记完成。

## 9. 当前结论

当前可报告的最强结论是：**test8 设计已明确，源码存在部分一致性守卫与后台服务骨架，但真实生命周期接线、WebUI 评测闭环、专项测试和真实效果证据尚不足；test8 未验收。**

完成源码后，本报告应只更新实际运行结果和证据等级，不复制运行数据或旧报告到插件目录，不把后续阶段门槛改写成 test8 当前完成条件。
