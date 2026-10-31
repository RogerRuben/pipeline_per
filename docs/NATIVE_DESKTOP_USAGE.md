# PipelinePer 原生入口运行包

完整解压后双击 `product.exe`。它是带原创图标的 x64 Win32 GUI 入口，使用包内 CPython 3.8.10 启动监督器及价格、试用效能、网页三个服务。无需外部 Python。关闭浏览器后服务继续运行，退出请使用管理页“退出软件”。

## 地址和外部端口配置

先查看软件目录“访问地址.txt”，也可双击 `SHOW_ADDRESSES.bat`。软件初次运行将默认配置复制到用户数据目录 `config\desktop.json`；以后以这个外部 JSON 为准。修改后退出软件再重新启动生效，无需改代码、训练或重新打包。管理页显示正在使用的配置路径和各服务实际地址；用户目录 `runtime\access.txt` 也记录访问地址。

端口值来自交付配置，可分别设置管理、网页、价格、效能四个端口。端口占用时明确报错，不静默换端口。`open_browser` 控制是否自动打开浏览器。

## 单服务操作

管理页可单独启动、停止、重启三个服务；其他进程保持运行。主动停止的服务保持停止，直到手动启动或下次完整启动软件。网页服务启动前，价格和效能服务须已启动。价格服务重启后旧分析请求会因模型实例身份变化被拒绝，请在深度分析页面刷新产品版本。

```powershell
.\product.exe --service price --stop
.\product.exe --service price --start
.\product.exe --service effectiveness --restart
.\product.exe --restart
.\product.exe --status --report "$env:TEMP\pipeline-status.json"
.\product.exe --stop
```

## 数据、更新和恢复

默认用户目录 `%LOCALAPPDATA%\PipelinePer`，SQLite 位于 `data\protocol_demo.db`，配置、日志、导出和备份分别在独立子目录。可用 `--data-dir "D:\PipelineData"` 显式指定，同一实例的命令使用同一路径。

首次才导入业务发布包；已有数据不会重新导入。新版本解压到新目录，继续使用原数据目录。更新前备份，换模型时验证字段／业务发布／价格契约；不兼容时停止并要求迁移。

```powershell
.\product.exe --backup --report "$env:TEMP\backup.json"
.\product.exe --restore "D:\backup.zip" --data-dir "D:\RecoveredData" --report "$env:TEMP\restore.json"
.\product.exe --data-dir "D:\RecoveredData"
```

恢复只写新的空目录；验证备份清单、SHA、SQLite 和兼容契约。恢复中断或已初始化后丢失数据库时拒绝重新导入默认数据。此包的效能仍为试用快照；真实价格准确率需以甲方数据和独立样本验收。

## Win7

原生入口按 Windows 6.1 编译，仅调用 Win7 可用 Win32 API，不依赖 PyInstaller 或额外 C 运行时；模型解释器和数值依赖仍有自身运行库／补丁要求。当前没有 Win7 实机，不能标记整包已通过 Win7。请运行 `TARGET_CHECK.bat` 并在目标浏览器实际操作后再确认交付。启动器和服务仍保持 Job 所有权及父进程监视，无法建立时明确失败。
