# R4.3.1 缺失模块修复

故障原因：R4.3补丁中的tools/product_r4_input.py在读取Excel时导入tools.price_attribute_input，但补丁清单漏带tools/price_attribute_input.py，原R4.1完整包也没有该文件。即使Excel不带专家评分表，也会先执行这条导入。该问题与用户表格内容、管理员权限、Windows或sklearn版本无关。

已有R4.3安装目录：

1. 结束正在运行的服务，将本补丁完整解压覆盖到原目录（能看到START.bat、runtime、tools、config的那一层）。不要只解压到单独的补丁文件夹。
2. 运行APPLY_R4_3_INPUT_FIX.bat。此入口只补齐依赖并更新补丁文件的完整性记录，当前训练参数、专家评分、输入表及模型均不修改，不会训练。
3. 运行CHECK_INPUT.bat确认输入正常，再运行RETRAIN.bat。后续START.bat继续直接加载保存模型。

首次从R4/R4.1/R4.2升级：本包为累计补丁，可以直接覆盖完整目录；需要启用R4.3默认训练设置时，仍运行APPLY_R4_3_PATCH.bat或其他预设入口。已有自定义参数时应使用上面的INPUT_FIX入口。

修正了补丁和完整包构建清单，并增加打包前的本地模块依赖检查。验收增加“无源码目录兜底的独立安装目录→实际CHECK_INPUT.bat→Excel重新导入→实际RETRAIN.bat”，防止工作区里存在模块导致测试假通过。本次没有改变R4.3训练算法、业务精度门槛或环境依赖。
