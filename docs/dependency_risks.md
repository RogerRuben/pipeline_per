# 依赖风险与验证门槛

以下是仓库锁文件事实，不代表目标机器已验证。

| 环境 | Python | NumPy | SciPy | sklearn | 其他 |
|---|---|---|---|---|---|
| automl_pipeline/requirements.txt 引用的旧训练环境 | 3.8目标 | 1.19.5 | 1.5.4 | 0.24.2 | pandas1.3.5/joblib1.0.1/threadpoolctl2.1.0/optuna3.6.0 |
| price_service requirements_win7_exact.txt | 3.8目标 | 1.24.4 | 1.10.1 | 1.3.2 | joblib1.4.2/threadpoolctl3.5.0/xgboost1.7.6 |
| 本次审计解释器 | 3.12.8（以验证报告为准） | 未验证 | 未验证 | 未验证 | 不是Win7目标环境 |

不可将两套锁文件合并安装，也不可仅因 Python 相同就承诺 pickle 兼容。当前虚拟模型的 manifest 没有证明正式 V3 训练依赖。

P0 未关闭项：选定正式 products.xlsx 和 V3 bundle，记录 SHA、导出类路径、训练环境 pip freeze、解释器位数；在隔离 Python3.8 环境运行受信任模型的加载和正式价格 HTTP 单/批预测；保留同一输入在训练环境的金标准结果并比较。模型不能加载时修复导出/环境，不偷偷换 portable 近似模型。

P4 门槛：固定经验证的 PyInstaller/运行库版本及 wheel 哈希；Win7 SP1 x64 上离线、无系统Python、非管理员、中文路径、端口冲突、二次启动、异常退出、坏模型、升级数据库迁移/备份/回滚逐项实测。未提供目标机，本次不声称 Win7 支持已经验收。

许可证：收集全部运行时、ECharts、打包工具许可；当前附件没有完整许可证清单，正式发行前补齐。正式模型缺失不阻断代码设计，但阻断真实模型兼容性验收。
