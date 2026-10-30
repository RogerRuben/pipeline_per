# 源码核查与差异

核查 commit：e6bd67b8efb415af6a5faea1f7bc1af977a25021，与任务书记录一致。当前本地旧工程未作为新项目源码来源，独立克隆了 GitHub main。

| 路径 | 核查结果 | 新项目处理 |
|---|---|---|
| automl_pipeline/__main__.py | 支持 optuna_v3，默认 optuna_v7 | 不改算法；build_profile 显式锁 V3，后续执行器必须读取 |
| automl_pipeline/product_price_optuna_v3.py | V3 配置和 train_price_optuna_v3 已有 | 直接复用 |
| app/server.py | 已有 price_workbench_schema/predict，兼容模式仍可本地推理 | P1 复用字段补充逻辑；深度页面要求 HTTP 网关可用，不沿用本地回退 |
| app/model_service_client.py | 已有 price_schema/predict_price 和推荐批量调用 | 增加价格专用批量方法时不可连带调用效能 |
| app/store.py | runtime_parameters 已做 business→model 映射；sync_model_schema 会写 model_registry | 仅复用只读查询/编码，避免重复映射与 registry 写入 |
| app/configuration.py | advanced_price 导航存在、默认 disabled | P1 就绪后开启同源路由 |
| services/price_service/app.py | schema、predict、batch 已有，batch 上限1000 | 复用；Deep API 初始上限100，明示超时和版本校验 |
| price_native_bundle.pkl.manifest.json | virtual_fixture=true，模型 virtual-native-price-20260730 | 不是所选正式 Optuna V3 模型证据 |
| references/deep_price | 20个旧路由；模型上传、激活和直接模型访问 | 仅参考，功能映射见矩阵 |
| references/expert_delivery | 含V13专家/演示源码及冻结调用文档，正式模型目录仅README | P5 正式冻结ZIP尚缺 |

附件原始 SHA256、成员哈希、旧路由行号、删密记录见 audit/attachments.json。旧后端硬编码凭据回退已从入库参考副本移除，其有效性未知；原持有人应撤销/轮换。未执行任何附件 Python，也未反序列化旧 pickle。

来源权限：主干已公开；本次上传的是源码、文档及去密参考资产。附件样本数据、二进制模型、字节码和原压缩包仅保留本地。许可证清单仍待核对，尤其参考 ECharts 的再发行条件。
