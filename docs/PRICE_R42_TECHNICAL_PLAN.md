# R4.2：从弱搜索改为可配置CV、纯数据模型及联合预测

本次触发：16条成品、36属性，R4.1外层MAPE 46.224%、R²为负，开发MAPE 26.932%；多轮停止于TARGET_UNMET_AFTER_RESTART。实际表与报告不便提供，本版改造及实验不声称复现该表，也不保证该表已经达标。

## 判断

TARGET_UNMET_AFTER_RESTART是搜索策略停止，不是程序崩溃。原策略初搜32次、默认重选24次，仅对少量固定配置换种子。5000个模型种子不能替代新模型、新特征表示及新的超参数。开发与外层均差时，应先查训练误差，分辨欠拟合、缺失价格因素、类型/数据问题和选模过拟合。

本次允许更弱正则、更复杂模型和更广开发搜索。开发验证、训练拟合、外层审核同时报告；反复用于挑选模型的验证集属于开发数据。修改CV可以合理地改变训练占比、价格覆盖和重复次数；筛选许多划分后挑最好分数会引入选择偏差，不能据此保证新成品准确率。

R²≥0.9等价于RMSE≤0.316×本批价格标准差。若成品价格本来接近，满足10%MAPE仍可能远达不到R²门槛；新版accuracy_constraints会给出对应的RMSE上限及实测值，便于判断两个目标哪个更严格。

## 已实施

1. CV支持inner/outer_strategy=auto、loo、group_kfold、price_balanced。内外层分别配置folds、repeats和共同seed；相同属性的重复记录始终同组。重复验证保存累计预测与覆盖次数，取每条记录全部折外预测的均值，不覆盖为最后一次预测。price_balanced把相邻价格等级分配到不同折，避免部分折集中于某个价格段。外层政策在运行前固定，内层所有补全、编码、筛选、PCA和多项式拟合都只用当前训练折。
2. 纯数据开关expert.enabled=false：原始1—5分和类型声明保留，价格先验、专家子集与分组倍率不参与训练。前台明确显示评分未启用。另有专家参与的对照配置。
3. 模型加入PCA+Ridge和折内筛选后的二/三阶多项式Ridge；GP支持RBF/Matérn并优化核尺度；核岭比较RBF、Laplacian、多项式、线性核；SVR比较线性、RBF、多项式。保留相对损失回归、稀疏回归、PLS和树模型。
4. 筛选尝试1/2/3/5/8/12个属性；nonlinear统计同时检查线性、绝对值、平方关系。多项式最多从32列展开，不对36属性盲目生成所有高阶交互。筛选数量和复杂度仍通过内层验证选择。
5. Ridge范围1e-8—1e6；Lasso/ElasticNet 1e-7—1；SVR C 1e-3—1e6、gamma 1e-7—100、epsilon 1e-5—0.5；核岭alpha 1e-9—1e3；GP噪声1e-8—1、尺度0.01—1000；树深至12。先按尺度做系统覆盖，再TPE细调；扩大范围同时保留中间尺度，避免“范围更宽、网格反而更疏”。
6. 融合默认至少2个候选、最多6个，权重非负、和为1，单个份额不超过95%，每个保留份额至少3%。使用同一开发折外预测比较并优化MAPE/R²联合目标，尝试满足两个目标的约束解。先优化当前组合再考虑增加成员，避免未优化的两模型误导第三模型选择。候选不足时报告ensemble_requirement_met=false，不能伪造多个有效模型。
7. stop_when_unmet_stagnant=false时，重选次数用尽后不因未达标停滞退出，继续至种子上限或时间预算。重选默认最多40轮，每轮32次；5000仍指固定参数种子探索上限，系统初始化/TPE是另计的参数探索。每轮搜索有自己的时间上限，避免第一折耗尽整个任务预算；已完成全部外层预测时，某折搜索到时不再被误判为审核未完成。
8. 同时输出training_fit_metrics、cv_metrics、nested_cv_metrics、baseline_outer_metrics、generalization_gap_mape_pp，以及逐条审核误差、覆盖次数、模型份额、实际CV设置和停止原因。训练低误差而审核高误差指向泛化/选模不稳；训练本身也差，则检查模型表示、数据类型及价格信息是否充分。

## 配置及运行

补丁解压到原R4/R4.1根目录，先结束旧训练进程。

- APPLY_R4_2_DATA_ONLY.bat：纯数据，内外层8折各重复2次，初搜128次，参数重选最多40轮，种子最多5000次。单次搜索150秒、总预算3600秒。16条时外层每次训练14条、验证2条，不保证比留一更优。
- APPLY_R4_2_QUICK.bat：先诊断用，内外层5折各1次，初搜48次，每轮60秒、总预算900秒。
- APPLY_R4_2_EXPERT_COMPARE.bat：与DATA_ONLY相同搜索和CV，开启原专家评分作比较。
- APPLY_R4_2_LOO_AUDIT.bat：纯数据，外层恢复留一；用于检查CV改变的敏感性。

随后RETRAIN.bat训练；START.bat继续只加载保存模型。脚本先备份配置及清单，保留原始评分、类型、缺失标记及原质量门槛，不放宽MAPE/R²标准。配置应用会替换搜索、CV、融合和资源设置，请查看备份与effective_config.json。quality.hard_gate=true时审核精度及融合数量未同时满足便拒绝发布；false时仍可生成明确未达标的试用版本。

## 后续更广的方向

- 开发机可用现有TabPFN现代环境作为独立对照：它的预训练先验可能比手工算法更适合部分微样本任务，但不能保证此业务表现；它与Win7本地3.8运行路线分开。本次补丁没有把TabPFN伪装成0.24本地候选。
- 在有可验证的业务关系时加入型号族、价格时点、采购数量、材料/尺寸等价格决定因素。没有价格信息的属性无法靠更多随机种子补出来；原有1—5分仅代表重要性，不能直接当加价系数。
- 研究分层成本/残差、特征组约束和局部相似成品模型；前提是数据确有产品族和可用组别。16条不盲目再分成许多更小群组。
- 增加新历史成品、纠正同配置异价/单位混用。对异常点保留逐条诊断，不靠自动删除难预测记录来制造达标。

## 参考

- [scikit-learn 0.24 RepeatedKFold](https://scikit-learn.org/0.24/modules/generated/sklearn.model_selection.RepeatedKFold.html)：重复CV及可复现划分。
- [scikit-learn 0.24 Ensemble methods](https://scikit-learn.org/0.24/modules/ensemble.html#stacking)：加权集成与折外预测用于组合训练。
- [scikit-learn 0.24 GaussianProcessRegressor](https://scikit-learn.org/0.24/modules/generated/sklearn.gaussian_process.GaussianProcessRegressor.html)：核参数优化能力。
- [Cawley & Talbot, JMLR 2010](https://www.jmlr.org/papers/v11/cawley10a.html)：小样本模型选择过拟合及评估偏差。
- [Hollmann et al., Nature 2025](https://www.nature.com/articles/s41586-024-08328-6)：TabPFN的小样本表格建模路线。
