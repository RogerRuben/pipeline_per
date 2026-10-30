# P0 架构决策记录 · 2026-10-30

来源：用户本轮已确定架构；任务书和附件作为证据输入。代码基线见 audit/source_baseline.json。

1. 复用产品构建、Optuna V3、价格模型、现有 HTTP 价格服务、推荐和 SQLite。保持 automl_pipeline 算法不变，构建命令显式 optuna_v3。
2. 深度分析并入 app/ 与同源 Portal，页面 /price/deep，接口 /api/deep-price/v1/*。不新增常驻旧 Flask 服务。index.html 为布局和交互基准；其余三页纳入功能对照，page1 不作为首页。
3. 深度分析通过当前已发布业务版本读取产品，通过 Store.canonical_business_parameters/runtime_parameters 编码，再调用现有价格网关。禁止直接读取旧 pickle、样本 CSV、训练 Excel。价格服务故障返回 unavailable。
4. 模型库只读；安装、切换、发布和回滚仍由现有管理流程负责。分析调用不得执行 sync_model_schema 等有 registry 写入副作用的方法。GET/POST 计算都沿用 Portal 登录保护。
5. 模型身份和产品发布版本绑定；批次期间切换版本应拒绝或整批重试，不能混用两个版本。价格万元，业务输入使用稳定 field_id，中文只作展示。
6. 可加属性普通路径 ML，纯可加路径基准差额；混合变化返回 unsupported_mixed_adjustment，公式未定不得双算。P0 仅声明契约。
7. 效能先复用 trial_snapshot 并展示试用状态。未来 Provider 提供 schema/evaluate/health/model_manifest；V13 算法及依赖整包调用，不改专家源码。
8. P4 采用真正 Windows PE product.exe + onedir，监督服务进程、就绪后开浏览器、退出清理自有进程。模型/发行资源与 SQLite/日志/导出/设置分开。重封包不自动重训或覆盖用户数据。

阶段状态：P0 文档与工程审计已落盘；正式源表、实际 V3 产物依赖和 Win7 实机门槛仍待证据，P0 未全面验收。P1/P2/P3/P4/P5 均未实施。
