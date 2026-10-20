# TabPFN价格版本：Win11训练服务与Win7验收客户端

本交付使用两个独立环境。Win11开发机运行Python3.11.9、TabPFN2.0.9、CPU版PyTorch2.5.1、sklearn1.5.2；Win7验收机运行Python3.8.10、sklearn0.24.2和现有推荐/效能程序。**验收时Win7须能通过局域网访问Win11价格服务**。两边都能离线安装，模型执行不依赖公网；Win7不会执行TabPFN模型。

## 现场步骤

开发机直接试用：完整解压后，先双击`INSTALL.bat`，安装通过后双击`START.bat`，会载入包内已训练示例并打开价格、效能与推荐页面。无需先训练，不使用系统PATH中的Python。原长文件名入口仍可使用。`TRAIN_TABPFN_PRODUCT_WIN11.bat`用于主动训练自己的新数据。

安装器显式安装`tqdm`，在新的干净目录安装完整锁定依赖；检查所有模块真实导入、TabPFN实际预测、已保存bundle实际预测后才切换环境并显示成功。失败会保留原环境，详情见`logs/install_win11_failure.json`或`logs/install_win11_verification.json`。启动时也检查环境，缺失时自动离线修复。现代版本不导入sklearn0.24的旧训练检查；训练API支持范围按TabPFN2.0.9声明的sklearn1.2—1.6检查。本包仍固定安装已验证的sklearn1.5.2以匹配保存模型。

1. Win11 x64：解压`TabPFN_WIN11_CPU_OFFLINE.zip`，运行`INSTALL_TABPFN_WIN11_OFFLINE.bat`。安装只使用包内Python和带SHA256的本地wheel，不更改系统Python。若缺少VC运行库，可先运行包内`assets/vc_redist.x64.exe`。
2. Win11：运行`START_TABPFN_PRICE_WIN11.bat`。默认加载随包提供的融合模型，监听18901端口。两机须能访问该端口；按现场网络要求允许入站连接。保持服务运行，浏览器访问`http://Win11机局域网IP:18901/health`确认`status=ok`。
3. Win7 SP1 x64：解压`TabPFN_WIN7_LAN_CLIENT_OFFLINE.zip`，运行`INSTALL_TABPFN_WIN7_CLIENT_OFFLINE.bat`。然后执行`START_TABPFN_DEMO_WIN7.bat http://Win11机局域网IP:18901`。打开本机输出的推荐页面，默认18991端口。效能在Win7本机计算，价格在Win11实时计算。

启动器只激活解压包`prototype/recommendation`内的业务草稿。它校验远端价格模型摘要、成品代号、本地效能和业务包摘要；服务或版本不符会明确失败，不会静默换成其他价格模型。停止客户端不会停止Win11价格服务。

源项目直接运行时，Win7启动器也支持现有`runtime/venvs/automl02438`环境，或设置`TABPFN_WIN7_PYTHON`为Python3.8解释器绝对路径。

## Win11训练和比较

包内附带原有双行中英表头的10条/50属性工作簿。运行`TRAIN_TABPFN_PRODUCT_WIN11.bat`会在新的输出目录完成价格训练、预设耦合效能和推荐草稿。英文属性代号用于计算，前台显示中文。

实际输入和设置示例：

```bat
TRAIN_TABPFN_PRODUCT_WIN11.bat "成品表.xlsx" --price-config "price_config.json" --product-code MY_PRODUCT --product-name "我的成品"
```

默认修正配置为`docs/examples/price_tabpfn_micro_robust.config.json`，诊断旧配置保留于`price_tabpfn_attribute_scores.config.json`。`expert.scores`使用价格输入属性的唯一英文代号及原始1—5分，未评分使用中性3分。示例分值用于软件测试，不能替代真实专家意见。

XLSX可增加与R4相同的“价格属性评分”工作表：英文代号、中文名称、价格重要性评分，以及可选属性类型、专家编号、价格属性组。多专家记录保留并取中位数；工作簿的已评分属性覆盖同名JSON分值，空表不清空JSON。未知代号、重复和越界会报错。效能偏好及耦合独立读取，不使用这些价格评分。

旧`price_tabpfn_win11.config.json`是早期模型族份额演示配置，保留兼容，不代表本次属性评分方案。属性1—5分不会自动变成模型融合份额、信任系数或带货币单位的成本系数。只有明确提供实际工程成本参数时才启用成本模型；当前默认示例不提供成本公式。

本机三环节自测可运行`START_TABPFN_LOCAL_WIN11.bat`，或传入新训练的输出目录。它会启动本机价格、效能与推荐服务。生成模型须配合对应的业务草稿：替换现场模型时，将新输出目录同步给Win7客户端，通过`--output`指定。

只测试纯TabPFN时：

```bat
runtime\tabpfn311\python.exe -m services.tabpfn_price_service.app --model prototype\price\tabpfn_only_bundle.pkl --port 18911
```

纯TabPFN接口可在18911端口的在线JSON页测试。Win7验收默认绑定融合模型，避免把两种方法的指标混在一起。

## 指标与方法

所有目标价格须为正数。平均误差百分比定义为**MAPE = mean(abs(预测价格−实际价格)/实际价格) × 100%**；R²同时作为选择、融合和验收指标。默认门槛为MAPE≤10%、R²≥0.85，均可配置。

候选包括原价均值、对数价格均值、原价中位数TabPFN，以及岭回归、贝叶斯岭回归。配置提供评分后，还比较评分至少4分的TabPFN属性子集及R4属性先验岭回归。完整属性候选保留，避免强制丢弃低评分属性；阈值和候选先验强度可以调整。

TabPFN会自行预处理输入，因此简单在输入上乘分值可能被内部缩放抵消。本版本把评分用于专家属性子集候选，不声称这样能直接改变底座的注意力权重。属性先验岭回归复用R4：在折内补全、编码、缩放之后施加`2^(strength*(score−3)/2)`并整体归一化，影响正则化结构；强度0作为无先验对照。分值不会固定价格变化的正负方向，也不是因果结论。

固定随机种子流逐步平均。微样本先以同一折外数据比较配置，仅晋升最佳神经配置至微样本种子预算；不强制TabPFN进入最终预测。外层每个训练部分重新执行整个候选比较、种子扩展和融合权重学习。验证按相同属性配置分组，重复记录不会跨折。

10条样本默认完整嵌套分组留一：外层训练9组、内层训练8组。外层单条不计算R²，汇总所有折外预测计算两个指标；没有独立外部表时明确显示未提供。先前两条反复查看的记录不再称为独立保留集。更多数据可使用`holdout_nested`。具体问题、修正和独立虚拟检验见`docs/PRICE_TABPFN_MICRO_FIX.md`。

融合为非负权重、总和1，默认由OOF上的MAPE与R²学习，不预设工程40%/线性30%/TabPFN30%。失败候选被记录，较差成员允许零权重；极小权重通过重新优化删除。旧模型族约束模式保留兼容，需要另外显式配置，不能用属性1—5分填入。

可以调整`tabpfn`的预测成员数、温度、拟合模式、候选目标变换、输出均值/中位数和官方推理配置；`baselines`的alpha、特征数、模型开关；验证划分和重复数；种子预算；`expert`的原始属性1—5分、子集阈值、先验强度和可选分组强度；MAPE/R²门槛；线程数、单次拟合和总时间预算。模型族份额约束为独立的兼容选项。未知主配置字段会报错。此版本比较配置候选，不对TabPFN底座做神经网络微调。

## 试用结果和边界

当前微样本修正版重新训练并记录原价/对数、完整属性/专家子集及先验强度对照。准确数值以`prototype/price/training_report.json`为准；`virtual_generalization_report.json`记录冻结模型对60条新虚拟记录的结果。`price_attribute_scores.json`保存50个属性的实际分值、是否提供及原始专家记录。前台分别显示原始属性评分、模型贡献、由数据学习的融合份额。

质量硬门槛阻止未同时达到MAPE/R²目标的构建发布；打包工具默认也拒绝未达标包。软件链路通过、数值目标达到和真实业务精度认证分别记录。

只有10条记录，以上结果用于验证实现和数据流，不能证明真实成品的泛化精度。报告保持`business_accuracy_certified=false`和样本证据不足；探索区间没有95%覆盖率认证。效能仍是预设权重和3条经验耦合，尚未使用真实效能标签训练。

这套方法已进行实际CPU模型训练和Python3.8跨进程HTTP验证。Win7操作系统实机认证仍需现场测试。已有虚拟指标不能替代现场真实数据验收。

## 可追溯交付

包内包含Python压缩包、完整wheelhouse、固定版本和逐文件SHA256；Win11包另含公开TabPFN-2回归权重和对应许可证。安装使用`--no-index --require-hashes`并运行`pip check`。ZIP旁提供`.sha256`，`delivery_manifest.json`和`assets/assets_manifest.json`记录文件摘要。模型重新载入使用自己的本地权重路径，不依赖开发机训练路径。

原始来源：[TabPFN2.0.9](https://pypi.org/project/tabpfn/2.0.9/)、[公开回归权重及许可](https://huggingface.co/Prior-Labs/TabPFN-v2-reg)、[Python3.11.9](https://www.python.org/downloads/release/python-3119/)、[Python3.8.10](https://www.python.org/downloads/release/python-3810/)。

## Win7是否可以训练

可以在Win7原生训练兼容的Python3.8/sklearn0.24传统价格模型。现有R4离线训练版已经包含Optuna及属性评分输入，见`docs/PRICE_R4_IMPLEMENTED.md`。

TabPFN本包采用官方Python3.11环境，不能仅靠压缩包/venv封装在Win7原生运行。Python3.11官方支持Windows8.1及以上，官方Win7路线为Python3.8；TabPFN2.0.9要求Python3.9及以上。[Python系统支持范围](https://docs.python.org/3.11/using/windows.html)、[TabPFN2.0.9要求](https://pypi.org/project/tabpfn/2.0.9/)。运行库/系统接口兼容与Python依赖隔离是两件事。
