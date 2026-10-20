# 可选择V2或V3的价格、效能、推荐流水线

完整包内含一套Python3.8.10 x64、sklearn0.24.2、Optuna3.6.0离线环境，两种价格搜索引擎共用。沿用Win7运行环境接口；本次验收在开发机执行，未另行取得Win7实机测试结果。

## 日常使用

1. 解压到短路径，例如`D:\ProductAutoML`。完整包已保存两版虚拟数据演示模型，可直接运行START。
2. 替换`input/products.xlsx`。第一行中文名称，第二行英文唯一代号，第三行起成品记录。价格列建议`价格(万元)` / `price_wan`。
3. 双击`SELECT_V2.bat`或`SELECT_V3.bat`选择版本，运行`CHECK_INPUT.bat`检查，再运行`RETRAIN.bat`训练。两版结果分别保存，训练失败保留已有完整模型。
4. 后续`START.bat`只读取当前选择的保存版本，不读取训练表、不重新训练。选择的版本尚无模型时会明确提示先训练，不会用另一版代替。
5. `EXPORT.bat`导出当前选择的已训练成品，包括环境、价格模型、效能模型和推荐数据。导出包无需原训练表。

`COMPARE_V2_V3.bat`以同一表、评分、类型、验证规则及总预算分别训练两版。默认两版各300轮；可在命令行运行`COMPARE_V2_V3.bat --trials 20`快速检查。对比过程关闭历史成绩复用，结果分别保存，当前版本选择保持不变。报告在`outputs/compare_...`，含中文CSV和说明。两版选出的开发划分可能不同，不能根据被搜索过的开发分数声称独立泛化更强。

## 属性只有两种

在可选工作表`价格属性评分`中使用列`英文代号`、`中文名`、`价格重要性评分`、`属性类型`。类型可填`数值`或`配置类型`（英文`number`、`enum`）。评分列可留空，类型声明和专家评分分别生效。

| 情况 | 属性类型 | 内部处理 |
| --- | --- | --- |
| 数量1—5、出力80、维护周期7200、任意连续量 | 数值 | 按训练集MinMax缩放，保留原数值含义 |
| 驱动方式、安装配置、材料名称；用1/2/3表示不同配置 | 配置类型 | 训练集内学习独热编码，不把配置代码当作大小或距离 |

整数不会因取值少而自动变为配置。未声明时，正常数值按数值，文本类别按配置。旧布尔/文本声明会合并到配置语义；本版不提供有序等级选项。数值整数在推荐搜索中仍保留整数取值约束。

配置编码、数值缩放、英文代号和中文名称一起保存。验证集出现训练中未见的已知类别时，其独热位为0，不用验证行拟合编码。服务只接受保存表内的合法类别；新增类别需重新训练。核心模型可能有更多编码列，前台仍显示原始属性中文名，解释中的类别贡献归回原属性。

所有用于价格的特征需处理完整，价格必须为正数。最低有效记录数由`input.min_samples`配置，默认4，可调高到6、8等，不能低于4。训练集和验证集各至少2条，以保留训练和R方验证的定义。不自动把`-1`当缺失，不静默填充或剔除记录。可选异常值过滤默认关闭。

4—7条数据均可训练。默认`validation.auto_adjust_test_size=true`：4条时2条训练、2条验证；5条时3条训练、2条验证；6条时4条训练、2条验证；7条时5条训练、2条验证。4、5条会自动提高验证比例，6条及以上的默认20%可直接划出至少2条。若设为false，严格使用自定义比例，不满足两边各2条时在训练前提示。实际划分写入`price/validation_split.json`和训练报告；原配置与实际配置分别保存为`requested_config.json`、`effective_config.json`。8条及以上的有效原划分不会因本功能改变。原始V2源码核心文件保持原样，流水线通过可配置的已解析数据入口支持4—7条。

少量数据的开发MAPE/R²容易随划分变化；本功能允许试用、保存和导出模型，不把这些分数当作独立业务精度认证。异常值过滤后也会重新检查最低数量与划分可行性。

## 专家分数用于价格

专家填写1—5分，按英文属性代号绑定，内部按原V2映射给稀疏回归、岭回归、支持向量回归及专家树使用。独热展开的每个类别继承所属原属性的分数。多位专家对同一属性取中位数。分数不是融合百分比；六模型融合权重由搜索学习，弱模型可降为0。

`expert.enabled=false`可作统一倍率的纯数据对照；`expert.use_supplied_defaults=false`停用原版本附带的中文领域评分，未填写项使用`default_score`，默认1。效能偏好与价格评分分别读取；没有耦合关系时不自动生成耦合，效能和其他可选表可不填。

## V2与V3

V2保留所提供ZIP中的六模型核心、NSGA-II双目标、MinMax、对数价格、划分种子与参数和融合权重联合搜索。原始核心文件逐字节保留；新增共用的类型适配，纯数值输入继续与原算法对照。

V3使用同一模型核心，先NSGA-II广域搜索，再多目标TPE精修。总预算由两阶段共享；默认300轮为180+120，并非每个锚点额外120轮。选取折中、低MAPE和高R²的不同Pareto候选作为锚点，定期更新。局部搜索固定锚点划分种子，只调整参数与权重；定期全局探索独立新种子，不搜索“好种子附近的数字”。部分精修轮只调整融合权重，可复用成员预测。

TPE继承前阶段真实完成的试验观测。最终从两阶段及兼容历史候选的并集中选解，后一阶段不会因名为V3而强制覆盖前面的优解。没有指标自动提前停止；轮数或显式时间预算决定终止。不能承诺任意真实数据都达到设定指标。

当前上传V3 notebook只有调用入口，缺少其导入的`price_automl_optuna_v3.py`。本包V3是依据该入口设计在已核验V2内核上实现的流水线版，非缺失源码的逐字移植。溯源记录见`services/price_service/optuna_v2/V3_DESIGN_PROVENANCE.json`。

## 可调整设置

两版分别配置`config/price_optuna_v2.json`、`config/price_optuna_v3.json`。普通切换保留配置。`APPLY_V2_V3_QUICK.bat`将两版设为各10轮；`APPLY_V2_V3_DEEP.bat`将两版设为各5000轮；这些入口不训练。使用真实数据时应按耗时与试验结果调整预算。

| 设置 | 含义 |
| --- | --- |
| `search.n_trials` | 总新试验预算；V2默认150，V3默认300 |
| `search.seed_low` / `seed_high` | 实际训练/验证划分种子范围，默认1—10000 |
| `search.optimizer_seed` | 优化器随机种子，默认42 |
| `search.timeout_seconds` | 总搜索时间上限，默认null，即不设限 |
| `validation.test_size` / `price_bins` | 验证比例与价格分层上限，默认0.2 / 3 |
| `input.min_samples` | 最低有效价格记录数，默认4，可配置为不小于4的整数 |
| `validation.auto_adjust_test_size` | 默认true；仅当划分不足2条验证或2条训练时自动调整比例 |
| `targets` | R²和MAPE百分比目标，默认0.9 / 10；`hard_gate`默认false |
| `expert` | 价格评分、附带先验开关、默认分及字段类型 |
| `deployment.model_scope` | 默认`full_data`全量重拟合，可显式改`selected_split`部署选中划分快照 |
| `v3.nsga_fraction` | NSGA预算占比，默认0.6，其余给TPE |
| `v3.num_anchors` / `anchor_refresh_interval` | 锚点数和更新间隔，默认3 / 20 |
| `v3.local_fraction` | 模型参数局部范围占原搜索区间比例，默认0.25；对数参数在对数空间处理 |
| `v3.global_fraction` / `weight_only_fraction` | 全局逃逸和权重专门精修的调度比例，默认0.2 / 0.25，0表示停用；调度频率按倒数取整，全局逃逸优先 |
| `v3.startup_trials` / `ei_candidates` | TPE预热观测数及候选数，默认10 / 24；前阶段观测计入预热 |
| `v3.cache_entries` | 模型验证预测LRU缓存上限，默认256；0关闭。缓存不保存大量森林对象 |
| `v3.warm_start` | 自动或指定历史文件、参数文件及观测复用开关 |

V3默认自动寻找最近保存的V3历史，没有则寻找V2历史。数据、顺序、评分、字段编码、验证协议和内核身份完全一致时，历史成绩可以进入新搜索。任何一项变化，旧参数仅作提案并重新训练计分。显式`history_file`或`config_file`可覆盖自动查找；相对路径以包根目录为准。`auto_history=false`关闭自动查找。`max_history_trials`默认1000，先保留原选中候选，再保留Pareto候选和近期记录；`max_warm_proposals`默认5。新试验预算不把导入旧观测冒充新训练。

预测缓存包含数据身份、训练与验证记录的有序列表、编码、专家处理和完整成员参数；只改权重可以命中，需要新启用的模型若无缓存则实际训练。日志每轮打印阶段、进度、MAPE、R²、累计成员拟合和缓存命中。报告记录两阶段轮数、历史使用、候选池和缓存统计。缓存查找KeyError可退回原评估路径；TPE采样异常可在原参数范围内恢复；数值不稳定候选标为FAIL并排除，详见OPTUNA_V3_METRIC_FIX.md。

## 导出与指标解释

`price/training_report.json`、`best_config.json`、`optuna_trials.csv`、V3的`all_trials.csv`、`pareto_front.csv`、`study_history.json`和`price_attribute_scores.json`保留完整记录。`price_ensemble_bundle.pkl`为选中参数的全量模型，`selected_split_bundle.pkl`和`selected_split_predictions.csv`保留开发快照。服务使用`price_native_bundle.pkl`，导出后核对单条/批量及重新加载预测一致。

所有MAPE/R²明确标为参与搜索的开发成绩，独立外部测试未提供。默认全量部署模型会重拟合，不能拿开发划分快照的分数当作其独立测试成绩。虚拟示例验证软件流程和兼容性，不代表真实业务精度。

实现参考：[Optuna3.6的TPE](https://optuna.readthedocs.io/en/v3.6.2/reference/samplers/generated/optuna.samplers.TPESampler.html)、[Study.add_trials](https://optuna.readthedocs.io/en/v3.6.2/reference/generated/optuna.study.Study.html#optuna.study.Study.add_trials)、[sklearn0.24预处理](https://scikit-learn.org/0.24/modules/preprocessing.html)。
