# 默认启动目录冲突修复 · 2026-11-02

用户在 R3 开发包生成运行包后，直接启动 product.exe 报 schema 不兼容。本机 launcher.log 证实，新产品 CLIENT_PRODUCT 尝试使用公共默认目录中原有 SYNTHETIC_V3_ACCEPTANCE 产品的 SQLite。此前自动 EXE 测试使用 --data-dir 指定临时目录，漏测已有公共目录时的默认双击入口。

修复后，产品编码的 SHA256 前 32 位决定稳定的独立默认目录：%LOCALAPPDATA%/PipelinePer/products/<hash>。重新封包或同产品模型升级不会更换目录。若公共旧目录属于同一产品，继续发现并使用旧目录，并保留原有完整兼容检查；无法核验旧数据库时明确报错，不能悄悄绕过。显式 --data-dir 和 PIPELINE_PER_DATA_DIR 仍优先。产品或 Schema 不兼容时，中文提示列出产品身份、数据目录和操作建议。

## 本机实测

- 桌面回归 18/18 通过，新增跨产品默认路径、同产品旧目录发现、升级路径稳定及不可读旧库保护用例。
- 复用用户 2026-11-02 已训练模型，没有重新训练；模型 SHA256 为 6580c65256e460c0be6fec165911386dd9696196db14bd3e34e958066a1aad3f。
- 修复 EXE 实际启动不传 --data-dir，正确选择 CLIENT_PRODUCT 独立目录；重复启动复用同一实例。
- 推荐、简易预测、深度预测同一输入均为 16.4538 万元。
- 公共旧 SQLite 的 SHA256 前后相同。原包、原模型和旧数据库未修改。
- 本机 17891 已由其他进程占用，修复包通过外部 defaults/desktop.json 将网页设置为 17892，其他服务仍为 17890/18101/18102，没有停止原占用进程。
- ZIP 中 2483 个封存文件全部与已测试目录的文件 SHA 一致。
- R4 开发包通过 CHECK_ENVIRONMENT.bat 完整性和包内依赖检查。本轮未重做完整训练测试，模型复用运行包的默认启动已单独实测。
- 测试后仅停止本次验证进程。用户重新启动修复包后访问 http://127.0.0.1:17892/portal 。

[默认入口实测](audit/default_startup_20261102/default_startup_acceptance.json) · [ZIP 核验](audit/default_startup_20261102/runtime_zip_verification.json)

修复源码提交：7846069a2e79668ae6b2418595ff6f843d6e1108。main 和其他 PR 未合并。

## 文件与交付边界

- 修复运行包：deliverables/client-v3-20261102-defaultfix.zip，SHA256 56cbddf30535a6f2c79812d8ab9eb93be72ae2c8329621d76779f06608d8ee28。
- 后续训练用 R4 开发包：deliverables/ClientBuilder_V3_Win7x64_20261102_R4.zip，SHA256 3a95e96fec50a229093a58e1534d330c10238f15d460cc8359ba118ac552df80。
- R4 的端口仍通过 settings/desktop.json 外部配置；在本机使用时请先将 web 17891 改为可用端口，例如本次使用的 17892。

当前原型运行包确实包含 payload 中的 Python 业务源码；开发包也包含构建源码。原生 EXE 启动器不等于业务逻辑全部编译隐藏。本轮只修复默认启动与交付说明，R4 不是无可读 Python 源码的保护发行包。HTML/CSS/JS 本身需要交付浏览器，和 Python 业务源码保护是不同问题。

目标为 Win7 SP1 x64，本轮仍在 Windows 11 测试，Win7 实机未验证。
