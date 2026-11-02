# R6 价格深度分析复原与软件交付验收 · 2026-11-02

按用户确认的 R6-A～E 完成：以原四模板为基准，恢复独立导航与敏感性/情景子选项卡、因素图表、只读模型卡、情景差额/明细/气泡/Top5。保留统一 HTTP 价格服务，未改变 V3 训练和价格算法、专家效能算法，也未实施可加属性混合定价。

[逐控件/图表/接口覆盖矩阵与复演步骤](R6_COVERAGE_MATRIX_20261102.md) · [下载页](https://github.com/RogerRuben/pipeline_per/releases/tag/r6-deep-analysis-20261102)

## 最终两个包

| 用途 | 文件 | 大小 | SHA256 |
| --- | --- | --- | --- |
| 最终用户软件，无可读Python业务源码 | PipelinePer_R6_20261102.zip | 69,665,145 bytes，约66.4 MiB | 01e1ab329728a77b2c383453c6d1805a9cd0d59e57f02b4caab0d9b44796321e |
| 甲方训练开发包，正常保留源码 | ClientBuilder_R6_Win7x64_20261102.zip | 141,607,832 bytes，约135.0 MiB | 71c77896963ef401b027b203294dbb8366453f8de43886038d31e55694086add |

构建源提交：18baf0c63fd7b8b04f1b51a0d6ff5b56f5a92a08。完整 EXE SHA256：d01a11fa4833ff0cc9dd93feea1326f8bf0b10cd4aafe963cf413afafcfd657b。

运行 ZIP 完整解压后双击 product.exe。当前预览网页 http://127.0.0.1:17901/portal ，管理页 http://127.0.0.1:17900/ 。首次可提前编辑 defaults/desktop.json；已有安装继续以用户目录 config/desktop.json 为准。仅端口模板列为可配置文件并严格校验格式，模型与其他受保护文件仍校验 SHA。退出旧软件再启动新版本，用户 SQLite 独立保留。

开发包先执行 CHECK_ENVIRONMENT 和 SELF_TEST；替换 input/products.xlsx、配置产品与数据类别，再执行 TRAIN_AND_PACKAGE。REPACK_LAST 复用已验证模型，不重新训练。常规现场流程不需要另装 Python、Git、PyInstaller 或编译器。给最终使用方只发生成的运行 ZIP。

## 功能与真实数据口径

- 当前模型重要度在构建阶段离线计算，不修改或拟合模型。固定种子20261102、3次置乱，优先使用训练报告中的验证样本ID（本例32条），保留 MAE 增量正负值、标准差、样本/特征/模型/业务来源指纹。部署全数据重拟合后明确不是独立测试，不作因果贡献。
- 历史演示保留真实旧 Excel 的25项排序，legacy_demo 与原文件 SHA 明示，不能作为当前60字段模型解释。专家评分另列，未提供则显示缺失。
- 模型库仅展示当前 HTTP 服务可靠发布的一个模型；不把磁盘文件当作已发布历史，不制造模型卡。候选六族与实际部署 Lasso/Ridge 分列，真实权重28.41% / 71.59%。多版本注册与在线原子切换不是本轮新增能力，操作仍走开发包/管理员受控发布流程。
- 训练 MAPE 百分数与验证 MAPE 小数比例分别处理；实际卡片显示训练约0.91%、验证约1.50%。未提供的 MAE/RMSE 显示不可用。搜索划分验证不称独立测试。
- 单参数局部半径为 max(abs(当前值)×20%,1)，再约束至业务范围；历史模式按发布历史 min/max。两种 Top10 由服务器区分“重要度先选属性”和“扰动后按最大价格变化排序”，不混同全局重要度或专家权重。
- 情景从当前属性复制，默认只选要改的字段，可切高级全部字段。原价、新价、差额、变化率、改动清单进入服务器结果；气泡与Top5来自额外单字段HTTP预测，不能相加作混合定价。
- 0保持有效；缺失映射null。当前模型的必填缺失按服务契约拒绝，恢复参考属性可再预测；未声明专用不可用编码时禁用第三分支，不猜测为0。
- 机理与大模型保持未配置禁用状态；价格 TXT 是结构化分析结果，不冒充机理报告。时间/数量校正规则未确认时也不计算。

## 最终实际验收

| 验收 | 结果 |
| --- | --- |
| 固定 Python3.8 深度/桌面/开发配置/封装边界回归 | 84项通过 |
| 最终真实 EXE 单服务与端口 | 14/14通过 |
| 最终真实 EXE 持久化/恢复/崩溃/同模型回滚 | 20/20通过 |
| R6真实EXE专项：绑定重要度、单位、成员、排序/范围、情景、导出 | 10/10通过 |
| 同一最终开发 ZIP：解压、空PATH、40次V3训练、元数据、封装、启动、重打包、拒绝篡改 | 11/11通过 |
| 原四模板参考 | 模板文本核对（仅尾部空白差异）、原件与镜像SHA、静态参考截图 |
| 实际浏览器 | 27项已记录交互检查；最终EXE页面原生视口截图、导出TXT文件 |
| CSV/TXT价格与差额 | 同模型SHA，误差0（小于1e-8） |
| 版本进入EXE | 7个关键HTTP静态资源与源码字节SHA一致 |
| 运行包边界 | 两个实际运行ZIP全部909项出厂文件SHA核验，无.py/Notebook/BAT/源码开发目录 |
| 首启端口模板 | 实际EXE修改端口可验证；非法host键、模型篡改仍拒绝；测试修改全部恢复 |
| 默认目录更新 | 160历史记录、60字段定义、1产品等业务内容摘要保留 |
| Top10性能 | 最终恢复演练约4.281秒，当前60字段虚拟表模型 |

[回归原文](audit/r6_20261102/python38_final_regression.txt) · [最终EXE控制](audit/r6_20261102/controls.json) · [恢复与回滚](audit/r6_20261102/target_report.json) · [R6专项](audit/r6_20261102/r6_api_acceptance.json) · [最终开发ZIP流程](audit/r6_20261102/kit_acceptance.json) · [资源身份](audit/r6_20261102/exe_resource_acceptance.json) · [软件边界](audit/r6_20261102/runtime_boundary_audit.json)

TXT为实际浏览器下载样本；CSV用同一导出端点的独立本地验收会话取得，未把它谎称为浏览器落盘文件。[真实TXT](audit/r6_20261102/browser_scenario_export.txt) · [CSV](audit/r6_20261102/http_scenario_export.csv) · [数值核验](audit/r6_20261102/csv_txt_consistency.json)。

原版为静态模板参考，没有执行旧 Flask/pickle；原因素图用真实旧表的只读JSON响应驱动，其余旧API不可用提示保留。新版图来自实际软件浏览器视口，截图没有人工拼接。[参考来源与SHA](audit/r6_20261102/reference_sources.json)。

## 在实测中修正的交付问题

Windows 默认 HTTP 地址复用允许同端口重复监听，曾使并发验收与预览实例请求混落不同实例。已以实际双监听复现实证确认，改用 SO_EXCLUSIVEADDRUSE，并让自动验收使用专用外部配置端口；最终20项恢复回滚复测通过。[复现实证](audit/r6_20261102/port_isolation_repro.json)。行为依据见 [Microsoft 官方套接字说明](https://learn.microsoft.com/en-us/windows/win32/winsock/using-so-reuseaddr-and-so-exclusiveaddruse)。

首启端口模板此前被一并封存，和提前外部配置要求冲突；现只允许经过格式验证的 desktop.json 变化，其他文件保护不放宽。[实际完整性检查](audit/r6_20261102/prelaunch_config_integrity.json)。

一次早期重新封装的推荐请求返回400但当时正文未记录；之后四次独立冷启动未复现。源码同时确认推荐门槛缺少已存在的恢复调用：启动探测错误缓存后，健康服务恢复仍可能被拒。已补恢复调用、永久不匹配仍拒的回归，并为后续失败保留完整正文。不能将丢失正文的那次错误虚报为确证根因；最终同一开发ZIP的完整训练与重打包已通过。

## 仍保留的门槛

所有实际执行来自 Windows11 x64，当前模型由虚拟数据训练；未作甲方业务精度认证。Win7 SP1 x64 无实机环境，仍是现场候选版。PyInstaller5.13.2官方Windows范围为Win8及以上，[官方要求](https://pyinstaller.org/en/v5.13.2/requirements.html)；不能用本机结果宣称Win7验收通过。冻结字节码封装不等于不可逆加密。

R6在独立 codex/r6-deep-analysis 分支，基于R5开发分支，继续 Draft PR；main不合并。旧预览、失败候选与缓存按可恢复回收站方式整理，保留用户数据和已验证模型，清理统计见 [记录](audit/r6_20261102/cleanup_summary.json)。

