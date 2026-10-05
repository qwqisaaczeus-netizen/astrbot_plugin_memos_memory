# 从 6.0.0-test10 升级到 6.0.0-test11

1. 在 AstrBot 中直接安装 `astrbot_plugin_memos_memory-6.0.0-test11.zip`。
2. 重载插件。
3. 不执行同步或重建命令。
4. 打开 `/threads` 核对版本与原策略。升级不会自动应用新的四档预设。
5. 需要开始小比例真实验证时，手动应用“均衡长期”，它会使用 10% 稳定会话 Canary；不希望任何在线脉络注入时保持 Shadow。
6. 打开 `/house?preview=1` 可检查新 Live2D 待机微动作；无法加载官方 Core 时会自动静态回退。

本次没有数据库 schema 变更。既有原文、Episode、日记、滚动状态、ACCESS、遗忘、心潮、时间、小院内容、人工反馈和脉络派生数据均保留。
