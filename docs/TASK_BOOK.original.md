# 工业产品价格预测与推荐软件：P0 架构基线及分阶段开发任务书

**文档版本**：P0-v1.0（2026-10-30）  
**状态**：架构方案与下一阶段开发任务书；**不代表代码已实现或完成 Win7 实机验收**。  
**本轮原则**：只分析、梳理和冻结契约，不修改原仓库业务代码，不开始打包。  
**最终目标**：在 Windows 7 x64 / Python 3.8 约束下，复用已有“产品表→Optuna V3→价格模型→价格服务→推荐系统”流水线；适配价格深度分析功能；先交付无正式泵效能的可执行目录，后续追加冻结泵模型并可重复封装。用户双击**真正的 `product.exe`**，由其负责启动本地服务并打开默认浏览器，而非运行 BAT 作为最终入口。

## 0. 已确认决策与明确排除项

| 决策 | 冻结内容 |
|---|---|
| 主集成源码 | `RogerRuben/recommend_prototype`；P0 阅读时 `main` 的 HEAD 为 `e6bd67b8efb415af6a5faea1f7bc1af977a25021`（后续开发必须重新记录实际起始 SHA，不得使用浮动 `latest` 作为发布标识）。`recommend_autoML` 可作为历史参考，不覆盖最新推荐主干。 |
| 训练引擎 | **Optuna V3**，构建参数显式使用 `optuna_v3`；不自动改为 V7/V3+、V6 等主仓库默认新引擎。 |
| 原始产品输入 | 构建时一次性读取 `input/products.xlsx`（不是 `.xlxs`），既有双行表头契约：中文名 + 英文字段 ID；运行时从已发布数据、SQLite / 服务模型读取，HTTP 请求不重新读取训练 Excel。 |
| 深度分析原型 | 附件一 `价格预测界面0823/templates/index.html` 为首页布局与交互基准；`factor_analysis.html`、`model_evaluation.html` 为配套子页面，纳入功能核对；不以 `page1.html` 替换首页。 |
| 深度分析实现 | **允许修改前端和完全重写后端**，以功能复原为验收标准；不得再从分析模块直接加载旧 pickle、访问旧 `样本1.csv` 作为产品真源。 |
| 预测权威入口 | 现有价格服务的 `POST /api/v1/predict`、`POST /api/v1/predict/batch`。推荐和深度分析共用同一当前已发布价格模型与版本。 |
| 深度分析数据权限 | 对推荐系统业务数据库、模型发布、正式模型库 **只读**；计算请求可使用 POST，下载报告可产生临时导出文件，但不改变正式发布状态。 |
| 机理与 LLM | 初版界面保留位置、状态、契约与禁用/未配置反馈；不以假文案冒充真实解释。禁止打包硬编码 API Key。 |
| 初版效能 | **不改专家泵 V13 原工程**；复用已有试用效能服务/快照，可按输入表中前几个**可计算且满足契约**的有效特征构建演示默认值，并标注试用；为未来冻结泵 V13 提供可替换适配口。 |
| 可加属性 | 保留可加规则层和双路径设计，先做接口与测试约定；“可加与非可加同时修改”的最终计价公式暂不定，不能静默双算。 |
| EXE | 真正的 Windows PE `product.exe` 主入口；启动本地价格服务、推荐 Portal、试用效能/未来泵服务、健康检查、默认浏览器打开和退出清理。允许 **onedir** 发行，不要求所有数据嵌入单个 exe。 |

### P0 非目标

- **不**在本阶段重写已有表格、AutoML 和推荐流水线。
- **不**在本阶段训练新模型、修改现有训练精度或切换生产模型。
- **不**在本阶段实现正式泵 V13 学习、正式 LLM/因果解释、三层价格模型。
- **不**在本阶段产出正式可执行包；需要先完成接口与依赖核验，再开始后续阶段。

## 1. 现有资产核查与复用边界

### 1.1 推荐与价格主干（仓库现状）

- `automl_pipeline/__main__.py` 已有 `--price-engine` 版本选择，包含 `optuna_v3`；`automl_pipeline/product_price_optuna_v3.py` 是 V3 配置/训练入口。
- `automl_pipeline/product_pipeline.py` 已能产出价格模型、试用效能快照和推荐业务草稿，并有保存结果的服务启动流程。
- `services/price_service/app.py` 暴露 `/api/v1/schema`、`/api/v1/predict`、`/api/v1/predict/batch`；`app/model_service_client.py` 已是推荐系统的统一价格调用客户端。
- `app/store.py` 使用 SQLite，`app/server.py` 提供 Portal、推荐和管理接口；`app/configuration.py` 已预留“价格深度分析”导航配置，当前默认关闭。
- 已有 `RUN_SAVED` / 导出、模型摘要、交付与回滚等机制，先**复用和验收**，仅补足 EXE 宿主与深度分析适配需要的接口；禁止平行发明第二套版本发布逻辑。
- 已发现兼容性风险：历史训练环境 `sklearn 0.24.2`，仓库部分推理 requirements 标出 `sklearn 1.3.2`。P0 必须查明**本次选定 V3 产物的真实 pickle 依赖与导出类来源**，确定 Win7 Python 3.8 可实际加载的精确 runtime；不得按“同为 Python 3.8”判断可以互载。

### 1.2 附件一：深度分析页面

用户交付文件名为 `价格预测界面1005(1).rar`，归档内部目录是 `价格预测界面0823/`，保留该原始路径标识避免版本误认。

- 首页：`templates/index.html`，顶部还有 `factor_analysis.html`、`model_evaluation.html` 两个子页面。包含：选择参考产品自动回填；功能/性能/环境分组参数；时间与批量校正；预测价格、可视化；机理解释流式状态；CSV/TXT 导出；单参数和 Top10 敏感性分析。
- 后端：`app.py` 提供 `GET /api/products`、`POST /price_adjust`、`POST /predict`、解释状态/流接口、保存文件接口、特征重要度与敏感性接口、场景预测等。
- 旧数据/模型：`样本1.csv`、`feature_importance.xlsx`、旧 `models/` 或 `model/` 目录中的 pickle/npy，加载旧编码器及单独模型族。
- 风险：输入字段、分类映射、产品数据、特征重要度与多个页面 API 依赖**硬编码中文名称/列位置**；`model_evaluation.html` 还存在**本地模型上传、激活与切换**操作，不能直接保留为只读分析用户权限。
- 缺失：`pipeline0821.py` 导入 `predictor0730`，归档只有 `__pycache__/predictor0730.cpython-311.pyc`、无对应可编辑 `.py`。**不以该 pyc 为部署依赖**；由于重构后统一改走价格服务，该旧预测实现不应成为阻塞项。
- 安全：旧后端包含大模型接入相关凭据配置；审计全部源文件、说明和前端资源，发现真实硬编码令牌必须删除并轮换，不写入发行包。

### 1.3 附件二：泵效能 V13

- 附件 `03_泵系统V13_界面接口版` 有 `system_app.py` / `system_ui.html` 和 `frozen_system_model.py`；**当前提供的是演示/专家应用与接口代码，不包含甲方正式冻结模型**。
- 正式冻结包：`system-effectiveness-frozen-runtime-package-13.0`；运行类 `FrozenSystemRuntime`；模型路径 `model/frozen_system_model.json`；输入字段、任务 ID 和单位以冻结包契约为准，不应硬编码示例六字段。
- 需要的主要结果：`system_assessment.decision_score`、`continuous_score`、`direct_score`、`product_score`、属性贡献、`task_transfer`、`system_v13_visualization`。
- 初版只通过现有 `services/effectiveness_service` 的**相同业务外层接口**输出试用效能，并明确 `backend=trial_snapshot` 或对应来源标记；正式模型导入后采用适配器转换为既有 `/api/v1/evaluate` 与 schema，前端无需了解原专家项目内部结构。
- 接入正式冻结包前必须整包校验格式、SHA256、字段/单位、任务，与专家软件对同一输入比对；不得用“前几个特征计算”的试用分数冒充 V13 专家评分。

## 2. 目标总体架构

```text
构建侧（只在首次构建/主动重训/主动重新封包时运行）
  input/products.xlsx ──> 既有双行表头解析、数据契约及版本快照
    ├─> 已有 AutoML 构建流水线 ──[engine=optuna_v3]──> price_native_bundle.pkl
    ├─> 已有产品/推荐发布草稿 ──────────────────> SQLite/业务版本快照
    ├─> 已有试用效能快照 ──────────────────────> 效能适配器输入
    └─> 字段映射、单位、模型摘要、特征统计与解释元数据 manifest
        └─> 深度分析前后端集成测试 ──> Win7 构建与 onedir 发布

运行侧（用户双击 product.exe；绝不重新训练）
  product.exe（进程监督/启动/健康检测/浏览器/退出）
     └─> 统一推荐 Portal（HTTP，同源入口）
           ├─> 推荐业务（SQLite 可写，由受控业务接口处理）
           ├─> 价格深度分析（只读业务/模型发布状态；允许计算）
           │      ├─> 产品/属性/schema 查询 ← 已发布数据与映射
           │      ├─> 单次或批量价格预测 ──> 统一价格服务
           │      ├─> 可加属性规则路由（后续阶段启用）
           │      ├─> 敏感性/情景/时间批量校正
           │      └─> 机理/LLM 插槽（未配置时明确标记不可用）
           ├─> 价格预测服务（已发布 V3 模型；/api/v1/predict[ /batch ]）
           └─> 效能服务（初期试用 snapshot；未来泵 V13 运行包适配器）

发行文件不等于运行数据：
  Product/  product.exe  runtime/  web/  models/  config/  manifest.json
  独立可写位置：userdata/ (SQLite、日志、导出、备份、用户设置)
```

**唯一权威原则**：价格模型只由现有价格服务载入；业务产品来自已激活版本；深度分析不得再拥有自建的模型库/pickle 加载器；同一 HTTP 请求必须记录 `product_code`、`release_id`、`price_model_version/hash` 和价格单位。

## 3. 数据及跨模块契约

### 3.1 字段契约（必须创建明确文档）

- 每个属性保存 `field_id`（原第二行英文代号）、`label_zh`（第一行中文）、`unit`、`type`（数值/枚举/布尔等）、`required`、`allowed_values`/范围、`model_value_mapping`、`group`（功能/性能/环境，可配置）、`price_model_input`、`additive_rule_id`（可空）。
- `field_id` 是服务和数据库稳定标识；页面只显示中文名及单位，**不得把中文名当请求 JSON 键的正式身份**。中文重命名不应破坏原有模型及引用。
- 入库使用 Business Value；到价格服务前通过**现有** `Store.runtime_parameters()`/模型字段转换机制编码，避免重复定义类别映射。对于缺失、`-1`、枚举编码必须沿用模型契约，不能复制旧页面的硬编码策略。
- 输入 Excel 在 build/publish 阶段读取，产出可追溯 `source_sha256`、`schema_hash`、业务版本与模型版本。运行时产品切换只查已发布数据；Excel 被移走或修改不得造成 START 隐式重训。
- **价格单位统一**：服务既有 `predicted_price_wan` 为万元输出。时间校正、量价校正、可加规则和 CSV 统一显式 `unit=万元`，内部必要时做精度保留，避免元/万元混算。

### 3.2 推荐增设的深度分析适配 API（拟定，不是已有接口）

统一挂载在推荐 Portal 同源路径下，建议页面 `/price/deep`，命名空间 `/api/deep-price/v1/*`；最终路径在 P1 技术评审时可微调，**避免将附件的 `/predict` 或 `/api/products` 直接占用推荐根路由**。

| 新接口（拟） | 方法 | 来源与职责 | 初版状态 |
|---|---|---|---|
| `/schema` | GET | 当前已发布价格字段：中英文映射、单位、分组、允许值、特征范围 | 必做 |
| `/products` | GET | 推荐系统已发布产品列表/分页；不读 `样本1.csv` | 必做 |
| `/predict` | POST | 接收 Business Value → 转换 → 调 `18101 /api/v1/predict` → 标准结果 | 必做 |
| `/predict/batch` | POST | 转换多组方案 → 调原价格服务 batch；受长度/耗时限制 | 必做 |
| `/price-adjust` | POST | 时间、批量的规则/基准配置计算，与模型预测分开 | 必做 |
| `/feature-importance` | GET | 来自真实模型解释元数据或明确标记的专家评分；**不得混淆** | 必做降级 |
| `/sensitivity/single`、`/sensitivity/top10` | POST | 用同一价格预测网关批量计算；未来由可加属性路由分流 | 必做 |
| `/scenario` | POST | 多属性模拟；普通属性服务预测，可加/混合变化按规则状态决策 | 必做 |
| `/model-summary` | GET | 只读当前模型身份、成员、指标、发布时间、来源、可用解释 | 必做 |
| `/explanation/status`（或 task） | GET | LLM/机理插槽状态；未配置返回明确 `not_configured` | 预留 |
| `/export` | POST/GET | 下载本次计算结果与来源元数据；不得写业务 DB | 必做 |

> 请求/响应都应含统一错误码、`request_id`、`release_id`、`model_version`、`price_unit`、`pricing_mode`（`ml`/`additive_rule`/未来 `mixed`），并区分 `unavailable`、`invalid_input`、`unsupported_mixed_adjustment`。返回超时不静默改用旧 pickle。所有示例响应在 P1 给出实际 JSON schema 与快照测试。

### 3.3 旧功能到新组件映射（复原清单）

| 原文件/端点 | 需要保留的用户能力 | 新实现原则 |
|---|---|---|
| `index.html` 产品选择、`GET /api/products` | 下拉选择产品并自动回填参数 | 从业务版本读取，动态中文表头、稳定英文字段 ID |
| `index.html` 固定中文输入栏 | 功能/性能/环境分组输入 | 保留 index 版布局；字段列表和控件类型改由 `/schema` 动态生成，处理可选项与单位 |
| `POST /predict` | 价格预测与图表 | deep-price 适配器 → 单一价格服务；结果包含模型来源 |
| `POST /price_adjust` | 年份指数与采购数量校正 | 独立可配置业务规则，先核对 `PPI`、基年、批量弹性系数；与 ML 价格分开显示 |
| `GET /explain_status/<task>`、SSE | 机理说明区域、进度、导出 | UI 保留；缺模型/大模型时显示“未配置”，不调用外部网络，不生成假解释 |
| `/save_csv`、`/save_txt` | 导出价格与解释结果 | 带来源和版本的导出；解释未启用不得生成伪文本 |
| `/api/feature_importance` | 关键特征/机理页展示 | 从当前模型真实可得元数据读取；否则明示“暂无模型重要度”，专家评分单独列示 |
| `/api/sensitivity_single` | 单属性价格敏感性曲线 | 数值/离散合法点采样，批量调用；可加属性改用规则产生固定/分段/区间结果 |
| `/api/sensitivity_top10` | Top10 对比曲线 | 真正基于价格变化率展示；专家加权展示须显式标注为另一指标 |
| `/api/scenario_features`、`/api/scenario_predict` | 多参数情景模拟 | 复用 schema、属性类型与同一报价路由，不单独恢复旧预测器 |
| `model_evaluation.html` 的 `/api/model_sets` | 模型库/评价信息 | **只读模型摘要、版本、训练指标** |
| `model_evaluation.html` 的上传、切换模型 | 旧交互存在，但与分析只读冲突 | 普通分析页面移除写入口/仅展示不可用提示；由受权限控制的既有管理发布流程负责，不允许前台任意上传 pickle |
| `factor_analysis.html` | 模型属性展示 | 复原布局和可验证的指标含义；没有证据的数据不展示成事实 |

### 3.4 可加属性路由（P0 定义，P3 实现）

规则必须显式声明：`rule_id`、字段 ID、单位、版本、适用范围、基准值、`fixed_or_interval`/`per_component`/`piecewise_value`、边界策略、与其他规则关系；规则不可混入模型权重。

- **没有变化涉及可加属性**：始终走价格服务（`pricing_mode=ml`）。
- **只有可加属性变化**：以已声明的基准产品/基准报价和规则差额计算：`P_new=P_base+A(new)-A(base)`；允许确定值或区间值，输出规则明细（`additive_rule`）。
- **可加 + 非可加同时变化**：P0/P1 仅保留 `mixed` 契约和能力声明，未批准公式时应返回可识别的“暂不支持混合调整”，**不能预测后无条件再加价造成重复计价**。待讨论三层模型时另开决策记录。
- 敏感性曲线也走同一分流入口，不允许页面内另外编一套加价公式。

## 4. 效能模块最小适配边界

1. 初版从当前**已发布**产品数据选择前 N 个适合试用计算的特征，选择规则须配置化且稳定：仅选择符合数值/编码/缺失要求的字段，记录字段 ID、单位、方向来源；缺少可用字段则标明模块不可计算，不能默默产生固定分数。
2. 试用效能输出沿用推荐已有的 schema/evaluate 接口及来源标志，与价格服务逻辑解耦；推荐相关功能继续正常运行，不需要改动泵 V13 专家源码。
3. 为未来 V13 留下 `EffectivenessProvider`/适配器接口（`schema()`, `evaluate()`, `health()`, `model_manifest()`），版本、SHA256、任务 ID 和字段映射来自冻结包。
4. V13 适配只在取得**正式完整冻结 ZIP**后执行：校验包→安装到隔离版本目录→对照相同请求的专家应用结果→原子激活→保留上版回滚。现有冻结包与试用快照**不是同一种算法**，不能以“兼容输出格式”声称评分等价。

## 5. Windows 7 可执行交付设计（为后续 P4 定义）

- 推荐先用 **PyInstaller `onedir`** 方案开发 `product.exe`，或同等级真实 PE 入口；Python 3.8、sklearn 与原生依赖按锁文件固定版本。PyInstaller 的 Windows 7 **不是当前官方保障平台**，必须在实际 Win7 SP1 x64（含所需 UCRT/VC 运行组件）进行启动、安装与 API 实算验收。
- `product.exe` 不是 BAT wrapper；它应管理服务进程树、选择可用本地端口并传参、等待 health/schema/model 实算就绪，才打开默认浏览器 `/portal`。不能只检查端口可连接就报成功。
- 关闭主程序应正常终止它启动的服务，已有其他产品实例不能被误杀；重复双击、端口占用、杀进程恢复均需有明确策略。
- **构建入口与用户入口分离**：开发构建可以是脚本/命令，最终用户必须只需运行 `product.exe`；**重新打包**不等于**重新训练**，模型内容/配置不变时可直接复用既有发布模型。
- 正式安装、升级不得覆盖 `userdata/`。SQLite 需迁移、版本号、备份和回滚；日志/报表/临时数据有可写权限；建议非管理员用户也可运行，短英文发行路径用于 Win7 兼容测试。
- 离线运行：ECharts 等资源随包分发，禁止依赖 CDN；登录权限沿用推荐主干；不对局域网开放 18101/18102 等模型端口，优先 `127.0.0.1`。

## 6. 分阶段实施任务（每阶段必须独立审查）

### P0：设计冻结与工程审计（**本任务书所处阶段**）

**T0.1 源码与依赖基线**：保存仓库 commit SHA、两附件 SHA256；建立源码路径/依赖清单；记录 `optuna_v3` 引擎配置、模型产物布局和主要静态资产；确定第三方许可证与离线依赖。  
**T0.2 功能复原审计**：为 `index.html`、`factor_analysis.html`、`model_evaluation.html` 列出控件→旧端点→新端点、权限、依赖、显示效果及预计自动化验证；对硬编码中文、旧分类编码、缺失模块和秘钥作记录。  
**T0.3 数据/服务契约**：形成 schema 字段映射说明、服务请求/响应、模型身份、训练数据 vs 发布业务数据、price/feature/rule 单位及错误码。  
**T0.4 打包兼容门槛**：列出精确 Python/sklearn/numpy/scipy/Optuna 依赖，pickle 加载实测方案，Windows 7 目标主机测试清单和备份策略。  
**T0.5 阶段验收评审**：输出《P0 决策记录》《API 草案》《功能回归矩阵》《依赖风险清单》，审定后才创建业务代码修改 PR。

**P0 完成定义**：所有关键路径明确；没有“默认新版算法”“业务页自行加载 pickle”“重新运行时加载 Excel”“只用 bat 启动”“泵试用数据假称正式效能”这些歧义；仅保留混合可加属性定价、正式泵冻结包和正式 LLM 三类有意延期的开放问题。

### P1：统一数据与深度分析服务适配（第一个可运行中间产物）

- 保留既有 `products.xlsx → price V3 → 推荐` 构建链路；抽出 **DeepPriceGateway**（拟名）和产品/字段 schema 查询，不复制训练或 pickle。
- 开发 `index.html` 动态控件生成、产品回填、分类 Business/Model 值转换、预测、年份/批量调整、摘要可视化、导出；实现面向推荐 Portal 的同源路由与登录访问。
- 模型评价页改只读，特征重要度和专家评分分开展示；解释位置返回 `not_configured` 可见反馈。
- 提交回归测试：同一产品/同一模型/同一请求，深度分析与推荐通过正式价格服务所得价格数值一致；更换产品/重启后不重读 Excel；业务数据库和模型 hash 均不被分析操作修改。

**P1 验收产物**：浏览器可运行的统一 `/price/deep` 页面（尚无 exe），接口契约与测试报告；旧首页关键功能逐项对照截图或用例。

### P2：敏感性、情景与只读模型评价完整复原

- 完成单因素、Top10、情景批量运行；对连续、整数、枚举和缺失值做合法性约束；批量调用正式价格服务并支持超时/部分失败。
- 统一模型版本/测试指标/专家信息展示；明确训练结果、解释元数据、非因果曲线的区分。导出包含模型版本与配置摘要。
- 异常/离线/权限/UI 回归，保留 `index.html` 为视觉基准。

**P2 验收产物**：功能对照矩阵全部“实现/明确预留”，浏览器中无旧 pickle 读取、旧 CSV 依赖；自动化单元+集成测试报告。

### P3：可加属性规则入口（先纯可加）

- 按数据契约维护可加规则配置、运行时增减价、价格区间、部件数量乘数、分段数值规则；校验规则版本及单位；敏感性改走统一路由。
- 仅可加变化与纯普通变化均可准确返回；混合变化主动拒绝或标记待决，**不默认组合**。

**P3 验收产物**：示例配置、规则案例、区间与逆向恢复测试、敏感性一致性对照；未定三层方案形成决策草案但不执行。

### P4：EXE 首版与反复构建

- 增加 Windows 真实 `product.exe` supervisor、进程管理、相对路径静态资源、运行时与可写数据目录隔离、健康检查、构建 manifest 和一键打包命令。
- **无正式泵 V13** 版可用试用效能 snapshot 和既有推荐路由；明确试用状态。
- 允许前端修改后仅重封、适配器修改后重封、主动重训后重封三种不同工作流，保存上个可运行版本并支持回滚。
- 在 Win7 SP1 x64 实机测试冷启动、离线、端口冲突、二次启动、浏览器关闭与退出、数据库数据保存、重启、迁移、权限不足、模型损坏/缺失。

**P4 验收产物**：`Product/` 目录、真实 `product.exe`、构建脚本、锁定依赖、manifest、SHA256、Win7 实机记录（若未实测仅标“待验证原型”）。

### P5：泵 V13 正式模型后装集成

- 在收到完整冻结包后实现适配器；不重写专家算法；模型 ZIP 和依赖整体校验；与原专家程序对照评分；保持现有推荐/深度分析价格路径不变。
- 支持模块启停、整包更换与原子回滚、重新封装；所有状态标记清楚“试用/正式/不可用”。

**P5 验收产物**：正式泵效能包适配验证报告、替换与回滚演练、重新打包通过的 EXE。

## 7. 必须执行的验收矩阵（P0 先定义测试用例）

| 分类 | 必测案例 | 判定标准 |
|---|---|---|
| 数据一致性 | 输入表构建、产品选择、中文显示与英文字段 ID、枚举/缺失/单位 | 数据发布后所见属性与模型请求一致；修改源 Excel 不影响已保存运行版 |
| 统一定价 | 同一属性从推荐页面、深度页面、批量 API 调价格服务 | 价格、单位、发布版本一致；服务失败不回退旧模型 |
| 分析复原 | 产品回填、参数区、价格图、时间/批量、敏感性单/Top10、情景、下载、评价页 | 关键功能可用且视觉以 index 为准；无硬编码模型字段导致错误 |
| 模型只读 | 普通用户尝试上传/激活/切换模型、通过分析接口修改业务记录 | 无权限或无入口；模型 hash、发布记录与业务库不变 |
| 可加属性 | 固定值、区间、乘数量、分段值、减配、数值边界、混合改动 | 报价/上下限/规则明细正确；混合待决时明确拒绝 |
| 效能 | 试用来源标记、可用字段不足、正式 V13 待接入 | 不误标“专家评分”；未来接口可独立替换 |
| 模型发布 | V3 显式选择、已保存版本启动、版本切换回滚、恶意/损坏 pickle | 明确版本；生产启动不训练；不加载非可信上传 pickle |
| EXE | Win7 冷启动、无 Python/无网络、路径含中文、端口占用、退出进程、重复启动 | 真 EXE 唤起全部服务；不依赖 BAT；用户数据不丢失 |

## 8. 风险、优先级与阻断条件

| 级别 | 风险 | 处理动作 |
|---|---|---|
| P0 阻断 | 训练与推理环境版本不同导致 V3 pickle 不能加载 | 找到本次 V3 bundle 使用的真实 sklearn/NumPy/自定义类路径；同环境重新加载并做 HTTP 对照 |
| P0 阻断 | 中文硬编码字段与双行表头字段身份不一致 | 构建 `field_id → label_zh → business/model value` 映射，schema 驱动页面 |
| P0 阻断 | 模型评价页遗留上传/切换权限 | 迁出普通分析页面；沿用已有认证、模型发布流程 |
| P1 阻断 | 模型来源/单位不透明，敏感性“重要度加权”被当成原始价格变化率 | 输出 `model_version`, `unit`, `metric_kind` 并调整图表标注 |
| P4 阻断 | 仅在 Win10/11 打包，未在 Win7 测试 | 正式发行前必须 Win7 x64 实机端到端验收（含 UCRT/VC 运行时） |
| P4 阻断 | EXE 升级覆盖 SQLite 或触发无意重训 | 数据目录独立、模型版本锁定、启动只读模型、备份/原子发布机制 |
| 延期决策 | 可加+非可加属性混合报价 | 单独讨论三层模型/差额定价，P0 只保留显式不支持状态 |
| 延期资产 | 正式泵 V13 ZIP 未到位 | 仅发布试用效能并明确标识；不阻断初版无正式泵 EXE |
| 延期功能 | 机理模型和 LLM 未到位 | UI/接口禁用状态和安全凭据策略先行，不假装真实机理结果 |

## 9. 推荐的项目组织（拟议，不要求 P0 实际创建）

```text
repo/
  app/                             # 原推荐系统：保留
    static/                        # Portal（加入深度分析页面/资源）
    deep_price/                    # 拟增：schema/adapter/analysis/rules/export
  automl_pipeline/                 # 原流水线；显式选 optuna_v3，不重写
  services/
    price_service/                 # 原推理服务；训练和运行依赖需锁定
    effectiveness_service/         # 复用试用接口；未来泵适配器
  product_shell/                   # 后续新增：product.exe 入口、进程监督、打包
  config/
    build_profile.json             # 引擎、产品、功能开关、资产选择
    additive_pricing.json         # 后续纯可加规则及版本
  tests/
    deep_price_contract/           # 页面、字段、接口、端到端一致性
    release_win7/                  # 真 EXE 系统验收
  docs/
    P0_architecture.md
    deep_price_api_contract.md
    deep_price_feature_matrix.md
    release_manifest_contract.md
```

**原则**：优先并入现有 `app/`，不另建一个长期开机 Flask 服务；将旧页面前端资源转换为同源静态资源。若已有 server 路由结构不适合模块化，则 P1 提交最小兼容路由层改动，避免强行依赖旧 Flask 应用。

## 10. 开发 Agent 工作指令（下阶段用）

> 基于本任务书执行。先在全新功能分支从已核验的 `recommend_prototype` commit 出发；先输出源码现状差异、字段映射和 API schema，明确拟改动文件与回归影响。**不得修改 `automl_pipeline` 的 V3 算法、不得另写价格模型加载器、不得更改泵 V13 专家源码、不得把真实原始数据上传第三方服务**。每次仅实施一个已批准阶段，提交测试证据和可运行中间产物供人工验收；发布打包阶段不得未经授权自动重训/覆盖当前模型或 SQLite。旧深度分析附件只作为功能和 UI 参考，不直接携带旧 pickle、旧密钥或 3.11 pyc 到 Win7 产品中。

## 11. P0 交付核对表

- [x] 冻结用户选择（`index.html`、真实 EXE + 浏览器、Optuna V3）。
- [x] 确认现有推荐与价格流水线**优先复用**；识别实际主仓库的 V3 入口与服务接口。
- [x] 识别旧深度分析的主要页面/接口、旧 pickle、独立 CSV、硬编码字段、模型上传/切换和不可迁移旧 pyc。
- [x] 识别泵 V13 冻结运行时契约及“无甲方正式冻结包”边界。
- [x] 给出目标架构、适配接口草案、版本/数据/权限/报价/打包约定、阶段与验收矩阵。
- [ ] 后续实施前，锁定本次真实 V3 模型的依赖矩阵并在 Windows 7 环境复核。
- [ ] 后续实施前，验证实际源 Excel 的双行字段映射、分组和价格单位；未附原表时不推断其具体列。
- [ ] 后续实施前，人工批准 P1 首批拟修改文件清单、接口命名和 UI 验收截图。

---

### 核查资料及来源定位

1. `RogerRuben/recommend_prototype` 的 `main` 源码（2026-10-30 访问）：`app/server.py`、`app/model_service_client.py`、`app/store.py`、`app/configuration.py`、`services/price_service/app.py`、`automl_pipeline/__main__.py`、`automl_pipeline/product_price_optuna_v3.py`、`automl_pipeline/product_pipeline.py`、`docs/OPTUNA_V2_V3_PIPELINE.md`、`docs/PRODUCT_V2_PORTABLE.md`、`docs/V21_WORKFLOW_PORTAL_WORKBENCH.md`。
2. 附件一 `价格预测界面1005(1).rar`：内部 `价格预测界面0823/app.py`、`pipeline0821.py`、`templates/index.html`、`factor_analysis.html`、`model_evaluation.html`、`scenario_analysis.html`。
3. 附件二 `软件公司界面接口交付版_锁V17_泵系统V13_20260904.zip`：`01_对接说明/软件公司界面接口交付说明.md`、`03_泵系统V13_界面接口版/docs/系统级冻结模型调用与迁移说明_V13.md`、`app/frozen_system_model.py`。
4. 外部兼容参考：Python 官方 Windows 文档与 PyInstaller 官方 requirements/usage 文档（Win7/Python3.8、非官方 Win7 保障与 UCRT 依赖）。

**注**：本文件包含“现有源码核查所得事实”和“拟议设计/未来验收条件”两类信息；标注为“拟”“建议”“后续”的接口与模块目前不能视为仓库中已经实现。
