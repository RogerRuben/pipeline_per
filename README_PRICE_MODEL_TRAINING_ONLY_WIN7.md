# 价格模型训练专用 Win7 离线包

本包只用于价格模型训练、原生模型导出和 AutoML 候选搜索，不包含价格在线服务、效果模型、推荐页面或主应用。

## 目标机要求

- Windows 7 SP1，64 位
- 64 位 CPython 3.8
- 包内已提供价格训练和 AutoML 的离线 Python wheelhouse；不需要联网安装 Python 库

## 标准训练流程

1. 解压到不含中文和空格的目录。
2. 如果 Python 不在 PATH，先设置 `set PYTHON38_EXE=C:\Python38\python.exe`。
3. 运行 `CREATE_PRICE_MODEL_TRAINING_ENV_WIN7.bat`。
4. 运行 `TRAIN_PRICE_MODEL_WIN7.bat`，输入历史成品 CSV/XLSX、成品代号和价格列。
5. 输出位于 `services\price_service\model\price_native_bundle.pkl`，并生成训练报告。

## Notebook 和 AutoML

- `RUN_PRICE_TRAINING_NOTEBOOK_PY38.bat`：启动价格训练 Notebook。
- `CREATE_PRICE_AUTOML_TRAINING_ENV_WIN7.bat`：创建 AutoML 0.24 独立训练环境。
- `RUN_AUTOML_024_WIN7.bat`：运行 AutoML 0.24 候选搜索。

## 验证

运行 `VERIFY_PRICE_MODEL_TRAINING_WIN7.bat`，它只检查价格训练库、训练模块和离线 wheelhouse，不加载或启动在线服务。
