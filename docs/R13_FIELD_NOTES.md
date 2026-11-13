# R13 现场兼容说明

这不是 Windows 7 客户交付验收。下面只记录这次代码改了什么，以及还没验证的部分。

## 效能 ZIP

导入仍检查路径、清单哈希和模型格式，不执行压缩包里的 Python。源码和内置运行时不完全一致时，不再直接拒绝。软件继续使用内置专家运行时，并标记“已加载，原算法一致性未验证”。

本次工作区没有甲方原始泵 V13 压缩包，所以没有做同一输入的六组评分对照。

## 价格模型

标准包格式不对，或缺少 Python 模块时，报错会写出实际格式或缺失模块名。本次没有拿真实 V4–V7 模型文件做导入对照，不能称为全版本支持。

## 开发包

训练源码会带上已有的 optuna_v2、v4、v5、v6、v7 目录。完整封装入口默认仍是 optuna_v3。其他版本用 `TRAIN_PRICE_ONLY.bat` 单独训练，再用 `PACKAGE_EXISTING_MODEL.bat` 打包。

页面文字可放到 `PIPELINE_STATIC_DIR` 指向的目录，重启 Web 后生效，不必重编译。Python 逻辑变更必须用 `REBUILD_ENGINE.bat` 重新冻结，不能只改已封进 EXE 的旧文件。

## 评价指标

V3 优化目标仍是 R² 和 MAPE。MAE、RMSE 只记录。价格没有变化时，R² 记为不可定义，不再写成 0。旧 V3 搜索过程没有在这次改成四目标。

## 本机环境

- Python 3.8.10 x64（`D:\CB7\CB7\runtime\python38\python.exe`）
- 构建目录 `D:\CB13_build_tools`：PyInstaller 5.13.2、pyinstaller-hooks-contrib 2023.8
- 运行库补了 openpyxl 3.1.2、SciPy 1.7.3（专家运行时需要 `scipy.stats.qmc`）
- 轮子缓存在 `D:\CB13_wheels`，安装时不访问外网也可以从这里装

## 本机已执行

- 用模拟数据分别训练 optuna_v2、v3、v4、v5、v6、v7，都写出 `price-native-bundle-1.0`，并重新加载后做了一次预测。这不是甲方历史文件的一致性验收。
- 从解压后的 `outputs/CB13` 执行 `TRAIN_PRICE_ONLY.bat`，得到模型文件。
- `PACKAGE_EXISTING_MODEL.bat` 封装的是指定的 v5 目录，不是上一次训练目录。
- `REBUILD_ENGINE.bat` 在该开发包里生成了新的 `product.exe`（sha256 `55d790bb63d5eea7af4c52c96b3c5a78df2b485f10175dabf875cf094c400124`）。单独的引擎目录没有 `PACKAGE_MANIFEST.json`，所以不能把它当成已经装好的客户包直接启动。
- 设置 `PIPELINE_STATIC_DIR` 后，首页返回的是覆盖文件里的文字，没有重新冻结。
- 用内置专家导出函数生成了模拟泵 ZIP，导出脚本报告 6 组对照通过。这不是甲方原始泵包。

## 未验证

- Windows 7
- 甲方原始效能包与内置源码的逐行差异，以及同一输入的六组评分
- 甲方手里的 V4–V7 历史模型文件。本仓库训练写出的仍是统一 native bundle，不能据此称为旧版本文件已全部导入验证
- 无网络整包重建 EXE
