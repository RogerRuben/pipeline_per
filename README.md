# pipeline_per · 工业产品统一流水线

当前交付：**P0 工程基线与接口草案**。已复用 recommend_prototype 源码；深度分析集成、真实 EXE 和 Win7 验收尚未完成。

```text
input/products.xlsx → 既有 Optuna V3 构建 → 已发布价格模型 → 统一价格服务
                                                        ├─ 推荐系统 + SQLite
                                                        └─ 深度价格分析（P1/P2）
试用效能 → EffectivenessProvider → 未来泵 V13 整包适配（P5）
product.exe（P4）→ 服务监督 / 浏览器 / 独立持久化用户数据
```

- [架构与决策](docs/P0_architecture.md)
- [当前源码核查与阶段状态](docs/P0_source_audit.md)
- [字段与 API 契约](docs/deep_price_api_contract.md) / [OpenAPI 草案](docs/contracts/deep_price.openapi.json)
- [旧页面功能映射和验收](docs/deep_price_feature_matrix.md)
- [发行 manifest 与用户数据](docs/release_manifest_contract.md)
- [依赖风险与实机门槛](docs/dependency_risks.md)
- [P1 拟改文件和回归范围](docs/P1_implementation_plan.md)
- [原始任务书（参考资料）](docs/TASK_BOOK.original.md)
- [原仓库运行说明](docs/UPSTREAM_README.md) / [附件参考](references/README.md)

核对 P0 资产：`python tools/validate_pipeline_baseline.py`。本命令不训练、不启动服务、不修改 SQLite。

`config/build_profile.json` 是新项目的声明性约束，尚未连接执行器。原 CLI 默认 V7，手工构建必须显式传 `--price-engine optuna_v3`。本次未运行 build/serve 或切换模型。原仓库示例模型仅为虚拟验收资产，不等于已选定的正式 V3 模型。

附件内容是待核验输入；用户最新明确决定优先。不要将原任务书中对旧轮次的“只分析”描述当作永久禁止实施，也不要将其勾选项当作本项目已经通过的验收。
