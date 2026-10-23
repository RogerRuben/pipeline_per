# AutoML → 快速建立成品流水线

本目录是V2/V3/V4/V5快速建模的实际源码。推荐系统位于[`../app/`](../app/README.md)，价格和效能HTTP服务位于`../services/`。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `__main__.py` | 源码命令：生成配置、检查输入、构建、运行保存结果；默认V5 |
| `product_pipeline.py` | 从成品表构建价格模型、效能快照及推荐业务草稿，管理构建状态与独立三服务试用 |
| `product_price_optuna_v2.py` / `product_price_optuna_v3.py` / `product_price_optuna_v4.py` | 各版本配置和训练适配入口 |
| `product_optuna_v4_adapter.py` | V4划分、搜索、实际重拟合检查、原生导出及报告 |
| `product_price_optuna_v5.py` / `product_optuna_v5_adapter.py` | V5基线保留、预测库融合、条件搜索、恢复与双范围导出 |
| `product_optuna_v2_input.py` | 离线包共用的双行表头、字段类型、专家评分及输入检查 |
| `product_effectiveness_bootstrap.py` | 可选效能偏好与已声明耦合关系的试用快照 |
| `product_modes.py` / `product_optuna_versions.py` | 保存模型模式登记、V2/V3比较及选择 |
| `configs/` | 完整默认配置模板；现场设置应写到自己的配置文件 |

算法实现保留在`services/price_service/optuna_v2/`、`optuna_v4/`及`optuna_v5/`，原生推理类继续使用原模块路径，兼容已保存的模型。通用字段编码和旧V1/R4辅助工具保留在`tools/`；兼容的旧`tools/product_*.py`入口转发到本目录同一模块对象，原有导入、命令和测试替换依旧有效。

## 源码用法

在仓库根目录、Python3.8独立训练环境中运行：

```bat
python -m pip install -r automl_pipeline/requirements.txt
python -m automl_pipeline config --profile quick --output outputs/example/price.json
python -m automl_pipeline inspect automl_pipeline/examples/virtual_16x36.xlsx --price-config outputs/example/price.json
python -m automl_pipeline build automl_pipeline/examples/virtual_16x36.xlsx --price-config outputs/example/price.json --output outputs/example/build --virtual-data
python -m automl_pipeline serve --output outputs/example/build --activate-draft --no-browser
```

`config`的standard使用该版本默认预算，quick为40轮且V4最多12个划分，V5 deep为300轮基线及最多5000次总成员fit/30分钟预算，旧版本deep仍为5000配置试验。配置文件已存在时不会覆盖。V2/V3选择需要同时向相关命令传入`--price-engine optuna_v2`或`optuna_v3`，使配置schema与引擎匹配。

`build`输出价格原生pkl和报告、效能JSON快照、业务发布JSON、推荐系统数据副本及总manifest。`serve`只启动保存结果；停止后再执行同一命令不会重训。主程序使用自己的当前运行目录，流水线产物位于新的`outputs/`目录。

输入约定、V5目标优先与原V4搜索方式、训练门槛、专家1—5分、预算和评估口径见[`../docs/OPTUNA_V5_PIPELINE.md`](../docs/OPTUNA_V5_PIPELINE.md)。配置改动只有在下一次构建中生效。

## 离线打包与已保存导出

`tools/product_portable.py`、`product_saved_start.py`、`product_export_saved.py`是离线包的操作层；源码运行使用上面的模块命令。`tools/build_product_v5.py`与`upgrade_product_v5.py`负责当前打包和补丁安装，V4和双版本构建工具仍保留。

打包及导出必须同时包含`app/`、`automl_pipeline/`、相关`services/`和`tools/`；兼容入口与实际实现应一起交付。`tools/pipeline_source_layout.py`列出流水线必需文件，依赖检查也检查`automl_pipeline`导入。

Git不包含离线环境和现场训练模型；基线虚拟演示模型属于回归资产。沿用2026-10-16全离线打包工具时，需另行准备已有基础环境ZIP、本地固定运行环境以及V2/V3/V4/V5的完整虚拟演示结果；脚本中的资产路径是该交付基准的本地约定。仅克隆源码即可执行配置、输入检查与构建，不要求先下载旧交付包。

历史验证和重打包脚本位于`tools/verify_*`及`build_*`。涉及旧ZIP的集成验收属于有资产的交付测试；模型搜索测试和源码入口检查可在独立依赖环境中执行。
