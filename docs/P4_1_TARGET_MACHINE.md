# P4.1 目标机验收

当前包仍是待验收原型，不保证 Windows 7 可运行。目标为 Win7 SP1 x64 普通用户环境。Python 版本一致不能证明引导器、DLL、Windows Job 与浏览器均兼容。

## 自动执行

将整个包复制到目标机的短路径（如 `C:\PipelinePer`），保留原始 ZIP 与 SHA256 文件。双击 `TARGET_CHECK.bat`。脚本仅使用包内 Python，在 `%LOCALAPPDATA%\PipelinePerTargetCheck_*` 下创建全新隔离数据，清空子进程 PATH，实际运行本包 EXE；不会连接、修改或停止已有安装，不会联网安装依赖或上传报告。

脚本测试文件完整性、三服务启动、重复启动、真实 V3 预测与一致性、Top10、通过实际 API 保存方案、在线备份、价格子进程退出恢复、启动器被结束后清理、陈旧状态恢复、备份还原到新目录、重新启动、保存方案／规则／价格一致、SQLite 完整性及禁止覆盖已有目录。

恢复演练使用 P4.1 版本化备份，包含文件清单和哈希。可在命令行追加匹配前版目录做回退与返回当前版测试：

```powershell
.\runtime\python38\python.exe -B -X utf8 .\payload\product\target_check.py --package . --out D:\P4Evidence\new_check --rollback-package D:\PipelinePerPrevious
```

输出目录必须全新，不能位于软件包内部，且其上级目录须存在。该恢复演练会主动结束其自己启动的进程，全部数据均隔离。若包内 Python 根本无法启动，查看输出目录旁的 `.bootstrap.log`；若 EXE 启动失败，查看该目录 `userdata\logs` 和 `target_report.json`。收集证据后由操作者决定如何传回，不自动发送到 GitHub。

## 人工浏览器步骤

自动脚本不能代替浏览器验收。完成自动步骤后双击 product.exe，在实际目标机浏览器操作并截图：

1. Portal 打开推荐、简易价格、价格深度分析、试用效能及运行管理。
2. 选择产品、修改数值和枚举、预测；敏感性曲线、Top10、情景比较可见。
3. 导出 CSV/TXT，与 API 原始结果和价格口径核对。未配置规则应显示未配置。
4. 保存用户方案、修改配置、备份、退出、新版启动、独立恢复、回退，确认数据保留。
5. 关闭浏览器后服务继续运行，使用管理页停止后全部服务退出；普通用户重复启动不出现多套实例。

记录操作系统版本／SP／位数、VC/UCRT 补丁状态、浏览器版本、包版本和 ZIP SHA、异常日志、Top10 耗时、是否运行于 PCA／其他 Job 环境。修复实际失败后须重新执行。不要在没有失败依据时关闭安全防护或兼容性策略来“通过”验收。

## 结论边界

`target_report.json` 在非 Win7 环境会明确给出 `not_a_win7_target_machine`。Win7 SP1 工作站上自动检查通过也只表示可执行链路通过，人工浏览器仍须独立验收；`win7_validated` 不会被脚本擅自设为 true。

PyInstaller 5.13.2 的官方要求为 Windows 8 或更新版本；Win7 不能使用嵌套 Job。本包仍采用已测试的 Job 所有权机制，失败时拒绝启动而非放任孤立进程。若目标机出现引导器／DLL／Job 失败，需要依据日志调整封装方案并再次实测。

参考：[PyInstaller 要求](https://pyinstaller.org/en/v5.13.2/requirements.html)、[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
