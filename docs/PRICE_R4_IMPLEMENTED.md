# 成品价格预测、效能与推荐 R4.1

R4.1修正核岭截距、搜索覆盖与高维训练耗时，完整原因和诊断文件见`PRICE_R41_AUDIT_FIX.md`。

自带Python 3.8.10 x64、sklearn 0.24.2、NumPy 1.19.5、SciPy 1.5.4、pandas 1.3.5、Optuna 3.6.0。无需安装Python或联网。目标为Win7 SP1兼容路线，系统仍需具备对应运行库/系统补丁；本次验证主机并非Win7。

## 使用

1. 完整解压到可写目录。
2. 用处理好的成品表替换`input/products.xlsx`。第1行中文名称、第2行唯一英文代号、第3行起为记录。价格注明元/万元等单位。CSV也支持，使用CSV时移走默认XLSX。
3. 首次训练自己的数据，双击`TRAIN.bat`。也可将数据表拖到TRAIN.bat。
4. 之后双击`START.bat`直接运行保存模型。此入口不读取训练表、不比较训练参数、不自动重训。包内已训练的虚拟示例可直接启动试用。
5. 需要更新模型时使用`RETRAIN.bat`，新版本写入新的outputs目录；旧版本保留。训练失败不会切换当前运行版本。
6. `EXPORT.bat`将当前已训练版本和环境导出到exports目录。导出的运行包有自己的START.bat，移走原始训练表也能运行。固定旧版本可运行其目录内的START_PROTOTYPE_WIN7.bat。

## 输入

只需“成品数据”。“价格属性评分”“效能属性配置”“耦合关系”可缺失、为空或仅表头。

价格属性评分列：英文代号、中文名称、价格重要性评分（原始1—5分）、可选属性类型、专家编号、价格属性组。1—5分在内部转换为价格先验，不要求百分比，不作为成本系数或效能权重。未评分使用中性先验并明确标记。多专家保留原始记录并采用中位数；JSON可开启单专家对照。

属性类型可以填写number、boolean、enum、text、ip_grade。数字编码的控制方式/材料等请声明enum，避免被当作连续量。英文代号必须对应成品表，未知、重复或评分越界会定位具体工作表和单元格。

效能偏好独立填写“效能属性配置”。没有已知耦合时保持空表，R4不会自动发现或宣称物理关系。效能与可行性仍为初版偏好与经验范围模型。

默认仅空白为缺失，真实-1温度保留。需要其他标记时在价格JSON的missing_tokens明确配置。价格必须为正，至少10组不同有效配置。30组只用于证据提示，不阻止10组训练。

## 价格训练

候选包括专家加权Ridge、Lasso/ElasticNet、正则化相对绝对误差、相对Huber、PLS、SVR、RBF核岭、专家分裂树、ExtraTrees、GBDT。可开启Matérn高斯过程。专家倍率在折内补全/编码/缩放之后施加，PLS不再消除倍率。Ridge采用样本空间等价求解，取消R3固定自由度3和固定2维PCA约束。

先用固定基准种子搜索超参数，再冻结参数探索估计器种子和温和样本权重。扰动保留全部记录，同组共享权重。所有选参、种子选择、融合及校准都在外层训练部分重做。

10组默认外层训练9组、内层每次训练8组，汇总10条外层预测计算MAPE/R²。13组以上内层默认5折，微样本外层仍留一；16条每轮训练15条，内层每折训练12条。单条不计算R²。没有外部表时报告“外部测试未提供”，不虚构独立保留集分数。更大数据使用分组K折。质量不足仍可导出试用模型，并显示未达标；quality.hard_gate=true时才强制拒绝。

相同配置的种子先平均，再根据互补增益选少量配置，学习非负融合份额。保留等权/单模型回退；不能保证增加模型一定提升。可选全局乘数从折外预测拟合并向1收缩，作为独立校准展示。

## 调整

训练设置位于`config/price_automl_r4.json`。ports与启动行为位于`config/portable.json`。修改训练设置不会改变当前已保存模型，须运行RETRAIN.bat。

- search：先warm_start系统搜索收缩和稀疏回归路径，再anchor_trials次TPE；promote、expert_strengths、ridge_alphas、各模型parameter_ranges、restart_trials/max_restarts与seed_cost_ratio可调。
- pipeline：mean/median补全、standard/robust/none缩放、identity/log价格、all/expert_core/screened视图及评分阈值、select_k折内筛选数量。
- seed_search：quick/standard/deep上限64/500/5000；max_trials可覆盖到100000。实际尝试、完整验证、失败、未完成及拟合次数分别报告。一个尝试表示一组固定参数的一个种子完成整套内层验证；不等于只改变种子编号。
- seed_search.early_stop：达到开发目标或改进稳定时提前停；未达标的稳定状态先重新选参，之后仍未达标会明确标记失败原因。关闭后按预算尝试到上限。minimum_trials、check_interval（默认64）、patience、minimum_gain、members_per_configuration、sample_weight_strength可调。5000是每轮探索上限，不是最终保存5000个模型。
- resources.total_seconds：训练时间预算；深度运行应按机器能力增加。预算不足的审核明确标为未完成，不显示通过。为最终试用导出最多另留120秒，报告计入总耗时。
- ensemble：最多配置数、MAPE/R²组合目标alpha、权重收缩、删除敏感性检查及报价校准范围。
- expert：JSON评分、属性类型、属性分组、可选group_strengths和compare_individual_experts。
- quality：目标MAPE、R²、证据组数与硬门槛。

模型参数范围示例：`"parameter_ranges":{"ridge":{"alpha":[0.001,100,true]},"expert_tree":{"max_depth":[1,4]}}`。未知键会拒绝，避免修改了设置却实际没有生效。

## 保存与解释

price_native_bundle.pkl保存拟合成员、预处理、价格反变换、配置内平均、融合份额、校准与属性顺序。price_attribute_scores.json保留原始评分及来源。前台使用中文属性名，分别展示价格评分、模型份额和加权金额；树路径、线性项与种子成员细节由预测API的configuration_details返回。对数线性项是对数价格项，不是万元直接加价。

effective_config.json、training_report.json、seed_report.json、weight_report.json、nested_audit.json及各次seed_trials.jsonl记录实际过程。native_equivalence_max_delta_wan检查保存重载与内存结果一致。区间来自折外残差，未验证95%覆盖率。

示例为10条、50属性及10条、40属性的虚拟数据，价格评分在生成前预设。示例精度用于验证流程，不是对真实业务或任意专家评分的保证。
