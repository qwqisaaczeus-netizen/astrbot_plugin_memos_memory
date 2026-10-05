# 6.1.0-test0：基线与兼容合同

本版是离线开发底座。V2 尚未接管运行时，旧生产路径仍有已知缺陷，不建议替换正在使用的插件来尝试修复日记。保留完整插件是为了逐阶段验证兼容性，不代表认可 RC8 的生成质量。

## 已交付

- `generation_v2/contracts.py`：稳定原文身份、有限尝试预算、显式超时优先、发布资格和恢复动作的纯函数合同。
- `generation_v2/preflight.py`：只读 SQLite 预检、在线一致快照、原文范围和哈希验证、旧补偿盘点。无网络调用，不读取模型密钥。
- `generation_v2/baseline.py`：相对底包的逐文件 SHA256 和静态调用位置清单，确认删除及修改范围。
- `tests/test_generation_v2_test0.py`：合同、完整与部分原文、缺失绑定、仅日记、WAL 快照、只读保障及真实旧超时行为复现。

## 当前调用责任表

| 层 | 当前入口 | 6.1 迁移目标 |
|---|---|---|
| 业务 | main 的压缩、状态和画像；xinchao、时间洞察 | 业务只提交有类型的任务 |
| 路由 | `_plugin_llm_text_chat` | 单一运行时管理尝试和跟接 |
| 调度 | `PluginLLMRuntime.call` | 持久 Task/Attempt、截止时间和公平排队 |
| 外置模型 | `ExternalModelRegistry` / `direct_llm` | 单次调用适配器，取消隐含超时截断 |
| 恢复 | `LLMCompensationStore` 和 main 的恢复函数 | 同批次单个作业，按产物恢复 |
| 发布 | `_store_one_diary` 和 Episode/向量持久化 | 持久发布操作、未知结果对账 |

## 数据所有权与升级

原文归档继续归 SourceArchive 管；Episode 和原文链接归 EpisodeRepo 管；向量是可重建派生数据；Memos 正文仍允许用户编辑。新任务库不能修改原文或强行覆盖 Memos。

预检分级：

- `source_complete`：批次数量、索引连续性、逐条正文哈希及篇章范围可核验。仅代表档案内结构完整，尚未核验语义证据。
- `source_partial`：关联批次存在，但原文不全、哈希异常或篇章范围待核实。原因分别列出。
- `diary_only`：没有原文批次绑定；只能进行日记衍生升级。
- `broken_link`：声明的批次找不到；先修映射，不能假装它从来没有原文。

保留所有旧内容。本版只提供预检，不执行迁移、不自动重写、不接管遗忘。test3 才实现内容升级预览，test4 实现发布与切换，test5 验证完整迁移及回退。

## 首批可复现故障

旧 `single_text_chat` 收到 180 秒显式任务期限、60 秒模型默认值时，传给客户端的仍是 60 秒。测试直接执行旧方法并截获参数，无真实 API 调用。该测试通过表示成功复现 bug，不表示 bug 已修复。V2 合同要求传递 180 秒，在 test1 实现新适配器时验证。

本地证据恢复不得取得新生成日记的发布资格；当前只实现纯函数合同，旧生产器未连接该合同。后续必须增加真实业务路径测试，不能用该纯函数替代端到端验收。

## Astr 适配初步核对

本机 Astr provider `openai_source.py` 提供 `text_chat(request_max_retries, request_timeout)`，非流式调用会向 `_query` 传递请求超时。流式入口没有同样的 `request_timeout` 参数，并保留独立重试循环。test1 必须分别做能力适配，不能通过切成 stream 自动宣称解决超时。

## 本地运行

在插件目录的父目录执行：

```powershell
python -m unittest discover -s astrbot_plugin_memos_memory/tests -p test_generation_v2_test0.py -v
python -m astrbot_plugin_memos_memory.generation_v2.preflight --database <原文库路径> --snapshot-dir <新的隔离目录> --output <新的报告文件>
```

可选 `--compensation <补偿库路径>`。报告不含原文正文、模型密钥或请求 payload；批次及 Memos ID 仍属于本地元数据。快照包含私人记忆，不应放进插件发布包。

test0 的退出条件：源数据只读、旧内容分级可解释、合同及失败基线可复现、底包可追溯、无删除旧功能。调用成功率和文学质量是后续阶段门槛，本版不宣称达成。
