# 甲方离线开发包 · Optuna V3

用途：在甲方开发机上读取本地产品表，使用现有 Optuna V3 训练，验证后生成带真实 `product.exe` 的完整运行压缩包。Python、训练依赖、程序源码、预编译原生启动器和虚拟示例均已包含；常规使用不需要安装 Python、Git、PyInstaller 或编译器，也不需要访问互联网。

**目标系统是 Windows 7 SP1 x64。当前没有 Win7 实机，已有完整流程测试属于 Windows 11 x64 结果；不能将此包当作已完成 Win7 首机验收。** 请先在甲方执行环境检查和示例自检，再使用真实数据。Win7 需具备可正常加载 CPython 3.8.10 和数值依赖的系统补丁／VC 运行库；不自动修改系统或安装补丁。

## 现场顺序

1. 完整解压到较短且可写的路径，如 `D:\PipelineBuilder`。保留整个目录结构。
2. 双击 `CHECK_ENVIRONMENT.bat`。通过后双击 `SELF_TEST.bat`，以包内虚拟表执行 40 次试验的完整训练、HTTP 验证、EXE 封装及启动检查。
3. 用甲方产品表替换 `input\products.xlsx`，编辑外部 `settings\project.json`：填写产品编码和名称，真实数据将 `data_kind` 改为 `customer`。仍使用虚拟数据时保留 `synthetic`。
4. 双击 `CHECK_INPUT.bat`。修正表格诊断中的错误后，双击 `TRAIN_AND_PACKAGE.bat`。默认按 `settings\v3.json` 训练 300 次试验，耗时取决于数据和机器。
5. 完成后从最外层 `deliverables` 取走 ZIP、`.sha256` 和构建回执。解压生成的 ZIP，双击其中的 `product.exe`。训练过程、模型、API 验证及错误日志保留在 `project\outputs\client_jobs`。

只有训练、接口验证、封包和实际 EXE 启动检查成功后，新的压缩包才复制到最外层 `deliverables`。训练指标未达到配置目标时，会在构建回执中保留实际状态；软件链路通过不等于真实业务价格准确率已经确认，请复核训练报告及独立业务样本。

## 产品表

支持双行表头 XLSX／CSV。第一行为中文名称与单位，第二行为稳定英文代号，第三行起为记录。示例：

```text
成品编号,成品名称,价格(万元),额定压力(MPa),部件数量(个),材料类型
product_id,product_name,price_wan,pressure_mpa,part_count,material
P001,产品一,12.5,18,2,铝合金
```

`examples\products_example.xlsx` 是 160 行、60 属性的虚拟演练表，不是业务数据要求或固定属性模板。真实产品可使用自己的属性。价格单位按万元输入；不要把元直接填入 `price_wan`。数值字段须保持数值，枚举值保持一致，训练缺失值按原有 V3 规则处理，不会擅自替换或插补。先处理缺失和无法解析的公式值，再训练。

可在工作簿使用已有价格属性评分／属性类型表，或编辑 `settings\v3.json` 的专家与字段类型配置。训练引擎显式锁定 `optuna_v3`；算法实现保持既有版本，历史搜索的自动复用默认关闭，避免无意使用其他项目记录。

## 外部配置

所有端口在 `settings\desktop.json` 设置，不写在程序代码中。例如：

```json
{
  "ports": {"control": 17890, "web": 17891, "price": 18101, "effectiveness": 18102},
  "open_browser": true
}
```

这些数值是可编辑样例。构建后成为新运行包的初始配置；实际运行时读取用户数据目录的 `config\desktop.json`。以后修改该文件并退出／重新启动软件即可，无需重新训练或打包。非零端口必须互不重复且为 1024–65535；`0` 只用于隔离诊断。端口占用会报错，不静默改用未知端口。

样例配置的 Portal 是 `http://127.0.0.1:17891/portal`，运行管理是 `http://127.0.0.1:17890/`。若没有自动打开浏览器，可看运行包的“访问地址.txt”、双击 `SHOW_ADDRESSES.bat`，或查看用户目录 `runtime\access.txt`。

## 只重新封装

训练完成并通过接口验证后，可以修改端口等交付配置，再双击 `REPACK_LAST.bat`。它复用上次已验证的模型，不再次训练，仍会重新封装并执行 EXE 启动检查。每次使用新版本目录；不覆盖既有模型、运行包或用户 SQLite。

开发包与运行包分开：开发包保留训练数据和模型审计；运行包包含模型、推理依赖、业务发布数据及程序。甲方的真实数据、模型和生成 ZIP 均留在甲方文件系统；没有自动上传 GitHub、遥测或云训练步骤。

## 服务管理和持久化

生成的运行包提供单独启动、停止、重启价格／效能／网页服务，以及全部重启和退出。主动停止的服务不会被自动拉起；独立重启保留端口。网页服务首次启动需要价格和效能服务已启动。

SQLite、配置、日志和备份默认保存在 `%LOCALAPPDATA%\PipelinePer`，可用 `--data-dir` 指定其他独立位置。换模型、更新和恢复仍执行 P4.1 契约检查，字段或价格口径不兼容则要求明确迁移。备份恢复命令及目标机演练见生成运行包中的说明。

## Win7 首机门槛

本开发包使用 CPython 3.8.10 和锁定的训练依赖；运行入口采用针对 Windows 6.1 的 x64 原生 Win32 EXE，普通用户权限，独立于 PyInstaller，引导器仅导入 Kernel32／User32。静态目标与本机通过不替代 Win7 实测。

如果 Win7 上包内 Python 无法启动，请保留系统错误截图和控制台输出，由甲方 IT 核对运行库及系统更新。不要通过关闭安全策略或绕过签名警告来验收。通过环境检查后依次完成虚拟训练封装、实际 EXE、浏览器操作、单服务控制、保存／备份／恢复和目标机 `TARGET_CHECK.bat`。使用支持当前网页 JavaScript 的已安装浏览器。

参考：[Python Windows 支持说明](https://docs.python.org/3/using/windows.html)、[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。Win7 不支持嵌套 Job；当前运行仍持有自己的 Job 并监视父进程，无法建立安全的进程所有权时明确失败。
