# OneClick 启动程序像素图标 · 2026-11-01

本轮将 `product.exe` 的启动文件图标重新设计为像素风，并重新编译原生启动器。图案由金色人民币价格币、青色效能仪表、白色点击指针以及底部连接节点组成，表达“一键串联价格与效能”。Portal 使用同一图形。

![从实际生成的 product.exe 资源提取的图标](audit/client_pixel_20261101/embedded_exe_icon.png)

图形以整数像素网格绘制，透明阶梯边角，不使用模糊缩放。ICO 包含 16、24、32、48、64、128、256 像素七档；16/24 像素采用单独简化的图案，其余使用整数倍近邻缩放。源文件为 `tools/create_launcher_icon.py`；产物为 `app/static/launcher.ico`、`launcher.png` 和 `launcher.svg`，甲方机器不需要 Pillow 或编译器。

## 更新后的离线包

- 文件：`ClientBuilder_V3_Win7x64_20261101_R3.zip`。
- 源码提交：`82d3fb48ccf853f0fb36761158834d7166a023dc`。
- 大小：72,009,278 bytes，约 68.7 MiB。
- SHA256：`c7e98723143a50bf242a49692fa04d9beb3cbc81af35fcb5dc4cb3b046a3b845`。
- [下载页](https://github.com/RogerRuben/pipeline_per/releases/tag/client-build-kit-pixel-20261101)。

新包更新预编译启动器与图标资源，并重新封存文件清单；现场执行 `TRAIN_AND_PACKAGE.bat` 或 `REPACK_LAST.bat` 时，生成的 `product.exe` 会携带新图标。旧 R2 包与已经生成的运行目录仍保留原图标，请使用新的完整包。

训练和现场使用步骤沿用 [离线开发包说明](CLIENT_BUILD_KIT.md)。外部端口配置仍位于开发包 `settings/desktop.json`，运行后的持久化配置为用户数据目录 `config/desktop.json`。

同一个 R3 ZIP 在本机实际解压后，11/11 项全流程检查通过，包括清空 PATH 后的环境检查、40 次真实 Optuna V3 训练、60 字段 HTTP 预测一致性、EXE 启动、模型不变的重新打包，以及样例误标和源码修改的拒绝检查。

实际验收范围和逐项结果见本目录的 [ZIP 全流程检查](audit/client_pixel_20261101/kit_acceptance.json) 和 [EXE 内嵌图标检查](audit/client_pixel_20261101/embedded_icon_checks.json)。EXE 检查从新包实际生成的 `product.exe` 中读取 Windows 图标资源，逐像素比对全部七个尺寸，7/7 一致；重新打包后的 EXE 也与首次生成的 EXE 完全一致。

目标系统仍为 Windows 7 SP1 x64；本机测试运行于 Windows 11 x64，Win7 实机验收尚未完成。本次没有改动训练算法，也没有合并 main。
