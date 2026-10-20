# V4 使用说明

本包保留 V2、V3，并增加 V4。离线环境仍为 Python 3.8.10、sklearn 0.24.2、Optuna 3.6.0。在当前 Windows 开发机运行该固定环境验证；未实测实体 Win7 机器。

## 日常操作

1. `MODE_MENU.bat` 选择版本及模式；也可直接双击 `SELECT_V2.bat`、`SELECT_V3.bat`、`SELECT_V4.bat`。
2. 把处理好的成品表放到 `input/products.xlsx`。第一行中文名，第二行唯一英文代号，第三行起为记录。`价格属性评分`表中的评分是**价格重要性1—5分**，不是百分比。属性只有“数值”和“配置类型”；1—5、80、7200都可为数值，配置编码可为文字或数字。
3. `CHECK_INPUT.bat` 检查输入和预算，`RETRAIN.bat` 才会训练。至少4条有效价格记录，训练、验证各至少2条。缺失、非正价格应先处理。
4. `START.bat` 只加载当前模式已保存的模型。切换模式不训练，未训练的模式会提示先训练；切回原模式直接使用原保存模型。改变轮数等配置须再次 RETRAIN 才生效。
5. `EXPORT.bat` 导出当前模型、效能、推荐草稿及离线环境。导出包没有训练入口，不需要原始训练表。

只有成品数据是必填。没有效能偏好或耦合关系，可以删掉可选工作表；不会自动编造耦合。效能评分与价格专家重要性分别使用。前台展示中文，英文代号用于字段对应。

## 可切换模式

| V4搜索模式 | 配置值 | 行为 |
| --- | --- | --- |
| 随机划分 | random | 预先固定有限的划分池，每轮选一个开发划分搜索，最终在同一划分池复核候选 |
| 全划分统一搜索 | global | 每套参数在所有选定划分上计算训练/验证预测，统一优化 |
| 逐划分搜索 | per_split | 每个选定划分独立搜索，收集候选后统一跨划分复核 |
| 混合复核（默认） | hybrid | 先在覆盖样本的少量划分上搜索，给逐划分搜索提供初始参数，再跨划分复核及融合 |

“全划分”指 `split_plan.json` 中实际选定的集合。4—7条记录、验证2条、没有分组限制时，唯一划分分别为6、10、15、21。训练行按固定顺序排列，重复种子不会冒充新划分。默认 `coverage=auto,max_splits=64`，组合更多时固定抽样64个；**并非全部1820个**。需要全部枚举可设 `coverage=exhaustive`，受 `enumeration_limit` 保护。

保留 V2 原始六模型核心；V3保留其两阶段搜索。V4在六模型基础上拓宽正则化、SVR和树参数，按训练条数调整树深、叶子和分裂样本数；专家树最小增益按当前训练价格的对数方差缩放。MinMax和配置独热编码只在当前训练子集拟合。

默认模型随机种子在 **0—10000** 范围搜索；`model_seeds=[0,42,137]`只用于初始参考，**不是只试三个种子**。`model_seed_mode=list`时才限于给定列表。TPE对随机种子独立抽样，不把相邻种子当作相近模型。随机种子不能增加真实样本信息，也不能保证更高独立预测精度。

## 训练约束、融合与解释

菜单可选择训练门槛：`hard`优先满足门槛、`soft`按违约程度惩罚、`off`只按开发目标选择。范围可选 `pooled`汇总训练预测、`every_split`每个划分都满足、`fraction`达到指定通过比例。默认训练MAPE≤10%、R²≥0.9，开发目标相同；阈值分别配置。

V4同时检查最终全量重拟合的训练指标。少量记录的某一折价格相同，R²显示为空（不可定义），不伪造0或1；该折若要求R²门槛则记为未通过。总R²用逐条预测汇总计算，不平均两条记录的小折R²。

候选均在相同划分上复核，最多保留 `review.max_candidates` 个进行融合选择。每套参数内部六模型有非负归一化权重，参数组合之间再有非负权重；差模型可以权重为0。默认融合的开发MAPE与R²均不得差于最佳单套参数，否则保留最佳单套。可以关闭跨参数融合，仍保留六模型内部融合。

专家1—5分与学到的融合份额分别保存。可以关闭专家评分做纯数据对照；原评分保留，训练使用统一评分。线性模型展示原单位对数价格系数，树展示分裂/重要性，组合展示两层份额。评分不是价格贡献百分比，也不保证每个属性与价格的方向。

默认 `publish.policy=allow_draft`：未达训练或开发门槛时输出明确标记的 `unqualified_trial_draft`。`require_targets`或旧 `targets.hard_gate=true` 则不更新运行版本，旧模型保留。任何模式都不能保证存在满足指标的参数，更不是数学上的全局最优。

## 预算与效率

`config/price_optuna_v4.json` 是全部设置入口。常用项：

| 配置 | 含义 |
| --- | --- |
| search.n_trials | 默认300，配置搜索总轮数；可设5000 |
| search.timeout_seconds | null为不设时间上限；到时停止新配置搜索，仍需完成最终复核、重拟合与导出 |
| v4.search.budget_mode | total共享总预算，或per_split逐划分预算 |
| v4.search.global_fraction | hybrid中统一搜索所占总预算，默认0.4 |
| v4.search.trials_per_split / global_trials | per_split预算方式下各局部研究的轮数、混合全局轮数 |
| v4.search.sampler / nsga_fraction | nsga、tpe、nsga_tpe；两阶段在各研究内共享预算 |
| v4.search.model_seed_low / model_seed_high | 模型随机种子范围 |
| v4.search.model_seed_mode / model_seeds | 范围抽样，或指定候选列表 |
| v4.search.coarse_splits / warm_candidates | 混合初筛划分数、送到局部研究的初始候选数 |
| v4.splits.validation_rows | null按validation.test_size自动计算且至少2条；可直接指定验证条数 |
| v4.splits.coverage / max_splits / enumeration_limit | auto、exhaustive、sampled，以及划分与组合枚举上限 |
| v4.splits.random_splits | random模式最多复核的划分数；max_splits=0时也用于抽样回退 |
| v4.splits.group_identical_inputs / group_field | 默认相同输入成品在同一侧；可指定批次等英文属性分组，两个限制取并集 |
| v4.splits.stratify | 默认false；开启后要求训练与验证都覆盖价格分箱，极小数据可能没有可行划分 |
| v4.train_gate | mode、scope、mape_percent_max、r2_min、min_pass_fraction、soft_penalty |
| v4.review.max_candidates | 最终统一复核候选上限，所有局部最佳参数仍保存在诊断报告 |
| v4.ensemble | enabled、max_configurations、weight_trials、non_degradation |
| expert.enabled / scores / field_types | 专家评分开关、英文代号对应1—5分、数值/配置类型 |
| deployment.model_scope | full_data用全部开发记录，selected_split用开发划分快照 |

`total`方式中global/random各用n_trials轮；hybrid把预算按比例分给初筛和全部局部划分；per_split把总预算分给各划分。总预算不足以给每个划分1轮时在CHECK_INPUT提前说明。`per_split`方式例如64个划分各5000轮就是320000轮；不会伪称总共5000轮。融合权重优化轮数另列，只复用预测数组，不反复训练模型。

成员预测缓存有上限，键包含数据、训练/验证成员、有效模型参数、种子和专家倍率；每次搜索独立缓存。无效候选记为失败，不把NaN、无穷大或假成绩交给TPE。运行日志显示当前研究、配置轮数、实际拟合数、缓存命中；最终统一复核会额外拟合，详见预算和缓存报告。更大预算不保证指标单调改善。

## 评估口径

默认 `development`：所有划分、超参数和融合权重都使用开发数据；跨划分分数仍是**参与搜索的开发成绩**。逐划分调参不能获得独立测试成绩。

可在菜单切换 `locked_holdout`。至少6条，默认固定种子留出至少2条，开发部分至少4条；同组记录一起留出。也可通过 `holdout_ids` 指定成品编号。必须声明全部属性类型。外层价格不进入选参、融合、训练门槛或最终重拟合；`full_data`在该模式下只指全部**开发**记录。完成选择后才评估外层留出。已经查看该分数并据此改方案，它就不再是下一轮未见测试数据，应换新外部记录验证。

模式切换不会混用已保存模型。V2/V3、V4搜索模式、专家开关、训练门槛范围、融合开关、部署范围、评估方式分别登记到 `state/modes.json`。数值预算改变不会自动重训。

## 报告与交付

`price/training_report.json`包含划分汇总训练成绩、最终部署训练成绩、开发成绩、可选外层成绩、达标状态与耗时。`split_plan.json`列出每个划分真实成员；`budget_plan.json`记录预算；`trials.csv`及`search_diagnostics.json`记录试验、失败原因与各局部最佳；`cross_split_review.json`、`cross_split_predictions.csv`记录最终候选和复核预测；`selected_configurations.json`记录参数和两层权重。原生pkl可直接接现有服务。

完整包中的已保存模型和表格均为虚拟演示，只验证运行链路。开发分数、独立外层分数和重拟合训练分数分列，不能用训练达标或虚拟例子的成绩证明真实业务精度。
