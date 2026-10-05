# astrbot_plugin_memos_memory 5.0.1 更新报告

## 目标

5.0.1 补全 5.0 ACCESS 的真实数据闭环：自动保存每次真实召回的 4.6 基线与 5.0 Shadow 对照，让操作者能在 WebUI 直接看见效果，并生成一个方便后续分析的本地 ZIP。该版本不改变主召回、ACCESS 评分公式、日记、原文、滚动状态、心潮、身体节律或时间注入。

## 自动留档

新增 `memory_access_observations` 请求级派生表。每次真实召回自动保存：

- `request_id` 与本轮 query；
- 4.6 基线名单、5.0 Shadow 建议名单及新增/移除差异；
- 候选数量、A/B/C/D 线索等级、池外/深层/原文救援计数；
- 每个候选的基础分、ACCESS 分、状态、干扰、路由和救援理由；
- 模型回复后，各条已注入记忆的回答支持度与采用率；
- WebUI 人工判断和备注。

默认保留最近 5000 次请求。超限只淘汰未反馈的最旧请求级记录；人工反馈记录不会被自动清理。它不受候选级 `memory_access_event_keep` 影响。

## WebUI

ACCESS 页面新增“真实 Shadow 评测流水”：

- 近 30 天真实请求数、建议变化率、池外/深层救援请求数和回答平均采用率；
- query 搜索、“仅看有变化”和“仅看救援”过滤；
- 逐次基线/Shadow 名单对比与候选评分详情；
- “基线更好 / Shadow 更好 / 基本等价 / 不确定”反馈；
- 一键生成并下载分析 ZIP。

## 分析包

ZIP 包含 manifest、汇总、请求级观察、ACCESS 状态、干扰边、评测案例和最近评测结果。它不包含 Memos Token、模型密钥、日记正文或完整原文档案；真实 query 和评测短线索可能包含私密内容，不会自动上传。

## 升级与兼容

- 插件版本：5.0.1。
- ACCESS 算法版本仍为 5.0.0，因为评分公式未改，避免旧评测失效和派生图无意义重建。
- 情景库 schema 从 10 升到 11。首次启动按既有 ArchiveGuard 机制创建迁移前快照，再幂等新增表。
- 5.0.0 的 Episode、原文、状态、ACCESS 事件、反馈、安全门和向量代际均原样保留。
- 无需执行 `/memos-sync`、`/memos-reindex` 或 `/memos-episodic-rebuild`。

## 新设置

| 设置 | 默认 | 作用 |
| --- | ---: | --- |
| `memory_access_observation_keep` | 5000 | 请求级样本保留数。 |
| `memory_access_observation_query_max_chars` | 2000 | 本地 query 留档上限。 |
| `memory_access_export_keep` | 10 | 本地分析 ZIP 保留数。 |
| `memory_access_export_dir` | 自动 | 默认在 episodic DB 同目录的 `access_exports`。 |

四档遗忘预设只会调整请求级样本数量；不会改 Token、Provider、角色名、URL、端口或路径，且仍强制保持在线接管关闭。

## 验证结果

- 在 AstrBot 4.26.8 实际源码接口下完成导入验证。
- 完整自动化回归 395 项全部通过。
- ACCESS 专项 107 项、WebUI HTTP 黑盒 2 项全部通过。
- 覆盖 schema 10 到 11 幂等迁移、零命中请求、回复利用率回填、人工反馈、导出、敏感令牌遮罩和旧数据保留。
- 发布 ZIP 经专用审计器复查通过；仅含一个插件顶层目录，不含运行数据库、配置、缓存或密钥。
