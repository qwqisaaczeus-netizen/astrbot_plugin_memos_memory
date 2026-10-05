# 安装与升级到 6.1.0

[返回首页](../README.md) · [发布说明](releases/v6.1.0.md) · [架构与边界](architecture.md)

## 下载与校验

使用固定版本的 [6.1.0 Release](https://github.com/qwqisaaczeus-netizen/astrbot_plugin_memos_memory/releases/tag/v6.1.0)，下载附件 **[astrbot_plugin_memos_memory-6.1.0.zip](https://github.com/qwqisaaczeus-netizen/astrbot_plugin_memos_memory/releases/download/v6.1.0/astrbot_plugin_memos_memory-6.1.0.zip)**。

GitHub 自动生成的 “Source code” 压缩包是仓库快照；它与原始插件附件的字节和 SHA-256 不同。需要核对本次正式封包时，请使用上述附件。

- 大小：`30098739` 字节。
- SHA-256：`c789dcd99ffc440cfd14ab337aa79cfe9996a8491bdee158b10598b743966af0`。
- ZIP 内根目录：`astrbot_plugin_memos_memory/`，共 270 个文件。

Windows PowerShell：

```powershell
Get-FileHash -LiteralPath .\astrbot_plugin_memos_memory-6.1.0.zip -Algorithm SHA256
```

Linux：

```sh
sha256sum astrbot_plugin_memos_memory-6.1.0.zip
```

## 环境与首次安装

随包资料记录的适配基线为 AstrBot `v4.26.8`；本次仓库发布没有重新在在线运行时验证。Memos 需可访问并具有日记读写权限；启用新生产还需确认服务支持自定义 `memoId` 和事件 `createTime`。不要把普通连接检查成功当成这两项发布能力已经确认。

1. 在 AstrBot 插件管理中安装该 ZIP。手动安装时，将插件根目录放入插件目录，避免额外套一层文件夹。
2. 使用 AstrBot 运行环境安装 `requirements.txt` 所需依赖。`sqlite-vec` 不可用时有 Python 余弦回退，性能可能降低。
3. 配置 `memos_base_url`、`memos_token`、角色名及 Embedding Provider，按需设置压缩/状态模型和 rerank。凭据只保存在本地配置中。
4. 重载插件。WebUI 默认监听 `127.0.0.1:8088`，端口以实际配置为准。
5. 检查连接与索引健康。若首次接入的既有 Memos 日记尚未同步，执行一次 `/memos-sync`；新安装空数据无需为了升级而执行重建命令。

## 覆盖升级

1. 先备份插件配置、实际使用的 `plugin_data/astrbot_plugin_memos_memory`、仍在使用的旧数据位置，以及 Memos 数据。运行中的 SQLite 应使用一致性备份，不能只拷贝单个 `.db` 文件并忽略 WAL。
2. 记录当前版本、数据目录、模型路线和生产模式。正在执行的任务在插件重载时可能中断；关闭浏览器并不会取消后台任务。
3. 安装 6.1.0 原始 ZIP，覆盖插件程序文件。保留配置和数据，勿卸载并清空数据目录。
4. 重载插件，强制刷新 WebUI；检查版本、原文/日记数量、状态历史及未完成作业。

| 起始版本 | 处理方式与边界 |
| --- | --- |
| 6.1 test / RC | 覆盖并重载。旧作业保留原模型、写作合同、预算与失败历史；升级不会自动添加新阶段或重跑。 |
| 6.0 RC / 5.x | 覆盖并重载；已有配置和源数据沿用。缺失派生层由启动流程幂等补齐，不能理解为没有任何本地数据库写入。 |
| 4.6.x 及更早版本 | 随包兼容路径保留旧日记，补齐缺失的派生结构；缺少的原始对话不会被伪造。本轮未实测所有旧版迁移组合。 |

例行覆盖升级不需要 `/memos-reindex`、`/memos-episodic-rebuild` 或重写旧日记。更换 Embedding 模型/维度、确认索引损坏或首次接入未同步资料时，再执行相应操作；重建和补索引可能产生 Embedding 费用。

插件 ZIP 不覆盖 AstrBot 核心。历史文档提到的 Provider 核心补丁属于单独操作，不是安装本 ZIP 的必做步骤；未应用适配补丁时，Provider/SDK 的内部重试行为仍须按实际 AstrBot 版本确认。

## 启用 6.1 新生产

安装后默认 `generation_v2_enable=false`，Memos 发布能力确认也为 `false`。原有兼容生产继续按配置工作；安装不是整体零调用、零写入模式。

1. 进入 `/production` 的「6.1 草稿与迁移」，检查当前显示的兼容生产、新生产或等待确认状态。
2. 选择一个已归档原文批次、模型和调用上限，确认费用后生成草稿。生成草稿不写 Memos，但会调用模型；关闭页面不会取消作业。
3. 检查引文、解释核验、正文、审稿和调用记录。机器核验不能代替人工判断。
4. 需要使用自动新生产时，点击「启用新生产」，确认费用、备份和 Memos 发布能力。后续普通、手动及夜间压缩才进入新链。
5. 已有草稿仍需单独点击发布。旧内容先做迁移预检，再按批次生成证据并显式应用；不会自动重写旧日记。

自动新生产每批默认 `generation_v2_job_cap=12`，包含重试和跟接；草稿入口默认 8 次。它们是物理请求上限，不是日记篇数、并发数或价格。已有任务使用冻结合同，调度中心改参数通常作用于后续任务。

失败时保留原文和已有结果。补齐未完成阶段、重新抽取或重写可能收费，需要界面明确确认；已有远端发布清单时优先恢复交付，补索引仍可能使用 Embedding。已由新链接管的未完成批次，关闭开关后不会自动交回兼容链再次生成。

## 保留数据与回退

升级不自动改写历史 Memos 正文、不自动重跑旧作业，也不将草稿视为已发布。首次初始化、派生索引补齐和作业恢复可能写入本地数据，不能表述为全程只读。

回退先停止插件，再恢复相互匹配的旧程序、配置和一致性数据备份。旧程序未必能直接读取新版数据库；若已显式发布或替换 Memos 内容，还需核对远端状态。仅替换 ZIP 不能撤销已经发生的远端写入。保留新版本副本与失败历史，避免覆盖唯一可恢复的数据。
