# 深度分析功能迁移与回归矩阵

全部新端点均是拟议契约，当前未挂载；前缀 /api/deep-price/v1。原路由函数与行号见 audit/attachments.json。

| 参考控件/旧路由 | 新接口/组件 | 阶段 | 验收用例 |
|---|---|---|---|
| index 产品选择 /api/products | GET /products、/products/{product_id} | P1 | 分页、选择回填、切产品清除旧结果；产品ID非product_code |
| index 功能/性能/环境参数 | GET /schema，动态控件 | P1 | 中文显示英文键；整数/枚举/缺失/单位、映射一次；无中文硬编码 |
| index /predict | POST /predict | P1 | 与推荐同产品/模型/参数价格一致；服务断开503；不读Excel |
| index /price_adjust | POST /price-adjust | P1 | 基年、PPI与数量边界；无配置返回not_configured；原价和调整价分列 |
| index 解释状态/流 | GET /explanation/status | P1插槽 | not_configured 可见；不请求外网，不伪造解释；流式实现延期 |
| /save_csv、/save_txt | POST /export | P1/P2 | UTF-8、来源身份、公式注入防护、过期/越权结果拒绝 |
| factor_analysis 属性重要度 | GET /feature-importance | P2 | 真实重要度/专家评分分列；无数据unavailable，不编造排序 |
| /api/sensitivity_features | GET /schema | P2 | 只呈合法可计算字段，枚举不作连续量 |
| /api/sensitivity_single | POST /sensitivity/single | P2 | 合法采样去重；真实价格曲线、基准零价不做除法 |
| /api/sensitivity_top10 | POST /sensitivity/top10 | P2 | 变化率，有限值，标注截断/缺失；专家加权不是价格变化率 |
| scenario_analysis 字段/预测 | GET /schema、POST /scenario | P2 | 多属性同网关、批量限额、版本锁、部分失败明示 |
| model_evaluation /api/model_sets | GET /model-summary | P2 | 展示当前已发布摘要、训练/验证/测试指标分清 |
| /api/upload_model、/api/active_model | 无分析写接口 | P1/P2 | 无前台入口；直接调用404/403，模型和DB哈希不变 |
| 可加属性敏感性 | 统一Gateway规则路由 | P3 | 固定/区间/部件/分段/减配/逆向；混合修改明确拒绝 |

页面统一用 Portal 登录和同源资源。新接口全部计算只读业务/发布状态，导出仅写临时文件。对照 index 原截图进行视觉验收；本次尚无新页面截图。正式价格测试必须带当前已发布 release_id 和 artifact SHA，不能用旧样本预测作为正式金标准。
