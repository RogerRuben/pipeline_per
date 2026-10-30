# P1-1 验证与遗留问题 · 2026-10-30

测试源码提交：`af1c2a0744eadb9adefc15793c3b700090c8f9cf`。源码树SHA、执行环境、测试模型SHA和命令见 [机器报告](audit/p1/p1_validation_report.json)。之后提交仅补充本验证记录和日志，不改变业务实现。

| 检查 | 结果 | 证据 |
|---|---|---|
| 深度分析集成 | 13/13通过；真实本机HTTP、临时SQLite；含完整Application初始化和发布流程 | audit/p1/p1_deep_price_contract_test_output.txt |
| 原兼容测试 | 18/18通过，含Optuna与spawn子进程 | audit/p1/p1_portable_compatibility_test_output.txt |
| 原价格导出回归 | 通过，三模型集成与过期权重回退保持原行为 | audit/p1/p1_price_dynamic_model_hotfix_test_output.txt |
| 来源与结构 | 174项通过 | audit/p1/structural_validation.json |
| Python3.8语法 | 新增及修改Python源码通过3.8语法解析；集成用例实际运行于3.8.10 | 本轮检查及机器环境 |
| P1契约 | JSON可解析，61处内部引用可解析；尚未用完整OpenAPI验证器验收 | contracts/deep_price.p1.openapi.json |
| 凭据扫描 | 全部当时已跟踪的756个可读文本文件，已知令牌/私钥模式未命中 | audit/p1/scope_and_scan.json |

环境为Windows宿主（10.0.26200）、Python3.8.10 x64、Optuna3.6.0、NumPy1.19.5、SciPy1.5.4、sklearn0.24.2。没有为本版重新安装/混合依赖；没有训练模型。以上只证明虚拟开发产物通路，不代表正式V3模型或Win7实机验收。

## 更广回归未通过项

1. 原始P0首次运行完整流水线即因DataMaster.parse的param_by_key未定义而失败。本版补上该定义；未修改DataMaster校验规则。
2. 本版完整旧流水线通过13项初始化、推荐和生成批次检查后，失败于“智能生成方案可独立查看”。在原始fbe13fe隔离副本仅补同一行启动修复后，复现相同的13项通过和相同失败；见 [基线复现](audit/p1/p0_baseline_with_startup_fix.json) 与 [本版结果](audit/p1/full_pipeline_test_report.json)。该问题不由新增深度分析路由引入，本版未修改推荐生成逻辑，也不宣称全流水线绿灯。
3. V21工作台旧脚本在原始P0副本同样因cost_effectiveness_analysis缺少open_new_window键失败；单独继续检查还遇到BAT启动文本断言与现有源码不一致。未修改这些无关功能或将失败用例删除/改为通过。

## 验收状态

P1-1最小价格通路已具备可复验中间产物；由于上述既有回归、未完成页面和正式模型/目标机验收，保留草稿合并请求供审阅，不直接合入main。

下一步为P1-2首页动态字段和交互迁移；P1-3再接校正规则/未配置状态、绑定独立会话的30分钟结果缓存与导出。新页面视觉截图和浏览器回归应在P1-2提交；本版没有新页面，未伪造UI验收证据。正式源表/V3依赖、完整再发行许可证清单、Win7以及EXE仍是独立门槛。
