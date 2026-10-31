# PipelinePer P4 桌面原型使用说明

本包使用虚拟产品数据训练的真实 Optuna V3 模型，以及试用效能快照。它用于软件交付链路验证，不代表真实业务准确率。Windows 7 尚待实机验收，EXE 尚未代码签名。

P4.1 新增已封存的交付契约与离线备份恢复。换模型时检查模型字段、顺序、类型、单位、枚举编码、缺失策略、业务字段及已激活发布身份、输入绑定和价格单位／尺度。匹配契约的重训模型可复用数据；不匹配则拒绝启动，等待单独迁移。P4 老数据首次升级仅接受构建时明确核验过的同模型、同业务包前版；不会仅凭产品编码推断兼容性。

## 启动与退出

1. 将整个 ZIP 解压到新的本地目录，保留目录结构；不要只复制 product.exe。
2. 双击 `product.exe`。启动器在后台启动价格服务、试用效能服务、网页服务，健康检查完成后打开 Portal。
3. 从 Portal 可进入推荐、简易预测、深度价格分析、试用效能、数据管理和“软件运行管理”。
4. 关闭浏览器后软件继续运行。通过“软件运行管理 → 停止全部服务”退出，或执行 `product.exe --stop`。

无需安装 Python，无需联网。重复双击会复用同一用户数据目录的实例。三个业务服务仅监听 127.0.0.1，并自动分配空闲端口；启动器会监督退出的子进程，60 秒内最多自动重启三次，之后保留管理页显示错误并等待人工处理。

## 用户数据与配置

默认数据目录为 `%LOCALAPPDATA%\PipelinePer`，与软件目录独立。SQLite 位于 `data\protocol_demo.db`，配置位于 `config`，日志位于 `logs`，导出和备份分别在 `exports`、`backups`。首次启动才导入包内业务发布包；已有数据库不会被重新导入或激活默认产品。模型文件保留在软件包 `release`，不会在启动时训练。

可用 `product.exe --data-dir "D:\PipelinePerData"` 或环境变量 `PIPELINE_PER_DATA_DIR` 指定持久目录。之后启动、状态、停止等操作均须使用同一路径。数据目录不能放在软件包内部，也不能是软件目录的上级目录。

深度分析校正规则读取用户目录 `config\deep_price_adjustment.json`。默认未配置，正式规则仍须带审批来源与产品、模型、价格基准身份。已有规则和价格单位配置在重新打包后保留。服务 URL 为每次启动动态注入，不写回用户配置。

此本地原型不默认启用登录。它只绑定本机回环地址。正式部署如需账户认证，应另行确定账户与授权策略，不沿用示例口令。

## 更新、重新打包与回退

先在管理页备份，然后停止全部服务；把新 ZIP 解压到新版本目录，双击新目录中的 EXE，继续使用相同的数据目录。不要把旧版 userdata、SQLite 或运行日志复制到交付包，也不要覆盖正在运行的目录。包的版本变化时，启动器会在加载数据库之前自动备份 SQLite 与 JSON 配置。数据 Schema 版本不匹配时拒绝启动，待专门迁移验收。

目前更新采用整目录替换方式，无在线更新器。相同数据 Schema 可在停止服务后启动旧目录回退；模型须与已有产品保持一致。若产品不同，使用匹配的模型发布包或另一个数据目录，启动器不会自动替换用户产品数据。

开发人员通过 `tools/build_product_exe.py` 指定已有 V3 发布目录重新封装，构建器不触发训练、不读取生产数据库。`--bootstrap` 可复用带源码指纹的相同 EXE 启动器；启动器代码变化则必须重新编译。各包带文件 SHA256 清单；哈希用于文件完整性校验，不等于发行方数字签名。

## 诊断命令

窗口化 EXE 的命令结果请通过 `--report` 写到指定 JSON 文件，例如：

```powershell
.\product.exe --status --report "$env:TEMP\pipeline-status.json"
.\product.exe --verify --report "$env:TEMP\pipeline-verify.json"
.\product.exe --backup --report "$env:TEMP\pipeline-backup.json"
.\product.exe --restart
.\product.exe --stop
```

恢复时先停止软件并选择**新的空目录**，执行：

```powershell
.\product.exe --restore "D:\Backups\backup-xxx.zip" --data-dir "D:\PipelinePerRecovered" --report "$env:TEMP\restore-report.json"
.\product.exe --data-dir "D:\PipelinePerRecovered"
```

恢复过程验证 ZIP 文件清单及 SHA256、SQLite 完整性和交付契约后再写入，不覆盖已有数据库或配置。P4.1 新备份附带安装与契约状态，运行控制令牌不进入备份。恢复范围为 SQLite 与 JSON 配置，导出文件、日志和外置模型须另外保管。旧 P4 未带清单的备份 ZIP 不会被当作新版备份静默接受，应先保留原包，另行核验迁移。

整目录回退也必须满足交付契约；涉及业务字段、编码、价格单位或数据 Schema 的变更需要单独迁移。不要删除 `delivery_state.json` 来绕过检查。

如启动失败，查看用户目录 `logs\launcher.log`、`price.log`、`effectiveness.log`、`web.log`。后台进程由启动器持有的 Windows Job 管理；强制结束启动器会清理其子进程，不会按进程名称批量结束其他软件。无法建立 Job 时拒绝启动并记录错误。

## Windows 7 验收门槛

构建运行时为 CPython 3.8.10 x64，价格依赖沿用 V3 验收环境。但 PyInstaller 5.13.2 官方要求 Windows 8 或更新版本，因此当前包不能标记为 Win7 可交付。必须在目标 Win7 SP1 x64、实际补丁和 VC 运行库环境上验证引导器、所有 DLL、模型加载、浏览器功能、停止与重启、独立数据持久化和重新打包保留数据；未通过时应调整构建工具链再验收。

参考：[PyInstaller 5.13.2 要求](https://pyinstaller.org/en/v5.13.2/requirements.html)、[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。Win7 不支持嵌套 Job，因此尤其需要在实际启动环境验证进程管理。

包内 `TARGET_CHECK.bat` 与“目标机验收说明.md”可收集目标机执行证据；Windows 11 上通过该脚本仍不会标记 Win7 已通过。
