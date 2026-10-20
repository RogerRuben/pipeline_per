# 一张成品表构建三环节初版模型

该入口面向快速试用：读取双行表头成品表，搜索价格模型与超参数，建立耦合效能基线，并生成可直接试运行的推荐系统成品草稿。构建产物写入新的目录。

```text
成品CSV/XLSX（中文名＋英文代号）
  → 属性类型、单位、缺失与允许值推断
  → 价格AutoML（模型＋超参数＋代表随机种子）
  → 偏好方向/权重＋耦合关系效能基线
  → 成品、属性、标签、耦合与历史记录草稿
  → 隔离三服务试运行（推荐、生成、编辑与显式重算）
```

## 直接试运行已经生成的初版

双击项目中的：

```text
outputs/product_pipeline_demo/START_PROTOTYPE_WIN7.bat
```

默认页面为 `http://127.0.0.1:18791/`，管理中心为 `/admin`。端口被占用时会自动选用空闲端口，以控制台显示的地址为准。关闭启动控制台或按 Ctrl+C 会停止本次启动的服务。试运行使用 `outputs/product_pipeline_demo/recommendation/data/protocol_demo.db`，启动脚本在该目录内激活已生成草稿。

模型、记录与评分来自虚拟数据，适合检验流程。效能方向、权重和可行性映射仍是试用假设。

## 一键构建

已有环境：`runtime/venvs/automl02438`，Python 3.8、scikit-learn 0.24.2、Optuna 3.6.0。

```bat
REM 无参数时使用内置虚拟数据，自动选择新的输出目录
BUILD_PRODUCT_PROTOTYPE_WIN7.bat

REM 使用示例双行表头工作簿；输出目录须为空或不存在
BUILD_PRODUCT_PROTOTYPE_WIN7.bat outputs\product_pipeline_input\虚拟成品_双行表头.xlsx --virtual-data --output outputs\my_product_trial

REM 替换为实际成品表与成品标识
BUILD_PRODUCT_PROTOTYPE_WIN7.bat 成品历史.xlsx --product-code PRODUCT_A --product-name "成品甲（试用）" --output outputs\product_a_trial
```

构建结束后运行输出目录中的 `START_PROTOTYPE_WIN7.bat`。缺少环境时先执行现有的 `CREATE_AUTOML_ENV_PY38_WIN7.bat`；运行已有环境即可使用本入口。

## 输入约定

第一张工作表，或 CSV，严格使用双行表头：

| 行 | 成品编号 | 成品名称 | 价格(万元) | 是否应急解锁 | 额定推力(N) | 电源类型 |
| --- | --- | --- | --- | --- | --- | --- |
| 1：中文名 | 成品编号 | 成品名称 | 价格(万元) | 是否应急解锁 | 额定推力(N) | 电源类型 |
| 2：英文代号 | product_id | product_name | price_wan | emergency_unlock | rated_thrust_n | power_type |
| 3起：记录 | V001 | 虚拟成品一 | 8.5 | 有 | 8000 | 24V |

英文代号以字母开头，只含字母、数字和下划线，全表不区分大小写检查唯一性。导入后保留原始代号拼写，重排列顺序或修改中文名不会改变属性身份。第一行各列须有中文名，括号内的内容用于单位。

编号、名称、价格、标签等业务元数据按中文列名识别；其余列为属性。价格输出统一为万元：注明元、千元或万元可自动换算，没有单位时按万元并写入提醒。价格必须为正；缺失价格的记录保留在业务草稿，但不用于价格训练。

支持数值、整数、布尔、IP等级、枚举与文本。枚举使用独热编码参与价格模型；文本保留为业务属性。数值和布尔进入效能试用模型，枚举和文本保留给价格预测、筛选与展示，不自动赋予效能偏好。0/1含义不明确时按枚举保留，需要人工确认。

空单元格默认缺失，额外缺失标识默认为 `-1`、`\`、`/`。若某属性的 `-1` 是有效值，例如真实温度，使用 `--missing "\,/"` 去掉该缺失标识。任一行缺失的字段会设为非必填。

价格AutoML至少需要12条有效价格记录、12组不同属性配置。完全相同的属性配置视为同组，重复记录不会增加独立验证样本数。样本过少或价格无变化导致R²无法评估时会明确报错。

## 价格搜索

候选模型：Ridge、Lasso、ElasticNet、BayesianRidge、SVR、RandomForest、ExtraTrees、GBDT。

默认24次搜索、3折、3个验证种子（42、137、256），180秒搜索预算。时间预算在当前试验结束后检查，不代表硬超时。每种候选模型先排入一次试验；时间预算太短时，报告会明确列出实际完成的模型。

流程为：

1. 用固定的独立划分种子保留20%的成品配置组作为测试集。
2. 仅在其余开发集做多种子分组交叉验证。缺失补全、标准化和独热编码在每个训练折内拟合。
3. 使用双目标搜索：最小化 `平均MAPE＋0.5×MAPE标准差`，最大化 `平均R²－0.5×R²标准差`。
4. 在满足平均MAPE/R²阈值的已完成试验中，选择两个稳健目标的折中结果。没有合格结果时保留试用模型并标记未达目标。
5. 从最接近多种子中位表现的验证种子中选取代表种子。不会选择分数最高的种子。
6. 全部选择完成后只评估一次独立测试集，报告测试MAPE和R²。最后用全部有效记录重训服务模型。

默认目标为MAPE≤10%、R²≥0.85，目标状态须开发集和独立测试集都满足。MAPE是绝对百分比误差均值，10表示10%，不会让正负误差相互抵消。独立测试指标来自最后全量重训之前的验证模型。价格区间来自开发集交叉验证残差，属于探索区间，尚未验证95%覆盖率。

可配置参数：

```bat
BUILD_PRODUCT_PROTOTYPE_WIN7.bat 成品历史.xlsx --product-code PRODUCT_A --product-name "成品甲" --trials 80 --folds 5 --seeds 42,137,256,512,1024 --target-mape 8 --target-r2 0.9 --timeout 600
```

`--models ridge,extra_trees,gbdt` 可缩小候选范围；`--log-target` 使用价格对数变换，预测自动恢复为原价格。

采用 sklearn Pipeline 避免预处理跨越训练/验证边界，参见 [官方数据泄漏说明](https://scikit-learn.org/stable/common_pitfalls.html)。MAPE百分比单位参见 [官方指标定义](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.mean_absolute_percentage_error.html)。

## 效能试用模型

同一 XLSX 可加两张可选配置表：

`属性配置`：

| 英文属性代号 | 效能偏好方向 | 试用权重 |
| --- | --- | --- |
| rated_thrust_n | 越大越好 | 2 |
| mass_kg | 越小越好 | 1 |

`耦合关系`：

| 源属性代号 | 目标属性代号 | 工程系数(可选) | 工程偏置(可选) | 绝对容差(可选) |
| --- | --- | --- | --- | --- |
| rated_thrust_n | peak_thrust_n |  |  |  |

系数留空时，对指定关系拟合线性经验区间；填写系数时采用 `目标＝系数×源＋偏置`，容差与目标属性单位一致。每个目标仅接受一条初版耦合，未知属性和非法容差会报错。

没有配置表时，依据中文名建议偏好方向，采用等权试用效用，并从完整数值样本中寻找少量相关关系建议；报告将其标记为待工程确认的统计关系。工作表顺序不能建立因果关系，样本范围不能当作工程极限。效能分数来自可解释的加权效用，风险来自耦合偏离与越界情况，可行性映射未经真实标注校准。

也可用 `--effect-config 效能配置.json`，结构示例：

```json
{
  "attributes": {"rated_thrust_n": {"preference": "higher", "weight": 2}},
  "couplings": [{"source": "rated_thrust_n", "target": "peak_thrust_n", "coefficient": 1.45, "offset": 0, "tolerance": 300}],
  "hard_rules": []
}
```

该产物使用现有 `snapshot_json` 后端，效能服务同时提供耦合范围给推荐生成器，用于调整候选方案。它属于初版基线，尚未学习真实专家偏好，也不提供V11逐请求动态协议评分。后续可替换为原专家效能工程或冻结运行包，保留英文属性代号即可继续使用业务草稿。

## 产物与复用

```text
输出目录/
  input/                              输入原文件快照
  price/price_native_bundle.pkl       现有价格服务可加载的原生模型
  price/training_report.json          选型、种子、交叉验证与独立测试指标
  price/search_trials.json            搜索记录和Pareto候选
  price/leaderboard.csv               已完成候选的指标比较
  price/seed_report.json              代表种子依据
  effectiveness/effectiveness_bundle.json  耦合效能试用模型
  effectiveness/bootstrap_report.json     偏好、权重、耦合来源与限制
  business_release.iprelease.json     带SHA256校验的成品草稿发布包
  recommendation/                    独立业务数据库与中文前台
  prototype_manifest.json            三环节文件和摘要
  pipeline_status.json               当前阶段、失败原因或完成状态
  构建结果.md                         结果概览
  START_PROTOTYPE_WIN7.bat            初版启动入口
```

发布包可在现有管理中心“成品数据工作区→导入离线发布包”中导入为草稿。价格pkl可由现有价格服务的 `--model` 指向；效能JSON由效能服务 `--package "" --snapshot <文件>` 指向。切换现有业务数据仍沿用已有数据中心流程。

初版目录的摘要用于发现意外修改，不是发布者认证。原生pkl只加载本流程生成的可信模型，并使用与训练兼容的环境。初版snapshot不符合正式效能运行包交付标准，不能直接冒充正式专家模型交付包。

## 验证

```bat
RUN_PRODUCT_PROTOTYPE_TEST.bat
RUN_FULL_PIPELINE_TEST.bat
```

专项验证包括双行表头与英文代号、旧单行表头兼容、CSV/XLSX一致性、重复配置分组、训练折内预处理、耦合破坏响应、模型摘要、草稿发布包校验、未达目标标记，以及真实HTTP推荐、生成与显式重算。专项测试在临时目录执行，并比较现有数据库和已安装模型的摘要。
