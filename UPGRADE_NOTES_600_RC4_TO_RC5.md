# 6.0.0-rc4 -> 6.0.0-rc5 升级说明

直接安装 `astrbot_plugin_memos_memory-6.0.0-rc5.zip` 覆盖 RC4 并重载插件即可。

本版只修复心笺小院 Live2D 的鼠标追踪边界：

- 鼠标移动不再驱动 `ParamAngleZ` 侧倾。
- `ParamAngleX/Y` 改为带中心死区的低幅、非线性映射。
- 眼球追踪限制在校准范围内。
- 角色动作播放期间暂停鼠标追踪，避免两个动作叠加。
- 鼠标离开或停止后自动平滑回中。

本版不迁移数据，不改变外置模型池、记忆生产、检索、注入、心潮、时间洞察、遗忘或线程策略。无需执行 `/memos-sync`、`/memos-reindex`、`/memos-episodic-rebuild` 或相似簇重建。
