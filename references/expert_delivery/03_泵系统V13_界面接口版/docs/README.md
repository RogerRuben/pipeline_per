# 系统级效能评价 V13 泵特化版

版本：V13.0，2026-09-03。

本目录从系统级 V12 独立升级，不覆盖 V12、V11 或其他封盘版本。V13 保留要求锚定联合 LP-UTA、物理可行性、公式、复核、冻结模型和未知方案评价，修复因耦合/公式联动导致的可靠覆盖停滞，以及跨任务复核编号重号问题。

## 解决的问题

- 选高流量任务时，先在高流量要求附近学习，避免一开始总拿个位数流量方案与高流量方案比较。
- 当前区域学到要求后，自动提示并切到中流量、低流量等代表区域；A/B 两边始终来自同一规格带，低流量区域不再固定输给高流量 A。
- 每条专家判断保存当时的区域校准任务快照，允许同一批判断在多个工况下共同训练。
- 共享 UTA 学全域共同趋势，区域 UTA 学局部修正；证据少时自动收缩到共享模型，新任务在邻近区域之间平滑插值。
- 未知方案评价增加六属性曲线、区域敏感性、跨工况情景表和流量-重量历史分布图。
- 可靠覆盖按主动设计变量判断聚焦程度，耦合被动变化和公式派生变化不再误伤控制变量证据。
- V12 状态可在 V13 中自动迁移；全部旧判断保留，复核编号去重后模型自动重算。

## 评价结构

```text
成品参数
  -> 公式回算与物理可行性
  -> 压力、流量、重量映射到液压系统
  -> 当前真实系统要求下的任务适配 UTA
  -> 功率、工作时间、脉冲压力等成品固有性能 UTA
  -> alpha 双通道合成
  -> 连续效能、硬性要求与最终决策效能
```

训练时使用自动生成的代表规格校准任务，评价未知方案时始终使用用户选定的真实系统任务。校准任务不会写回或篡改 Excel。

## 启动

```powershell
python system_app.py data/system_v13_pump_demo.xlsx
```

默认地址：`http://127.0.0.1:8799/`。

开发测试可使用：

```powershell
python system_app.py data/system_v13_pump_demo.xlsx --state-dir state_dev --no-browser
```

## 版本契约

- 系统状态：`system_profile_version=13`
- State 前缀：兼容沿用 `system_v12_pump_`，用于自动迁移 V12 判断
- Excel：继续读取通用 `system_excel_v10` 契约
- 冻结运行包：`system-effectiveness-frozen-runtime-package-13.0`
- 冻结模型：`system-effectiveness-frozen-model-13.0`
- 默认端口：`8799`

## 主要文件

- `system_app.py`：状态、区域训练闭环、HTTP API、评价与导出。
- `regional_context.py`：主规格识别、代表区域发现、校准任务和任务距离。
- `hierarchical_uta.py`：要求锚定双通道稳健联合 LP-UTA。
- `interactive_project_app.py`：生成、可行性、耦合和复核基础内核。
- `frontier_model.py`：部分可辨识物理前沿。
- `formula_constraints.py`：安全通用公式与历史校准。
- `frozen_system_model.py`：V13 隐私冻结模型和离线推理。
- `system_ui.html`：专家训练与未知方案评价界面。
- `create_pump_v13_demo.py`：六属性、跨数量级泵演示 Excel。
- `test_system_v13_regional.py`：V13 区域与冻结模型专项测试。

## 文档

- `系统级用户操作说明_V13.md`
- `系统级Excel填写指南_V13.md`
- `系统级技术总报告与维护手册_V13.md`
- `系统级冻结模型调用与迁移说明_V13.md`
- `系统级V13开发验收说明_20260903.md`

## 验收状态

- 自动回归：以 V13 最终验收报告为准。
- 浏览器：桌面 A/B 等高；移动端训练和评价无横向溢出；控制台无错误。
- 区域约束：所有测试区域内 A/B 均处在同一主规格带。
- 冻结推理：在线评价、冻结模型评价及运行时任务要求覆盖结果一致。

## 交付包

Windows 7 x64 无 Runtime 正式包：

```text
compare/deliverables/系统级效能评估专家版_V13_泵特化_WIN7_x64_无Runtime_20260903.zip
```

最终大小、SHA-256 和黑盒检查数量见同目录构建及验收报告。交付包不含 Runtime、甲方数据、专家 State 或测试点击；支持复制 V12 `userdata` 后自动迁移，V11 及更早 State 不兼容。
