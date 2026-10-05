# 6.0.0-rc3 -> 6.0.0-rc4 升级说明

直接安装 `astrbot_plugin_memos_memory-6.0.0-rc4.zip` 覆盖旧版本并重载插件即可。无需执行 `/memos-sync`、`/memos-reindex`、`/memos-episodic-rebuild` 或相似簇重建。

## 外置模型池

1. 打开主 WebUI 的“模型池”，或直接访问 `/models`。
2. 新增一个或多个 OpenAI 兼容模型，填写显示名称、API Base URL、model 和 API Key。
3. 逐个点击“测试”，确认连接、鉴权和模型名可用。
4. 选择默认模型；需要时调整优先级、超时、温度、输出上限与内部重试。
5. 开启“模型池接管”并保存。

启用后，插件内部的证据抽取、日记渲染、滚动状态、画像、心潮、时间洞察和脉络审校使用模型池；各旧设置中的 Provider 选择显示为导入模型。旧 Astr Provider ID 保留不改，关闭模型池后自动恢复使用。

模型池不会改变 AstrBot 的角色主回复模型，也不会接管 Embedding、rerank 或小院独立创作 API。API Key 单独保存在插件数据目录的 `external_model_secrets` 中；Windows 使用当前用户 DPAPI，加密内容不进入 WebUI 响应、日志或发布包。

## 兼容与回滚

- 外置模型池默认关闭，升级后行为与 RC3 一致。
- 不迁移或重写任何记忆、日记、原文、状态和索引。
- 关闭模型池并重载插件即可完全恢复 Astr Provider 路线。
- 降级 RC3 时，新增模型池数据会留在本地但不会被旧版本读取；不会影响旧配置。
