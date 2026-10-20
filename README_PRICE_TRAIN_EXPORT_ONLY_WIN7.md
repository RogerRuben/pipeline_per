# 价格模型训练与导出最小包

本包只做两件事：训练价格模型、导出 `price_native_bundle.pkl`。不包含在线服务、AutoML、Notebook、效果模型、推荐页面或主应用。

## 目标机要求

- Windows 7 SP1，64 位
- 仅需要 64 位 Windows 7 SP1；包内自带 Python 3.8.10 x64
- 目标机无需安装 Python、pip 或联网；解释器、标准库和训练库都已内置在 `runtime\python38`。

## 一键虚拟数据验收

直接运行：

```bat
PRICE_TRAIN_AND_EXPORT_WIN7.bat
```

不带参数时使用包内 `sample_virtual_price_history.csv`，输出：

- `output\price_native_bundle.pkl`
- `output\price_native_bundle.pkl.training.json`

## 使用真实历史数据

```bat
PRICE_TRAIN_AND_EXPORT_WIN7.bat "D:\data\history.csv" PRODUCT_A "产品A" price_wan
```

参数依次为：历史 CSV/XLSX 路径、成品代号、成品名称、价格列名。价格单位按万元处理；如果原始价格不是万元，请先换算或调整训练脚本参数。

训练会自动训练 Ridge、Random Forest、Extra Trees、GBDT、SVR，并按验证集误差生成集成权重，最终导出原生价格模型包。
