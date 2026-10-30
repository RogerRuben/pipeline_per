# P1-2 综合功能集成版

本轮以用户最新 A–D 扩大范围为准。新附件与前次审查计划内容相同，其“仅P1-0/P1-1”等旧范围没有覆盖本轮用户授权。本版继续在codex/p1-price-gateway，不合并main、不改AutoML V3算法、不启用可加/混合定价、不封装EXE。

## 可完整操作的流程

Portal → 价格深度分析 `/price/deep` → 选择当前激活产品 → 编辑业务属性 → 预测 → 单参数/Top10曲线 → 多属性情景比较 → 读取真实模型指标 → 导出CSV/TXT。

- 首页沿用参考index的深蓝标题、分组参数/价格卡、雷达与价格对比、敏感性区域等结构。字段身份、标签、单位、枚举/布尔和分组由业务Schema提供。自定义分组保留原标签，未配置分组时显示其他参数，不推断成正式业务分类。
- 数值型布尔0/1与true/false均可回填；情景提交直接读取各方案当前控件值，兼容键盘、粘贴和自动填充。切产品/改属性清理旧结果显示；异步计算期间变化的输入不会显示旧响应。
- 分析经现有HTTP价格单/批接口，无pickle/CSV加载和效能联动。单参数按合法范围采样，整数去重，枚举/布尔仅合法值。Top10在可计算字段中采样比较、排序后返回最多10项；总请求不超过100，最多49字段。图表的采样序号不是统一的属性变化百分比；每项真实取值和价格在表格中列明，零基价变化率为null。
- 情景可独立修改多项属性，保持方案顺序和名称；所有结果绑定相同发布、模型实例/哈希和单位配置，禁止混版。
- 模型评价只展示价格服务实际加载产物提供的metrics/feature_importance，专家评分独立标注。没有重要度或解释时明确暂无/未配置，不编造排序或大模型文本。
- 服务器为计算结果生成result_id，绑定独立dp_session随机会话与认证令牌，30分钟有效，退出登录清除会话结果。缓存限制每会话100、全局1000条；不改变业务SQLite或发布状态。导出不能接收客户端伪造价格，CSV对公式型文本转义。
- 时间/数量校正仅使用本会话原始单预测，分别返回原价、年份校正价与最终校正价。默认无确认规则，界面禁用并说明。原始预测不会被校正覆盖，也不能重复校正已校正结果。

## 规则配置

复制config/deep_price_adjustment.example.json到运行数据目录下config/deep_price_adjustment.json，再由业务负责人提供真实version/source/approved_by/approved_at、价格基年、各年PPI、参考数量和弹性，设置confirmed=true。示例不包含可误用的PPI/弹性数值。

本契约的方向显式为base_to_target：`P_model × PPI(target)/PPI(base) × (quantity/reference_quantity)^elasticity`，表示模型价以规则基年为基准。确认业务含义后才启用；缺年份、零/负指数、非法数量或无审批信息都拒绝计算。结果包含实际规则与SHA256以便追溯。

## 直接试用与复验

```powershell
<Python3.8解释器> tools/run_deep_price_demo.py
# 打开 http://127.0.0.1:18891/portal
python tools/run_p1_checks.py --python <Python3.8解释器>
```

演示启动器在outputs/deep_price_demo创建独立虚拟数据库，复用已有虚拟JSON价格/效能快照，启动本机HTTP服务并显式激活演示业务发布，不训练、不操作生产库。演示仅绑定127.0.0.1，不要求登录；正式部署仍沿用既有Portal认证配置。关闭启动器后服务关闭；重启时复用演示数据。它不是product.exe或正式封装。

正式部署使用现有发布管理准备/激活产品，配置本版价格服务及Portal HTTP模式、local_fallback=false。原run_app入口现在打开Portal。已有手工禁用的导航配置不会被强行覆盖，必要时由管理员启用advanced_price，URL为/price/deep。

## 四组提交与回归修复

A接页面和动态字段；B接分析与真实模型摘要；C接规则与会话导出；D处理实际浏览器发现的布尔回填/情景输入同步、生成批次规范化、完整回归和证据。

旧“生成方案可独立查看”失败原因：直接创建任务未注入scenario默认值，而推荐查询先应用scenario_policy，导致同一需求的fingerprint不同。现统一在canonicalize_generation_controls中注入情景默认值，不改排序或生成算法。完整旧流水线已能继续运行至HTTP生成批次与详情；退役绑定表、已委托给子BAT的启动路径和UI/API版本等旧断言改为验证当前契约，不恢复废弃接口。

当前14个实际接口冻结于contracts/deep_price.integrated.openapi.json。截图、实际下载样本、网页/HTTP价格对照、环境/源码/模型身份与测试记录见P1_2_validation.md及audit/p1_2。交互回放是实际截图的分步回放，不冒充实时视频。

正式products.xlsx、正式Optuna V3产物与依赖、Win7实机及正式EXE仍未验收。虚拟数据通过仅代表本轮功能开发验收，不能替代正式模型精度/兼容性认证。
