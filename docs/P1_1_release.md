# P1-1 · HTTP 深度价格通路

本版实现 P1-0 的接口/环境预检和 P1-1 最小通路。P1 整体尚未完成；首页迁移、校正、导出、独立导出会话和导航启用留在 P1-2/P1-3。未训练模型、未打包 EXE、未修改 V3 算法、推荐排序、SQLite 表结构或泵算法。

## 已实现接口

Portal 同源 `/api/deep-price/v1` 下：GET `/schema`、GET `/products`、GET `/products/{product_id}`、POST `/predict`、POST `/predict/batch`。其余拟议接口当前返回404。现有 `/price` 简易工作台仍保留；新 `/price/deep` 页面尚未迁移。

冻结契约：[deep_price.p1.openapi.json](contracts/deep_price.p1.openapi.json)。[请求/响应示例](contracts/deep_price.p1.examples.json)仅为虚拟说明值；实际请求须先获取 `/schema` 的版本身份。

1. SQLite以mode=ro和query_only打开。要求恰好一个active release，且其product_code与当前启用业务产品相符。产品记录取该产品的enabled historical/imported协议，product_id=agreement_id；不读取旧CSV或训练Excel。
2. 同一读事务读取发布信息、业务定义和产品。使用既有Store的canonical_business_parameters/runtime_parameters编码恰好一次，按价格schema筛选模型字段；不调用初始化、同步registry或发布写方法。
3. 只调用已配置HTTP价格网关。价格服务不可用、缺少加载实例身份或未配置网关时503，不使用本地模型兜底。只调用价格服务，不要求效能服务在线。
4. 请求携带product_code/release_id/model_version/price_model_sha256/model_instance_id/business_snapshot_sha256/price_output_signature。业务数据指纹包括当前发布、定义和历史记录；归一化签名绑定现有价格单位/尺度配置。客户端不可自行声明其他模型版本。
5. 模型SHA取自实际反序列化的同一份字节；启动时分配实例ID。请求向价格服务传expected_model_sha256/expected_model_instance_id，响应身份与完成后health再次核对。磁盘文件替换不会冒充已加载的新模型。
6. 单/批报价复用现有PriceOutputNormalizer一次；字段仍叫predicted_price_wan，结果统一万元。不得将recommendation_price_wan的风险报价当原始预测。
7. 批量最多100条、item_id唯一且保序。任一输入或结果不合法整批失败。所有上游调用共享30秒服务预算；每次调用传剩余超时并在返回时检查，属于同步socket超时机制，不承诺可抢占远端正在执行的计算。HTTP请求体另有5秒读取限额和1MB上限。
8. 业务读锁在上游预测前释放；保留同一连接的data_version复查，任意其他连接提交（即使是registry写入或切换后又恢复）都保守返回409。读取schema/产品时也复查数据及模型实例。
9. 沿用Portal认证；未登录401，跨站/异源403，仅接受JSON。统一错误信封；上游输入400、版本409、不可用503、超时504可区分。检查不是新的全站认证改造。

## 启动与核验

先按原仓库管理流程准备并激活业务发布版本；用本版价格服务加载已选模型，Portal须配置 `execution_mode=services`、正确price_service_url、local_fallback=false。若旧价格服务没有model_instance_id，深度接口会503，需同时升级服务。未激活发布版本也返回明确不可用，不隐式创建版本。

可使用现有run_app.py启动Portal；获取schema后提交预测。首次开发核验推荐直接运行隔离测试，无须操作生产数据库：

```powershell
python tools/run_p1_checks.py --python <独立Python3.8解释器路径>
```

测试仅使用outputs/下临时SQLite、可信的自动生成虚拟测试模型和仓库已有虚拟JSON模型。使用真实loopback HTTP，包含完整Application初始化、业务发布、现有工作台对照、单位尺度、类别映射一次、缺失值、版本冲突、权限、批量、只读和无源Excel案例。logs/记录命令输出和源码树哈希。

## 基线缺陷与范围

发现P0的DataMaster.parse中缺少param_by_key定义，导致首次安装启动失败；已在原始fbe13fe隔离副本复现。本版仅补该字典定义，未改校验规则。完整旧流水线在修复后通过初始化等13项检查，随后旧生成方案筛选断言失败；该生成行为未在本版改动。V21旧测试另有open_new_window配置键、旧BAT启动文本断言与源码不一致。上述不能标作全量回归通过，见验证记录。

Python3.8检查宿主是Windows 10/11，不是Win7验收。正式products.xlsx、V3 bundle及训练环境清单仍未选定，本版虚拟数据结果不证明正式模型兼容性或精度。

本地原README已有用户未完成编辑，本版未覆盖也未纳入提交。原P0审计记录保持历史含义；当前阶段用新验证记录与P1冻结契约。
