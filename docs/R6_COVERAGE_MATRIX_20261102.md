# R6 原四模板覆盖矩阵

本矩阵按用户批准的 R6-A～E 范围核对。原版参考来自附件提取目录；镜像只去掉了末尾空白行，原件与镜像各自 SHA 见 [参考来源](audit/r6_20261102/reference_sources.json)。旧 Flask / pickle 未启动。因素页参考图由原 Excel 的只读 Top10 响应数据文件驱动；其他原版图为静态模板，部分旧 API 不可用提示保留。图示用于结构与控件比较，不证明旧模型已运行。

| 原模板 / 能力 | R6 归宿及口径 | 验收依据 |
| --- | --- | --- |
| index：左右输入/预测布局、主导航 | 四个主视图；动态字段替代硬编码液压字段，保持双栏与分组区域 | exe_home_view.jpg，browser_acceptance.json |
| index：产品选择/回填 | 当前发布产品分页快照，160 条虚拟样例可选 | prediction / 产品回填浏览器步骤 |
| index：功能/性能/环境分组 | 由真实业务字段分组；本虚拟表缺少该分类，三个空分组禁用，其他参数可用，不擅自分类 | exe_home_view.jpg |
| index：输入可用状态 | 可输入 / 明确缺失映射 null；未声明不可用编码时禁用第三分支，0 保留为有效值 | zero_remains_valid、missing_maps_to_null_input |
| index：必填缺失反馈 | 当前模型必填字段缺失时按价格服务契约拒绝；恢复参考属性可继续预测 | required_missing_honours_service_contract、restore_reenables_input |
| index：单次预测 | 统一 HTTP 价格接口，不回退旧 pickle | 57 项接口回归、真实 EXE 三入口一致性 |
| index：价格卡/参考成交价 | 参考价与模型预测分别标注，单位万元；虚拟模型标识保留 | exe_home_view.jpg |
| index：雷达/价格比较 | 业务范围或参考值归一化雷达、真实预测与参考价柱图；不称为效能/因果 | exe_home_view.jpg、exe_home_lower_view.jpg |
| index：时间/数量校正 | 输入侧独立选项卡；规则未配置禁用，已配置规则仍走原范围绑定服务 | unconfigured_correction_disabled、既有校正回归 |
| index：独立整行机理容器 | 保留展示与下载控件，未配置禁用，不生成虚假解释 | exe_home_lower_view.jpg |
| index：异步结果失效 | 属性/方案名称改变即丢弃旧响应；刷新清空旧结果，保持原 epoch/身份防护 | edit_invalidates_prediction、refresh_clears_results |
| factor_analysis：专门入口 | 价格影响因素主视图，独立柱图/排名/累计占比 | exe_factors_view.jpg |
| factor_analysis：Top10 横图 | 当前模型离线置乱 MAE 增量；保留正负值，不以专家评分代替 | bound_importance_available、evaluation_metadata.json |
| factor_analysis：排名/比例表 | 原始值、标准差、正值归一占比；缺失标准差标不可用 | exe_factors_table_view.jpg |
| factor_analysis：累计贡献图 | 全字段正增量累计占比，不称为因果贡献 | exe_factors_table_view.jpg |
| factor_analysis：旧 Excel | 25 项真实原表，legacy_demo，原模型范围及源文件 SHA 明示；不送入当前模型重要度 Top10 | legacy_demo_isolated、legacy_importance.json |
| factor_analysis：专家评分 | 独立来源；未提供则明确未提供，不造值 | 浏览器来源切换、原服务元数据 |
| factor_analysis：模型/业务绑定 | 重要度包含模型/特征/参考样本/业务数据指纹；业务或模型不匹配则不可用 | bound_metadata 测试、57 项回归 |
| model_evaluation：模型卡 | 只读当前真实发布；可靠显示当前服务仅一个模型，不把磁盘文件推断为历史版本 | model_library_readonly、modelLibraryNote |
| model_evaluation：成员/融合 | 搜索候选六族与部署 Lasso/Ridge 分列；实际权重 28.41% / 71.59% | exe_models_view.jpg、真实 bundle 元数据 |
| model_evaluation：指标卡 | 训练、全数据重拟合、部署训练、搜索划分验证分别展示；无指标显示不可用 | exe_models_metrics_view.jpg |
| model_evaluation：MAPE / 单位 | 训练 mape_percent 直接显示；验证 mape 比例乘 100；MAE/RMSE 按明确万元元数据展示 | metric_units_and_members、单位回归 |
| model_evaluation：安装/启用 | 只读页不上传任意 pickle；提供开发包训练、验收、备份退出、发布新包的管理员流程和导航入口 | 只读接口测试、管理员流程折叠说明 |
| scenario_analysis：专用页 | 敏感性与情景主视图内的专用子选项卡，避免长页面锚点空白；保留旧书签 | analysis_subtabs、main_navigation_at_top |
| scenario_analysis：局部/历史 | 局部半径 max(abs(当前值)×20%,1) 与真实历史 min/max，均受业务合法范围约束，显示实际采样值 | local_sensitivity、historical_sensitivity |
| scenario_analysis：两个 Top10 | 重要度由服务器按当前绑定数据先选字段；扰动敏感性按本区间最大价格变化后排序，不能混同 | importance_top10、perturbation_top10、正向顺序回归 |
| scenario_analysis：自由文本 | Schema 明示不可分析性，自由文本不进入敏感性选项；不扩展 V3 文本算法 | schema 单元测试及选择器过滤 |
| scenario_analysis：简约编辑 | 只选要改的字段，高级模式可展开全部；多命名情景保持 | scenario_simple_mode、scenario_advanced_mode |
| scenario_analysis：价差摘要 | 原价、新价、差额、百分比、改动字段与旧/新值由真实预测和规范化参数计算 | scenario_delta_and_changes、服务器情景回归 |
| scenario_analysis：Top5/气泡 | 另做单字段 HTTP 预测，展示独立效应；不相加为定价公式 | scenario_bubble_and_top5、exe_scenario_bubble_view.jpg |
| 四视图：导出 | 共用会话导出区，30分钟，价格与版本由服务器记录；价格 TXT 明确区别于机理 TXT | browser_scenario_export.txt、http_scenario_export.csv、csv_txt_consistency.json |
| 四视图：版本/资源 | 下载 EXE 的7个核心静态资源与仓库源码逐字节 SHA 核验 | exe_resource_acceptance.json |
| R5 软件边界 | 运行 ZIP 不含 .py/.bat/payload；开发包可含源码 | runtime_boundary_audit.json |
| 首启端口 | defaults/desktop.json 可提前修改并校验格式；既有用户配置优先，其他源码/模型文件仍校验 SHA | prelaunch_config_integrity.json |
| 多实例端口 | Windows 独占监听；自动验收用隔离端口，不与实际软件争用 | port_isolation_repro.json、EXE 控制与恢复回归 |

## 刻意保留的边界

- 原任意 pickle/npy 上传和直接启用未恢复；完整多模型注册/在线原子切换不是本轮新增能力。当前没有受控历史目录时如实只显示一个发布模型。
- 机理/大模型未配置，保留禁用容器；不以价格 TXT 冒充机理报告。
- page1 热力图不在强制恢复范围。
- 不改 AutoML V3、专家效能算法，不实施可加属性混合定价。
- Win7 SP1 x64 仍需独立目标机实测；本轮所有执行证据来自 Windows 11。

## 浏览器复演路径

1. 从 Portal 进入深度价格分析，选择参考产品，预测显示约16.4894万元。
2. 改动输入后旧价格/导出失效，恢复参考属性再预测；0保持有效，必填缺失按服务契约报错。
3. 因素页切换当前模型、历史演示、专家评分，核对来源与差异；当前样本为报告中32个搜索验证ID，部署全数据重拟合后不称独立测试。
4. 模型页查看真实两成员、候选六族与明确单位的指标；展开管理员发布流程。
5. 敏感性页选局部或历史范围，分别运行单参数、重要度选取Top10、扰动敏感性Top10。
6. 情景子页添加方案，改连续参数00为50、部件配置00为基础；查看16.4894到15.6423万元、差额约-0.8471万元及两项改动。单字段效应不能相加作联合价。
7. 导出 CSV/TXT。TXT为实际浏览器下载文件；CSV另通过同一HTTP导出接口的独立验收会话取得，两者价格/差额误差小于1e-8。
8. 刷新产品版本，核对结果清空和控件ID不重复。

