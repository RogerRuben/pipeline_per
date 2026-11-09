# R8 · 统一门户与可选三层定价验收

日期：2026-11-09。分支：codex/r8-unified-portal-ds。已整合远端 codex/r7-startup-and-pages 的 8e3a70d 与本机 R7；本轮只有本地源码提交，没有推送新分支、合并 main 或上传交付文件。用户原有 README 修改与本地交付偏好均保留。

## 实现范围

- 采用 front-gate 的一套 Web 静态界面作为通用外壳，复用现有 Web worker。四个业务入口同源挂载：价格 /price/deep、效能 /effectiveness、效费 /cost-effectiveness/、推荐 /recommendation。未引入另一套 Python、Nginx 或门户服务进程。
- 产品名称在构建时优先读取 Excel“产品信息”表中的“产品名称”行，再写入模型/发布资料及 Portal。逐行样本名称与表级产品名称分开；旧 JSON 名称作为兼容回退，Excel 已提供名称时可省略 JSON 的 product_name。产品编码仍由 settings/project.json 配置。
- 门户顶部“服务管理”嵌入原管理界面；保留独立管理窗口。管理端 CSP 仅允许自身及本产品实际 Web 源嵌入，不使用通配符；令牌、Host、Origin 与 Sec-Fetch-Site 操作校验均保留。网页服务停机后，独立管理进程和已经打开的管理界面仍可恢复服务。
- 默认 price_backend=optuna_v3，保留现有 V3 训练及工作簿专家评分。可改为 ds_three_layer，从 settings/ds.json 读取已训练模型、字段映射与可选枚举别名。导入资产先快照再校验/封装，不调用 AutoML 训练。
- DS 模型加载与计算复用附件 ds_model.py 原算法，源码 SHA 和来源记录在 services/price_service/DS_PROVENANCE.json。适配为现有健康、Schema、单次、批量与模型实例身份协议；所有金额由元统一转换为万元一次，保留 SDK 原始三层金额、状态和警告。错误不转成 0 报价，第三层已包含 B，不重复追加。
- SDK 的必填属性必须完整映射到稳定英文业务 ID；布尔 false、数值 0 作为有效值。按单位计价字段的业务合法域不生成负数量。深度分析展示三层金额，DS 未提供的 AutoML 精度、成员权重和重要度保持不可用。
- 开发包含 V3 带评分示例、空白模板和配套 DS 虚拟模型/产品表。新增 BUILD_AND_PACKAGE.bat 通用入口，保留原 TRAIN_AND_PACKAGE.bat；SELF_TEST.bat、SELF_TEST_DS.bat 分别验收两种虚拟流程。REPACK_LAST.bat 复用已验证发布资产。

## 回归与实际文件验收

已通过：远端桌面兼容及鉴权 36 项、原完整流水线 45 项、价格服务契约 13 项、V3 算法 18 项、R7 历史批次元数据 5 项、工作簿评分 5 项、同源页面 2 项、DS 导入与布尔业务值 3 项、冻结文件范围 3 项、开发包路径与端口配置 8 项。

最终 CB8.7z 经过实际解压，完成全部文件清单校验、空 PATH 环境检查、移除 JSON 名称后的 Excel 产品名检查、V3 40 次试验及接口验证、实际 EXE 启动、V3 不重训封装、DS 输入检查及导入、不训练断言、模型文件不变断言、DS 三入口价格一致为 4.5 万元，以及 DS 不改变模型的重封装。配置在测试结束后恢复。

从同一最终开发包生成的两个运行包，分别通过真实 Chrome 的门户测试：四个业务页面嵌入、实际价格预测、Excel 产品名称、R7 需求变化后保留结果、管理页嵌入、价格服务单独停/启/重启、其他进程保持运行、网页停机后管理页仍可用并恢复网页，以及正确后端名称；没有 JavaScript 错误。AutoML 浏览器检查 14 项，DS 15 项（增加三层总价/明细验证）。四页检查证明页面可达，本轮未将附件所有用户、角色或其他模板页面视为已实现的统一业务后端。

本机推荐操作记录：AutoML 虚拟 160 条数据为 0.553、0.493、0.497 秒；DS 虚拟 8 条为 0.205、0.257、0.154 秒。样本规模不同，不据此比较两算法速度或承诺客户数据性能。

实际 EXE 测试修复了 JSON 布尔值被当成数值文本拒绝、管理页禁止嵌入的问题。最终开发包检查修复了效费测试文件被误计入生产指纹的问题：逐项证明所有生产文件与 EXE 未变化，仅按发布规则排除不携带的测试目录；生产文件校验保留。诊断和前后证据保存在本地。

证据位置：outputs/r8_kit_acceptance/acceptance.json、delivery_summary.json；outputs/r8_browser_v3final、outputs/r8_browser_dsfinal 内含浏览器报告与截图；outputs/r8_fingerprint_fix.json 记录指纹范围修正。验证完成后清理的测试目录不会作为可继续运行的交付路径。

## 本地交付

| 文件 | 字节数 | SHA256 |
| --- | ---: | --- |
| CB8.7z | 52707635 | 7fa91d1da2ab69c7ac83470d45aefdee10dbcf8ae82f98fb16757e8e0f73e331 |
| PipelinePer_R8_AutoML_20261109.zip | 71273288 | e46dd31ffd614d2e5f377da96bb3a5f41628bbe7e26d99c1ecdb631588e59f66 |
| PipelinePer_R8_DS_20261109.zip | 71198941 | 95f17ceb660a27ba4b5a3ae4dc77f9068b33632e7ac7a87e48695cd60115ffd1 |

三份压缩文件都小于 100,000,000 字节。运行包检查没有可读 Python 业务源码或 BAT 启动入口；开发包含构建源码与完整依赖，供甲方现场使用。

## 使用

开发包用 7-Zip/WinRAR 完整解压到短目录，例如 C:\CB8。填写 input/products.xlsx 的产品数据、专家评分和产品信息。产品编码及后端在 settings/project.json；DS 模型文件及映射在 settings/ds.json。先 CHECK_INPUT，再 BUILD_AND_PACKAGE。真实 DS 模型由对方提供，本轮 DS 仅以附件虚拟模型验收，没有训练客户三层模型。

演练运行包解压后双击 product.exe。AutoML 初始 Portal 为 http://127.0.0.1:17921/portal，管理页 17920；DS 初始 Portal 为 http://127.0.0.1:17931/portal，管理页 17930。首次端口配置在 defaults/desktop.json，初始化后读取独立用户目录的 config/desktop.json；地址可在启动器和管理页查看。两个演练产品分别使用独立编码 R8_AUTOML_DEMO、R8_DS_DEMO，旧用户数据没有覆盖。

已完成 Windows 11 x64 的实际 EXE 与浏览器测试。Windows 7 SP1 x64 仍待目标机验证，正式 DS 模型仍需拿到后做字段及对应样例验收；本轮不声明正式业务精度或 Win7 实机通过。
