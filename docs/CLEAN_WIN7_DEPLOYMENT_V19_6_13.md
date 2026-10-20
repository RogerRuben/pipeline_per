# V19.6.13 Clean 版：Win7 安装、价格模型与最终测试

## 1. 目标环境

- Windows 7 x64；Python 3.8 x64。
- 甲方断网机必须事先准备与 `services/*/requirements*.txt` 匹配的离线 wheels；clean 源码包本身不携带 wheels。
- 本机开发验证可使用 `runtime\venvs\model_runtime38\Scripts\python.exe`。

## 2. 安装和启动

1. 解压 clean 包到短英文路径，例如 `D:\recommend_v19613`。
2. 有网络或内网镜像时运行 `INSTALL_SOURCE_DEPENDENCIES_WIN7.bat`；断网机改用另行交付的 wheelhouse 安装。
3. 运行 `VERIFY_MODEL_ENVIRONMENTS.bat`。
4. 运行 `START_ALL_SERVICES_WIN7.bat`。
5. 打开以下页面：
   - 推荐系统：`http://127.0.0.1:17891/`
   - 独立价格预测：`http://127.0.0.1:17891/price`
   - 独立效能评价：`http://127.0.0.1:17891/effectiveness`
   - 数据管理：`http://127.0.0.1:17891/admin`

主系统只把完整参数 JSON 发给 18101 和 18102。Schema、成品代号、单位或字段说明不同会显示运维告警，但不再作为推荐前置门禁；真正的可用性标准是两个实算接口返回可解析的价格、效能和可行概率字段。

## 3. 用原 Notebook 训练并部署价格模型

1. 打开根目录 `规范版价格预测_V19_6原生服务导出补丁.ipynb`。
2. 使用英文属性编号作为训练表头，例如 `attr_001`、`attr_002`。
3. 按原 Notebook 完成清洗、训练、精度比较和专家选模。无需让所有算法都成功。
4. 最后一个单元格只必须修改：

   ```python
   PRODUCT_CODE = "你的成品代号"
   ```

5. 原始价格为元时设置 `TARGET_DIVISOR_TO_WAN = 10000`；已经是万元时设置为 `1`。
6. 运行最后单元格。它会发现当前内存里实际存在的模型，并直接写入 `services\price_service\model\price_native_bundle.pkl`。
7. 重启 `START_PRICE_SERVICE_WIN7.bat`，在 `/price` 页面输入参数验证。

导出包只要求包含成品代号、预测字段顺序、字段解析/缺失策略、训练预处理器（如果有）和至少一个可预测模型。字段标签、单位和枚举映射可选；英文表头直接作为 HTTP 字段编号。

## 4. 数据中心维护指标约束

进入“数据管理 → 约束规则”，通过下拉框选择左侧指标、比较关系和右侧指标。页面实时展示：

`左侧指标 比较关系 系数 × 右侧指标 + 偏置`

右侧指标可选择“无（使用常数）”。标签规则的标签、指标和模型输出字段也改为下拉选择；新增规则、耦合、协议等编号由系统在保存时自动生成。

## 5. 最终测试资产

`outputs\final_acceptance_fixture_20260814` 包含：

- `encoded_aircraft_door_lock_prediction.xlsx`：英文属性编号、模型值已编码，可直接构造预测 JSON；空白值用于验证价格服务缺失策略。
- `expert_state_v10.json`：V10 固定协议专家状态。
- `expert_state_v11.json`：由当前正式迁移/重训练路径从 V10 生成的 V11 状态，不是手工改版本号。
- `source_business_history_reference.xlsx`：原业务显示值参考表。

测试表和 expert state 均为虚拟功能数据，不得用于工程、适航、报价或采购结论。

## 6. 本机回归

开发源码仓库使用完整回归：

```bat
python tests\price_dynamic_model_hotfix_test.py
python tests\v19_6_service_governance_test.py
python tests\service_outage_historical_fallback_test.py
python tests\relaxed_http_contract_test.py
runtime\venvs\model_runtime38\Scripts\python.exe tests\basic_aircraft_door_lock_models_test.py
```

最后一条需要兼容模型依赖的 Python 3.8 虚拟环境。普通系统 Python 若 NumPy/scikit-learn 版本不同，不应拿来反序列化既有价格 pickle。

Clean 交付包只保留面向操作人员的三个烟雾测试：`relaxed_http_contract_test.py`、`service_outage_historical_fallback_test.py` 和 `product_release_download_http_test.py`；历史迁移、实验算法和开发基准不会进入交付包。
