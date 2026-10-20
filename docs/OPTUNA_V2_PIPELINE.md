# 提供的Optuna AutoML V2：流水线与成品导出适配

2026-10-16双版本包的入口、类型说明与对比流程以[双版本使用说明](OPTUNA_V2_V3_PIPELINE.md)为准。

此版本接入用户提供的完整代码ZIP（SHA256：531ed2fe033b1178910aeda812e35edac8c2f93057af158c82bafd282292947c）。price_automl_core.py逐字节保留，NSGA-II双目标、真实划分种子搜索、六模型参数范围、专家倍率、零权重阈值和Pareto选解规则均沿用提供版本。

## 使用

已有R4/R4.1/R4.2/R4.3离线目录：结束旧服务，完整解压本PATCH覆盖到能看到START.bat、runtime、tools的原目录。运行APPLY_OPTUNA_V2.bat，再CHECK_INPUT.bat、RETRAIN.bat。

- APPLY_OPTUNA_V2.bat：首次默认150轮；已有本引擎配置时保留手动调整的轮数。
- APPLY_OPTUNA_V2_QUICK.bat：切换到10轮检查流程。
- APPLY_OPTUNA_V2_DEEP.bat：切换到5000轮联合搜索；默认不设时间截断。
- START.bat：直接启动最后成功保存的版本，不读取训练表、不训练。
- EXPORT.bat：把最后成功保存的价格模型、效能模型、推荐草稿及原Python环境导出为ZIP。导出不训练，运行包不需要原训练表。

运行包不包含训练表、缓存、日志或推荐库的中间备份。Win7建议解压到较短目录，例如D:\ProductModel，避免系统路径长度限制。

应用配置本身不会训练，旧保存模型继续可用；RETRAIN完成后才切换新运行版本。配置和完整性清单自动备份。新配置在config/price_optuna_v2.json，旧R4配置保留。

## 输入与专家评分

成品数据仍为双行表头：第一行中文显示名，第二行英文唯一代号，第三行起为记录。表内成品编号、成品名称、价格列由现有导入识别；不会再默认删除第一列价格特征。价格单位沿用流水线的万元。

可选工作表“价格属性评分”填写“英文代号、中文名、价格重要性评分”等列，评分为1—5分。JSON expert.scores也可给出；工作簿同名属性评分优先。保留旧R4的价格评分与目标，新引擎默认开启原版本专家机制。expert.enabled=false用于纯数据对照。未显式评分时，对提供代码附带的25属性先验按代号、中文名或中文名+单位做精确匹配，不模糊猜测；其余按所提供版本使用1分，可设置expert.default_score。expert.use_supplied_defaults=false可关闭附带先验。

原版本对稀疏回归、岭回归和支持向量使用各自的倍率，对专家树使用分裂增益加权，倍率在内部处理；不将属性评分当作模型融合百分比，也不写入效能偏好。前台显示中文属性和模型名称。

属性按数值或配置类型处理。数值在训练集上MinMax缩放；配置在训练集内独热编码，编码与原属性对应关系保存在价格包中，不把配置代码当成顺序。显式声明的配置保留原始文本，包括001和IP67等代码。Cluster、is_outlier等诊断列沿用原规则排除。价格须为正数，最低记录数由input.min_samples配置（默认4，不能低于4），训练与验证各至少2条；auto_adjust_test_size默认自动调整不可行比例。对带价格记录中缺失的输入属性明确报错，不自动填充或默默删记录；整列为空的属性在报告中列为未参与。默认不启用异常值删除；显式input.remove_outliers=true时沿用提供版本的特征空间IsolationForest参数，并输出移除记录清单。

“效能属性配置”和“耦合关系”仍为可选，缺省时不自动发现耦合。价格评分不会用作效能权重。

## 可调整入口

config/price_optuna_v2.json包含search.n_trials、optimizer_seed、seed_low/high、timeout_seconds、validation.test_size/price_bins、targets、expert以及deployment.model_scope。MAPE目标在本配置中用百分数（例如10），适配器传给原内核时转换成0.1。

默认模型参数范围和优化方法来自原ZIP代码，未叠加R4的嵌套审核、样本权重扰动、筛选、校准或强制多成员规则。每轮trial是一次划分种子+六模型参数+六项权重的完整联合评估。权重低于原内核阈值的模型不参加拟合和导出。timeout_seconds默认null，若主动设置时间预算，将报告实际完成轮数；单次拟合不能中途截断。

## 导出产物与成绩含义

每次训练在outputs/run_*里同时产生：

- price/price_ensemble_bundle.pkl：原算法选参后在全量数据上重拟合的完整价格包。
- price/selected_split_bundle.pkl：选定开发划分的模型快照。
- price/price_native_bundle.pkl及manifest：接入现有价格服务、效能和推荐流水线的原生价格包。
- price/best_config.json、optuna_trials.csv、pareto_front.csv、selected_split_predictions.csv、training_report.json、price_attribute_scores.json：参数、轮数、种子、融合权重、每条预测与专家评分。
- effectiveness/effectiveness_bundle.json、business_release.iprelease.json、recommendation/和prototype_manifest.json：原流水线效能、推荐草稿及三组件完整性记录。

默认deployment.model_scope=full_data，与提供版本相同；可改selected_split部署所选划分快照。两套模型分开保存，不拿全量训练拟合值冒充验证分数。

R²/MAPE来自被Optuna搜索过的开发划分，不是独立外部测试成绩。全量模型只是使用选中配置重新拟合，不能直接继承开发分数作为未来精度保证。若targets.hard_gate=true且开发不达标，新报告保留而旧运行版本不切换；默认false保持提供版本的试用导出行为。

## 预测一致性与环境

服务按保存的特征顺序解析原始字段，配置编码后只做一次MinMax；各模型施加原专家倍率、预测log价格并只还原一次exp。服务对实际非零权重成员做同样的价格尺度加权。导出时核对算法包/原生包、单条/批量及重载预测一致，保留开发快照逐记录重放结果。

复用既有Python3.8.10、numpy1.19.5、scipy1.5.4、pandas1.3.5、sklearn0.24.2和Optuna3.6.0环境。双行Excel由已有解析器读取，不需要运行Notebook或新增openpyxl、matplotlib、Jupyter。不要在原离线包上用上游requirements文件覆盖已有环境。开发机使用以上固定版本验证；Win7实机未在本次环境执行。

## 相对提供代码的适配范围

核心文件未修改。optimizer.py增加已解析数据、专家评分、进度回调、可选超时、类型适配与搜索编排接口，保存开发快照和可复用试验历史。V2沿用NSGA-II；V3通过单独的编排器接入。原objective、参数建议函数、权重归一化、Pareto排名和默认全量重拟合逻辑保留，纯数值V2与原脚本做预测对照。

提供双行输入适配和原生服务成员包装；保留现有START/RETRAIN/EXPORT入口。软件验证使用虚拟数据，不代表用户真实数据已达精度门槛。
