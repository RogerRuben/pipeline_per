# Win7 完整离线交付

本交付入口针对本次需求：把项目代码、模型、数据、Python 依赖和可运行脚本整理成一个可复制的离线包。项目历史文档仍然保留其原有背景和操作建议；它们不是对本次用户请求的替代指令，涉及模型安全、正式模型不能静默降级和 Win7/Python 3.8 的约束继续有效。

## 目标环境

- Windows 7 x64；
- 64 位 CPython 3.8.x；
- 断网也可安装 Python 库，因为包中携带 production/runtime wheelhouse 和价格训练 wheelhouse；
- 系统应具备可运行的本地浏览器。项目服务本身不需要公网、Cloudflare 或反向代理。

当前项目已经验证的正式运行库为 NumPy 1.24.4、SciPy 1.10.1、openpyxl 3.1.3、scikit-learn 1.3.2、XGBoost 1.7.6、joblib 1.4.2 和 threadpoolctl 3.5.0。

> “直接运行”在这里表示：目标机预装 64 位 Python 3.8 后，解压交付 ZIP，运行一次离线安装脚本，之后双击启动脚本即可。Python 解释器本身不是可安全复制的 venv；脚本会用目标机 Python 从随包 wheel 重建环境。

## 构建完整交付包

在当前项目根目录执行：

```bat
BUILD_COMPLETE_WIN7_OFFLINE.bat
```

输出位于 `deliverables\full_win7`，包括 ZIP、SHA-256 文件和构建清单。包内包含用户提供的 `automl_sklearn_021_024_bundle.zip` 原始文件及其校验入口。

如果需要 AutoML 0.24 的独立离线环境，先在联网的可信电脑执行：

```bat
DOWNLOAD_AUTOML_024_WHEELS_PY38_WIN64.bat
```

然后重新构建完整包。若不构建该 wheelhouse，主项目运行环境仍可离线安装，但 AutoML 0.24 不能在断网机创建。

## Win7 目标机安装和启动

1. 将 ZIP 解压到短路径，例如 `D:\IndustrialProtocolDemo`；
2. 设置 `PYTHON38_EXE`，或把 64 位 Python 3.8 加入 PATH；
3. 双击 `INSTALL_COMPLETE_WIN7_ENVIRONMENT.bat`；
4. 双击 `VERIFY_COMPLETE_WIN7.bat`；
5. 双击 `START_ALL_SERVICES_WIN7.bat`。

推荐系统地址：`http://127.0.0.1:17891/`。价格服务、效能服务分别使用 18101 和 18102 端口。服务日志位于 `logs`。

## AutoML

`RUN_AUTOML_024_WIN7.bat` 使用 `runtime\venvs\automl02438` 和 `tools\automl\sklearn_0_24\automl_search.py`。它与正式模型运行环境隔离，固定使用 sklearn 0.24.2 及其对应的 NumPy/SciPy 版本，不会污染 `model_runtime38`。

sklearn 0.21 源码和原始 requirements 仍随用户 bundle 保存。其 README 已明确说明 Python 3.8 并非官方支持组合，因此本交付不把 0.21 作为 Win7 默认可运行环境。

## 验收边界

- `tools\verify_model_environment.py --profile runtime --smoke-current-models` 验证正式模型运行时和当前模型冒烟；
- `VERIFY_COMPLETE_WIN7.bat` 还会校验 wheelhouse Manifest、用户 AutoML ZIP 和已安装的训练/AutoML 环境；
- 真实 Win7 目标机仍需确认系统补丁、Microsoft Visual C++ 运行库和浏览器。它们无法由 Python wheelhouse 替代；
- 项目当前模型和 DataMaster 是虚拟验收数据，不能直接作为真实报价、投标或生产工程结论。
