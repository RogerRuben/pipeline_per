# 价格模型专用 Win7 离线包

这是本项目的价格模型独立交付包，不包含效果模型、推荐页面、主应用或其他服务。

## 目标机要求

- Windows 7 SP1，64 位
- 64 位 CPython 3.8；建议使用 Python 3.8.10/3.8.x
- 目标机无需联网，包内已提供价格推理和训练所需 wheelhouse

## 运行价格服务

1. 将 ZIP 解压到不含中文和空格的目录。
2. 如目标机的 Python 不在 PATH，先设置：`set PYTHON38_EXE=C:\Python38\python.exe`
3. 运行 `CREATE_PRICE_RUNTIME_ENV_WIN7.bat`。
4. 运行 `START_PRICE_MODEL_WIN7.bat`。
5. 浏览器打开 `http://127.0.0.1:18101/docs`，健康检查为 `/health`。

服务只加载 `services\price_service\model\price_native_bundle.pkl`，不会静默切换到其他模型。

## 重新训练价格模型

运行 `CREATE_PRICE_TRAINING_ENV_PY38.bat`，然后运行 `TRAIN_PRICE_SERVICE_MODEL_WIN7.bat`。输入历史成品 CSV/XLSX 和成品代号即可导出新的原生价格模型。

## AutoML 0.24

`CREATE_AUTOML_ENV_PY38_WIN7.bat` 和 `RUN_AUTOML_024_WIN7.bat` 仅用于价格模型候选搜索；包内包含用户提供的 `automl_sklearn_021_024_bundle.zip` 及离线 wheelhouse。

## 验证

运行 `VERIFY_PRICE_ONLY_WIN7.bat`。它会检查 Python 位数/版本、价格依赖版本、wheelhouse 清单、原生模型完整加载和示例预测。
