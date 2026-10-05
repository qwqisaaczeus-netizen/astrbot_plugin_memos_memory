# 6.1.0-test1：持久任务与单次调用边界

## 安装与阶段边界

本版从完整 test0 包延续，不迁移或重写现有记忆，不自动补偿，不启动 V2 调度。`RUNTIME_ENABLED=False` 指新生产系统尚未全局启用，不表示本版全部代码闲置：旧运行时已复用单次调用边界，外置单模型跟接的超时截短已修复，直接 API 的结构化错误和用量信息也已接入旧客户端。

它不是日记质量问题的最终修复包。日记的取证、叙事及发布改造按大纲在 test3、test4 完成；没有通过本版自动重跑历史批次。

## 实现对照

| 大纲 test1 要求 | 实现 | 边界 |
|---|---|---|
| Task/Attempt/Artifact 持久化 | generation_v2/store.py | 独立 SQLite；只在显式实例化时创建 |
| Astr 与独立 API 适配 | generation_v2/adapters.py | 非流式、同事件循环、单次分发 |
| 旧业务入口适配 | generation_v2/bridge.py 与 llm_runtime.py | text_chat 文本合同，不替代主聊天工具/多模态合同 |
| 超时与取消 | dispatch_once、SingleAttemptRunner | 显式预算；取消传播并落盘；不触发备用 |
| 错误分类 | classify_error、DirectHTTPError/DirectRequestError | 鉴权、配置、限流、过载、传输、超时、空正文、任务错误 |
| 无隐藏重试 | 严格适配器设置 request_max_retries=0 | 已验证本机 Astr OpenAI 与插件直接 API；第三方 provider 不作无证据保证 |

## 任务账本

TaskSpec 固定 task/job 身份、业务类别、作用域、prompt/schema/route 版本、截止时间、最大尝试次数和优先级。输入只接受 prompt、system_prompt、role/content 上下文；不接受 api_key 等配置字段。文本本身仍属于私人数据，保存在本机任务库，默认元数据导出不包含它们；不是加密归档承诺。

task_id 重复且合同相同为幂等创建；输入、版本或期限变化会拒绝复用，调用方需显式修订任务。SQLite BEGIN IMMEDIATE 保证并发领取只有一个成功；完成时核验 owner，拒绝迟到覆盖。尝试和产物提交在同一事务中完成。输入和输出有 SHA256 校验，损坏产物不复用。

账本带独立 application_id 和 schema 版本，不把任意已有 SQLite 文件当成任务库。原文、向量、心潮、旧补偿库均不迁移到该库。输入产物是任务所需的语义请求，不是第二个原文权威库。

## 单次执行

SingleAttemptRunner 只执行一次，不拥有调度重试循环。成功产物可按同一任务身份复用；失败停在 awaiting_recovery，重复调用不会重新付费。取消记 cancelled 并重新抛出 CancelledError。验证不通过保留草稿为 output_rejected。数据库重开不会擅自重领 running 任务；崩溃恢复与租约在 test2 实现。

无自定义 validator 时只验证非空文本，状态 succeeded 仅代表模型任务完成，不代表证据合格、文学质量合格或已写入 Memos。业务需要提供同步确定性的 schema 验证器，并使用正确的 schema 版本。

默认尝试上限字段为 3，但 test1 不自动消费剩余尝试。test2 才统一主路重试、跟接和作业总上限，不能把字段存在误认为调度已完成。

## 超时与适配器

显式 timeout 优先于模型默认值。任务绝对截止时间与尝试期限取剩余较小值；非流式总调用受 asyncio.timeout 限制。异步供应商调用仍在当前事件循环；只有 SQLite 工作放入线程。

严格 AstrAdapter 要求 text_chat 明确声明超时和 request_max_retries 参数；仅有 **kwargs 不足以证明支持控制。首字、连接分段和流空闲耗时不可观测时为 null。供应商 request ID、费用估计目前未取得可靠值，为 null；不猜测费用。可取得的 token 用量有记录。

本机 Astr v4.26.8 OpenAI 的 request_max_retries 表示总尝试次数，传 0 被内部限制为一次；同时 client.with_options(max_retries=0) 关闭 SDK 重试。对第三方实现仅检查签名不代表验证其内部行为，因此 capabilities 明确标记语义依赖 provider。

DirectAdapter 应包装 OpenAICompatibleTextClient，而不是自带轮换的外置池。其单次路径覆盖客户端配置的重试次数，HTTP 429 保留数值 Retry-After。日期形式 Retry-After 暂不可解析，返回 null，不捏造等待时长。

## 旧代码实际变化

- ExternalModelRegistry.single_text_chat 不再取 min(任务超时, 模型默认超时)；没有显式值才使用默认值。
- PluginLLMRuntime 保留已有排队、并发、熔断和返回对象，只把分发操作交给共享 dispatch_once，不另加重试循环。
- 直接 API 保留异常类型、HTTP 状态与用量，不把所有错误抹成一个 RuntimeError；不保存上游错误正文到 V2 尝试。
- TaskBoundProvider 是显式任务绑定的兼容入口，已测试旧 text_chat 调用形式；不自动给每条旧调用猜测 task_id。test2/3 接管业务时使用它。

旧外置模型池轮换、业务主路重试等仍属于旧运行时，没有宣称全局已无嵌套重试。test1 的单次合同为后续替换提供边界。

## 不动的部分

模型配置、密钥、命令、召回注入、时间锚定、身体节律、原文/日记、心潮状态、小院资源均保留。没有启用遗忘接管，没有付费真实模型实验，没有改 Astr 源码或已安装插件。

## 下一阶段

test2 以这些单次适配器和账本构建调度、跟接、总预算、检查点及旧补偿迁移，移除相应旧重试责任。需要验证重启、取消竞态、未知结果、费用上限和重复点击后才启用；不能直接把 RUNTIME_ENABLED 改为 True 当作完成。
