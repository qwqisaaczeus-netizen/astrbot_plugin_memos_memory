# 6.0.0-test11 升级到 6.0.0-rc1

## 升级方法

直接安装 RC1 ZIP 并重载插件。不要先删除插件数据目录。

不需要执行：

- `/memos-sync`
- `/memos-reindex`
- `/memos-episodic-rebuild`
- `/memos-passage-rebuild`

## 数据兼容

- schema 版本保持 13，不重写 Episode、原文、日记或滚动状态；
- 线程、Claim、前瞻、人工反馈、策略历史和一致性观测继续保留；
- 新增采集保留期只是配置字段，不要求数据迁移；
- 旧配置明确设置 `thread_memory_enable=false` 时仍保持关闭；
- 新安装默认开启本地 Shadow 派生与观测，保留 180 天。

## 行为变化

- 四档预设会同时开启脉络总开关和后台 worker，不再出现只改模式但后台不工作的组合；
- WebUI 改策略后会热启动/停止后台 worker；
- Canary 的 `injected` 只在实际追加成功后计数；
- 真实 Canary 追加会同步写为 `shadow=0`，预览与失败继续保持 `shadow=1`；
- 质量门与 `/threads` Canary 统计按当前角色作用域隔离，不再混入同库其他角色；
- 前瞻预约冲突、追加失败和 Shadow 预览不会成为真实注入证据；
- 健康积压时后台构建器以 2 秒节奏追赶，队列见底或出错后恢复正常低频周期；
- `/threads` 可直接调整本地请求观测保留天数。

## 推荐升级后状态

希望先收集数据时：

```text
thread_memory_enable = true
thread_worker_enable = true
thread_mode = shadow
consistency_mode = shadow
thread_observation_retention_days = 180
```

也可以在 `/threads` 应用“保守稳定”。需要小比例验证注入时，再应用“均衡长期”进入 10% Canary。

## 回滚

回滚插件代码不会删除 6.0 派生数据。若只想停止采集，可关闭 `thread_memory_enable` 或 `thread_worker_enable`；若只想停止注入，保持总开关开启并将 `thread_mode` 改回 `shadow`。
