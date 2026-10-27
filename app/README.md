# 推荐系统源码

当前为真实V21.6开发源码（3439c7d），带流水线输入、原生价格解释及旧实验类兼容修复。包含优化目标、条件侧栏拖拽、需求版本、场景策略及排序解释。V6的START打开统一门户，同时启动独立效费分析台；旧保存模型也使用当前前台。来源及局部兼容修改见[`../docs/RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json`](../docs/RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json)。

`app/`负责已有成品的业务运行：中文前台、HTTP接口、数据管理、历史协议推荐、方案生成、显式价格/效能重算、草稿校验及发布。启动入口是仓库根目录的`run_app.py`。

| 文件 / 目录 | 职责 |
| --- | --- |
| `server.py`、`static/` | HTTP路由与前台，包括数据中心、推荐页、价格/效能工作台 |
| `store.py` | SQLite持久化、初始化、业务数据生命周期、备份和恢复 |
| `recommender.py` | 历史方案筛选、排序与推荐 |
| `local_generator.py`、`gflownet_generator.py` | 候选方案生成；GFlowNet为保留的实验路线 |
| `model_service_client.py` | HTTP价格/效能模型调用、schema目录及降级行为 |
| `product_releases.py` | 成品草稿、校验、离线业务发布及切换 |
| `historical_onboarding.py`、`product_table.py`、`wide_import.py` | 成品历史表与业务主数据的导入解析 |
| `xlsx_utils.py` | 标准库实现的工作簿读写辅助 |

这里不执行价格AutoML搜索。快速训练和成品构建位于[`../automl_pipeline/`](../automl_pipeline/README.md)；两个模型服务位于`../services/`。共享表格解析和业务草稿接口被流水线复用，因此不另复制一套推荐系统。

```bat
python run_app.py --no-browser
```

默认推荐端口17891，数据中心路径`/admin`；模型服务URL配置见`config/model_services.json`。三个服务一起启动使用`START_ALL_SERVICES_WIN7.bat`，模型环境按各自产物配置。

运行数据库、日志、上传与导出属于本机状态。Git不提交现场数据库；全新目录可由`data/virtual_protocol_dataset.csv`初始化虚拟业务数据。生产数据通过成品发布流程导入。

完整说明及测试入口见[仓库README](../README.md)、[数据治理](../docs/DATA_GOVERNANCE_V19_6.md)和[成品交付](../docs/PRODUCT_DELIVERY_WIN7.md)。
