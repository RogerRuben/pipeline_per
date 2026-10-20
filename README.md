# 工业技术协议智能推荐系统与 AutoML 快速构建流水线

当前源码基准：**推荐系统 V19.6.14 + AutoML V4**，目录整理于 **2026-10-20**。

本仓库包含两个相互配合的部分：推荐系统负责使用已有模型和成品数据完成推荐、方案生成与显式重算；AutoML流水线负责从处理好的成品表训练价格模型、建立效能试用快照，并导出推荐系统可用的成品草稿。

## 源码分区

| 目录 / 入口 | 用途 |
| --- | --- |
| [`app/`](app/README.md)、`run_app.py` | **推荐系统源码**：HTTP后端、中文前台、数据中心、历史方案推荐、候选生成和业务草稿管理 |
| [`automl_pipeline/`](automl_pipeline/README.md) | **快速建立流水线源码**：成品表接入、V2/V3/V4价格训练适配、效能基线、推荐草稿构建与版本切换 |
| `services/price_service/` | 独立价格推理服务、原生bundle导出和可复用价格模型实现；`optuna_v2/`保留原V2核心及V3搜索，`optuna_v4/`为V4搜索与复核 |
| `services/effectiveness_service/` | 独立效能服务及已有专家工程产物接入 |
| `tools/` | 离线环境、打包、验证和历史入口；迁移到流水线目录的旧命令保留兼容转发 |
| `tests/`、`docs/` | 自动化验证、输入规范、部署说明与历史方案记录 |
| `config/` | 推荐系统运行配置；流水线的默认训练配置位于`automl_pipeline/configs/` |

详细边界和兼容关系见[源码目录与产物边界](docs/CODEBASE_LAYOUT.md)。推荐系统启动不调用AutoML训练；流水线在新的输出目录构建产物，随后由推荐系统消费。

`BUILD_SOURCE_DEPLOYMENT_NO_WHEELS.bat`生成的是推荐系统及HTTP模型服务的操作端源码部署包，不包含AutoML训练区；需要开发快速建模流程时使用完整Git源码。

## 使用推荐系统

推荐系统的业务后端使用Python标准库；价格和效能模型分别使用与其产物匹配的环境。Windows现场安装及三服务部署见[当前部署说明](docs/CLEAN_WIN7_DEPLOYMENT_V19_6_13.md)。

在已配置环境中启动三个服务：

```bat
START_ALL_SERVICES_WIN7.bat
```

只启动推荐系统后端：

```bat
python run_app.py --no-browser
```

默认地址：

| 服务 | 地址 |
| --- | --- |
| 推荐系统 / 数据中心 | `http://127.0.0.1:17891/` / `http://127.0.0.1:17891/admin` |
| 价格服务 | `http://127.0.0.1:18101/docs` |
| 效能服务 | `http://127.0.0.1:18102/docs` |

服务地址由`config/model_services.json`配置。推荐系统依据两个模型的schema建立字段目录，并发送完整方案JSON；各模型负责自己的字段选择和预处理。修改属性后需显式点击“重新计算价格与效能”，保存专家方案时会校验本次计算与当前参数一致。

数据中心支持成品草稿维护、校验、离线业务发布、备份及切换。业务数据、价格模型和效能模型的发布操作分别管理，详见[统一交付](docs/PRODUCT_DELIVERY_WIN7.md)和[操作人员手册](docs/操作人员手册_测试数据运行与成品更换.md)。

## 从一张表快速建立成品

### 源码运行

V2/V3/V4主线使用 **Python 3.8.10 x64、scikit-learn 0.24.2、Optuna 3.6.0**，依赖见[`automl_pipeline/requirements.txt`](automl_pipeline/requirements.txt)。请使用独立训练环境；已有正式模型仍按自己的依赖清单运行。TabPFN使用独立Win11环境，见[实验方案说明](docs/TABPFN_WIN11_WIN7_DELIVERY.md)。

```bat
python -m pip install -r automl_pipeline/requirements.txt
python -m automl_pipeline --help
```

下面以仓库中的**虚拟16条、36属性样本**验证流程，快速配置与构建结果均写入新目录：

```bat
python -m automl_pipeline config --profile quick --output outputs/my_first_product/price.json
python -m automl_pipeline inspect automl_pipeline/examples/virtual_16x36.xlsx --price-config outputs/my_first_product/price.json
python -m automl_pipeline build automl_pipeline/examples/virtual_16x36.xlsx --price-config outputs/my_first_product/price.json --output outputs/my_first_product/build --virtual-data
python -m automl_pipeline serve --output outputs/my_first_product/build --activate-draft --no-browser
```

新入口默认使用V4。正式训练时将示例路径换成自己的CSV/XLSX，去掉`--virtual-data`。`config`生成配置，`inspect`只检查，`build`才训练，`serve`只运行指定的保存结果；已有输出目录不会被覆盖。默认配置可在[`automl_pipeline/configs/`](automl_pipeline/configs/)中查看，`--price-engine optuna_v2`或`optuna_v3`可切换引擎。

### 输入表约定

第一行填写中文名称，第二行填写**唯一英文属性代号**，第三行起填写成品记录。价格建议采用`price_wan`（万元）；前台属性统一显示中文。

属性只有**数值**与**配置类型**两种。1—5、80、7200这样的不同量级可直接作为数值，训练内部按当前训练子集缩放；配置使用训练子集独热编码。数字形式的配置代号也应明确声明为配置类型，才能保留`001`等原始取值。

专家提供的是**价格属性重要性1—5分**，内部转换为模型倍率，并与学习得到的融合份额分开记录。可以关闭专家评分做纯数据对照。只要求填写成品数据；效能偏好、专家评分与耦合表可选，没有耦合时不自动编造。

主线最低支持4条有效价格记录，训练与验证各至少2条。输入应已处理完整，价格为正；固定外层留出模式至少需要6条，并需声明全部属性类型。

### 价格模型版本

| 版本 | 当前实现 |
| --- | --- |
| V2 | 保留提供版本的六模型核心，联合搜索划分种子、超参数及非负融合权重 |
| V3 | 在同一核心上采用NSGA-II及多锚点TPE搜索，提供缓存、历史参数复用及异常候选隔离 |
| **V4（默认）** | 可切换随机划分、全划分统一搜索、逐划分搜索、混合复核；增加训练门槛、实际全量重拟合检查、跨划分复核与参数组合融合 |

V4默认混合复核，训练和开发目标为MAPE≤10%、R²≥0.9，未达标允许输出明确标记的试用草稿。默认300轮配置预算，模型种子范围0—10000；预算、划分数量、专家评分、训练门槛、融合和部署范围均可配置。

**开发成绩参与了参数及融合权重选择，不能作为独立测试成绩。** V4分别记录划分训练、最终部署训练、跨划分开发及可选固定外层留出成绩；默认“全划分”受64个划分上限约束，需要穷举时显式配置。更多预算不保证达标。配置、评估口径与报告字段见[V4完整说明](docs/OPTUNA_V4_PIPELINE.md)。

### 已交付的完整离线包

离线包与源码仓库分别管理，ZIP、Python环境和新训练产物不放入Git。仓库中保留的基线虚拟模型用于演示和回归。当前已交付的基准文件为`ProductAutoML_V4_Offline_20261016.zip`及`ProductAutoML_V4_PATCH_20261016.zip`；本地副本位于被Git忽略的`deliverables/`，Git克隆不会附带这些文件。

在完整离线包根目录使用：

| 操作 | 入口 |
| --- | --- |
| 选择V2/V3/V4及V4模式 | `MODE_MENU.bat`、`SELECT_V2.bat`、`SELECT_V3.bat`、`SELECT_V4.bat` |
| 更换数据后检查 / 训练 | 替换`input/products.xlsx`，运行`CHECK_INPUT.bat` / `RETRAIN.bat` |
| 直接使用所选模式保存模型 | `START.bat`，不会自动重训 |
| 导出当前模型和环境 | `EXPORT.bat`，导出包不需要原训练表 |

这些操作BAT由打包工具生成，**不等同于源码根目录的入口**。不同模式的保存模型分别登记，切换到未训练模式会提示先训练，不借用其他模式结果。打包脚本位于`tools/build_product_v4.py`，完整离线重打包还需另外准备固定环境及演示产物，见[流水线源码说明](automl_pipeline/README.md)。

## 验证

在上述Python3.8训练环境中，从仓库根目录运行：

```bat
python tools/verify_product_optuna_v2_tests.py --report outputs/verification/legacy.json
python tools/verify_product_v4_tests.py --report outputs/verification/v4.json
python -m unittest discover -s tests -p source_layout_test.py -v
```

推荐系统的完整HTTP及数据生命周期验证：

```bat
python tests/full_pipeline_test.py
python tests/complete_parameter_payload_and_admin_save_test.py
```

2026-10-16的交付基准已验证165项检查，以及8组4—7条、40属性的实际BAT训练、模式切换和带环境导出。涉及旧交付ZIP的验收需要对应离线资产；源码克隆缺少旧ZIP时会跳过该资产专用测试。虚拟样本只用于验证软件链路，不能证明真实业务精度；当前固定环境已在Windows开发机验证，实体Win7未复测。

## 版本管理边界

Git保存源码、配置模板、虚拟测试样本、文档和测试。运行数据库、真实输入、专家现场配置、训练输出、日志、依赖wheel、环境及交付ZIP保留在本机或单独交付。首次运行时，可由仓库中的虚拟CSV初始化本机数据库。

历史V1/R4、原始Notebook及TabPFN代码保留作对照，当前快速建模主线以`automl_pipeline/`和V4说明为准。推荐系统原有多类型指标与DataMaster格式继续使用自己的业务规范；流水线的双行表头及两种属性类型是快速建模的输入约定。
