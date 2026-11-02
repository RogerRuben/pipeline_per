# R5 · 完整 EXE 与交付清理验收 · 2026-11-02

本轮纠正上一版只冻结启动器、仍分发 payload Python 源码的交付方式。启动管理、价格服务、试用效能服务、网页后端和网页资源均封装进 product.exe；服务进程通过同一个 EXE 的内部角色入口启动。最终运行包不包含 payload、project、独立 python.exe/pythonw.exe、Python 源文件、Notebook 或 BAT/PowerShell 启动脚本。

开发包保留训练和构建源码，附带预编译完整软件引擎。甲方用本地 products.xlsx 训练后，将模型和配置装入该引擎，生成另一份独立运行 ZIP；现场不需要安装 Python、Git、PyInstaller 或 C 编译器。

## 两种下载，分别使用

| 用途 | 文件 | 大小 | SHA256 |
| --- | --- | --- | --- |
| 直接运行软件 | PipelinePer_Runtime_R5_20261102.zip | 69,663,296 bytes / 66.4 MiB | a261cf321165aa43e01c9e2aaec07c676d2313363f0501b6e6cde03f97e507fe |
| 甲方本机训练和封包 | ClientBuilder_V3_Win7x64_R5_20261102.zip | 141,609,337 bytes / 135.0 MiB | 6612be3a9f614c062e408f86696d11db40245970fd145661c9ae5c092cd0723b |

[下载页](https://github.com/RogerRuben/pipeline_per/releases/tag/r5-frozen-software-20261102)

运行包完整解压，双击 product.exe。当前候选包网页默认 http://127.0.0.1:17892/portal ，管理页 http://127.0.0.1:17890/ 。首次端口来自 defaults/desktop.json；之后使用独立用户数据目录的 config/desktop.json。端口冲突明确报错，不关闭其他软件，也不静默换端口。

默认数据仍按产品编码隔离；兼容的旧用户数据继续保留，字段、价格口径等不兼容时拒绝覆盖。运行包使用用户此前已训练的虚拟模型，模型 SHA256 为 6580c65256e460c0be6fec165911386dd9696196db14bd3e34e958066a1aad3f。本轮未重新训练该模型，也未改 AutoML V3 算法或专家算法。

## 实际验收结果

| 检查 | 结果与范围 |
| --- | --- |
| 桌面、开发配置及封装边界回归 | 25 项通过 |
| Python 3.8.10 兼容回归 | 18 项通过 |
| Portal / Workbench 源模式回归 | 通过 |
| 真实完整 EXE 单服务控制 | 14/14 通过，启动、停止、重启及外部端口 |
| 真实 EXE 运行与恢复演练 | 18/18 通过，预测、Top10、保存、备份、新目录恢复、崩溃清理 |
| 最终开发 ZIP 离线全流程 | 11/11 通过：解压、空 PATH、40 次真实 V3 训练、HTTP 校验、封包、实际启动、重打包及拒绝篡改 |
| 默认入口 | 不传 --data-dir 启动，重复启动复用实例；再做三次冷启动均通过 |
| 原用户 SQLite | SHA 前后相同 |
| 预测一致性 | 推荐、简易预测、深度预测均为 16.4538 万元 |
| 稳定性追加 | 连续十次推荐请求通过 |
| 内嵌网页 | Portal HTML/CSS/JS、深度分析首页通过 HTTP 获取，与源资产哈希一致 |
| EXE 档案 | 1373 个冻结模块，关键业务后端、监督器、三个服务及资源模块均在 EXE 内 |
| 两个运行 ZIP 边界扫描 | 当前模型包与现场新训练重打包产物均无源码文件、脚本或开发目录，全部 909 个封存文件 SHA 一致 |
| Top10 | 本机 5.172 秒，60 字段 |
| 实际进程内存 | 4 个 product.exe，预测后的工作集总和约 217 MiB，私有内存约 289 MiB；不含浏览器 |

[服务控制](audit/frozen_release_20261102/desktop_controls.json) · [运行/恢复](audit/frozen_release_20261102/runtime_exercise.json) · [开发包全流程](audit/frozen_release_20261102/kit_acceptance.json) · [运行包边界核验](audit/frozen_release_20261102/runtime_boundary_audit.json) · [EXE 模块档案](audit/frozen_release_20261102/frozen_archive_proof.json) · [默认入口与内存](audit/frozen_release_20261102/default_startup_acceptance.json)

实际测试宿主为 Windows 11 x64。内存是本次样例模型与已完成预测后的观测值，不能代替所有模型和高并发负载的峰值。一次早期默认目录探测请求出现过 400，当时未保留正文，原因未确证；后续带错误正文捕获的三次冷启动及十次连续预测未再复现，保留为稳定性观察项。浏览器自动化连接超时，本轮未新增人工式浏览器视觉验收；页面资源已做实际 HTTP 字节核验。

完整开发 ZIP 实测中发现并修复了两项封装问题：V3 引擎指纹误包含未交付的旧引擎/专家演示目录，以及全新输出目录缺少父目录创建。此前两个候选包未交付，已回收；表中 11/11 结果绑定最后的 6612be3a... ZIP。

## 清理结果

用户所见约 7 GB 对应本仓库约 7.34 GiB 的文件目录占用，主要是重复交付解压目录和自动验收副本。清理并完成新构建后，仓库约 0.668 GiB（约 684 MiB），已包含最新运行目录、两个最终 ZIP、可复用完整 EXE 引擎和保留模型。

过期预览版、R2/R3/R4 本仓库副本、旧 BAT/源码运行包、重复测试环境、Zig 旧入口工具链、失败冻结构建和重复缓存均采用 Windows 回收站方式清理。另回收本轮自建临时验收目录约 1.80 GiB。所有目标均核对绝对路径并跳过活动进程引用。保留 Git、有效训练模型、现有用户数据库，以及用户 Downloads 内的训练成果。

这是项目目录占用下降；文件仍可从回收站恢复，不宣称磁盘空间已永久释放。批量永久删除曾被自动审批拒绝，后续改为可恢复清理，没有绕过该限制。

[最终清理统计](audit/frozen_release_20261102/cleanup_summary.json) · [首轮回收清单](audit/frozen_release_20261102/recycled.json) · [旧构建缓存](audit/frozen_release_20261102/recycled_build_cache.json) · [最终回收清单](audit/frozen_release_20261102/recycled_final.json)

## 交付边界与目标系统

这是 PyInstaller onedir 完整软件冻结包，Python 业务代码以编译字节码封存在 EXE 中，不再附带可读 Python 源文件；这不是不可逆加密。网页资源也内嵌，浏览器仍须按 HTTP 获取页面和脚本。[PyInstaller 官方封装说明](https://pyinstaller.org/en/v5.13.2/operating-mode.html)。

目标 Win7 SP1 x64 仍无实机环境。PyInstaller 5.13.2 官方 Windows 范围为 Windows 8 及以上，不能将 Windows 11 的结果当作 Win7 验收通过；当前仍是现场候选版。[官方系统要求](https://pyinstaller.org/en/v5.13.2/requirements.html)。

运行包源码身份为 6553461，开发包在仅修复封装器/范围校验后绑定 0edd638；实际完整引擎 product.exe SHA256 为 8201e5079391c4a6c85ba7769b195376a8728370eb246513ecd032ebd43c8e52，两个交付链路使用同一完整引擎。代码继续更新现有 Draft PR #5，本轮未合并 main。

