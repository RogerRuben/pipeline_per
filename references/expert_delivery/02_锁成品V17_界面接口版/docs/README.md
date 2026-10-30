# 成品级 V17 锁特化效能与复用评估

V17 是独立于成品 V15 的锁产品特化版。它围绕一份选定的基准技术协议，从基准向较差方向试探，学习每个属性的单项复用极限、属性间权衡、UTA 扣分曲线和整机综合复用分界。

本版只评价效能与复用，不学习或输出物理可行性、耦合关系和可制造性结论。

## 快速启动

开发环境：

```powershell
cd C:\Users\2\OneDrive\桌面\job\compare\product_level_v17_lock
python lock_v17_app.py data\product_v17_lock_demo.xlsx
```

默认页面：`http://127.0.0.1:8798/`

也可运行：

```powershell
python portable_launcher.py
```

启动器会让用户选择 Excel，复制工作副本到 `userdata/projects`，再用独立的 `userdata/state_v17_lock` 保存 V17 点击状态。

## 训练流程

### 第一阶段：逐属性复用极限

1. 方案 A 始终是选定基准，固定 100 分。
2. 方案 B 每次只让一个属性向较差方向变化。
3. 专家判断 B 是“仍可复用”“已不可复用”或“暂时难判断”。
4. 系统先扩大退化幅度，再用二分夹逼单项边界。
5. 当前属性完成后恢复基准，再处理下一属性。

### 第二阶段：稀疏多属性权衡

1. A、B 都从基准出发。
2. 每个方案只退化 2 至 3 个属性，绝不超过 3 个。
3. 每项退化都严格位于单项阶段已经明确确认的可复用侧，不越过单项极限。
4. 专家分别判断 A、B 整体能否复用，再判断哪个更值得复用。
5. A/B 偏好进入 Bradley-Terry 和 LP-UTA。
6. 整体复用标签用于标定综合复用分界。

单项边界会被映射到每条UTA曲线的统一有效位置。完成最低覆盖后，多属性题目按已确认可退化宽度自适应分配：可调整空间较宽的属性会多出现，但全部属性都会持续覆盖，采样次数不等于属性权重。

## 评分口径

基准固定 100 分：

```text
U(x) = 100 - sum_j Loss_j(d_j)
```

- `d_j` 是属性相对基准的无量纲退化程度。
- `Loss_j` 是 LP-UTA 联合学到的属性权重与边际扣分曲线。
- 优于基准的属性按达到基准处理，不加到 100 分以上。
- 好属性不补偿其他属性的退化。

未知方案同时输出总分、Bootstrap稳健区间、整机复用结论、单项复用状态、单项边界、主要扣分属性、扣分贡献和曲线图。

Excel可以放多份评价基准，但专家只围绕第一份主基准训练一次。选择较低基准评价时，软件把每条已学习UTA曲线的形状、权重和相对单项极限迁移到新基准的工程量轴上；优于当前基准的部分截断为100分，不重新训练，也不允许跨属性补偿。若新基准优于主基准或迁移超出证据范围，结果会明确标记不支持或低置信外推。

## 主要文件

| 文件 | 作用 |
|---|---|
| `lock_v17_app.py` | Web 服务、两阶段生成器、交互、状态、复核、评价和导出 |
| `lock_utility_model.py` | 基准退化坐标、BT/LP-UTA 包装、在线与冻结共用评分逻辑 |
| `preference_models.py` | 从 V15 复用的 Online BT 与 LP-UTA 学习器 |
| `project_excel.py` | Excel 读取、属性配置和基准技术协议解析 |
| `lock_ui.html` | 中文专家界面、A/B 对齐、高亮、边界图、UTA 图和未知评价 |
| `frozen_lock_model.py` | 数据最小化冻结包和无 Excel 推理运行时 |
| `portable_launcher.py` | Windows 离线文件选择、工作副本、状态与浏览器启动 |
| `audit_lock_v17.py` | 确定性虚拟专家闭环审计 |
| `test_lock_v17.py` | 算法、状态、冻结等值和隐私测试 |

## 状态与版本

- 状态契约：`lock_product_degradation_v17`
- 学习指纹前缀：`lock_v17_`
- 默认状态目录：`state_v17_lock`
- 冻结模型：`lock-product-reuse-model-17.0`
- 冻结运行包：`lock-product-reuse-runtime-package-17.0`
- 默认端口：`8798`
- 不迁移 V15 或其他旧版本 State

一份 Excel 中可以有多份基准技术协议。第一行是训练主基准，其余行是可选评价基准；切换评价基准不会创建新训练会话，也不会覆盖主模型。

## 验证

```powershell
python -m unittest -v test_lock_v17.py
python audit_lock_v17.py --multi-rounds 55 --output V17_algorithm_audit_20260827.json
```

详细资料：

- `锁成品V17_专家操作说明.md`
- `锁成品V17_Excel填写指南.md`
- `锁成品V17_技术说明与维护手册.md`
- `锁成品V17_冻结模型调用说明.md`
