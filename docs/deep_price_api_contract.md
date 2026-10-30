# 字段与 API 契约 v0.1（草案，未实现）

机器可读入口：contracts/deep_price.openapi.json（OpenAPI 3.1）。命名空间 /api/deep-price/v1；页面 /price/deep。该草案不是现有价格服务的原始响应结构，Gateway 必须做封装。

字段由当前发布产品业务定义与价格 schema 合并，以 field_id 匹配，中文名称不参与身份匹配。field_id→label_zh、unit、type、required、allowed_values/range、model_value_mapping、group、price_model_input、additive_rule_id 必须可追溯。无正式源表时不填写假列；虚拟示例字段不得作为产品默认字段。

Business Value → canonical_business_parameters → runtime_parameters（一次）→ 现有 HTTP 价格服务。模型所需字段筛选沿用当前 schema；不要在浏览器转换类别或将 -1 统一替换为零。源字段类型不合法应400；缺失值服从当前模型契约。

每次查询/计算返回 request_id、product_code、release_id、model_version、price_model_sha256 和 price_unit=万元；预测请求携带预期发布/模型身份，服务端以当前激活状态校验，不能相信客户端声明。SHA 不可得时 unavailable，不虚构模型身份。product_id 是业务产品记录ID，product_code 是发布产品族身份，两者不能互换。

单预测返回原始 predicted_price_wan，时间/批量调整独立展示。调整只有配置了经过核准的基年/PPI/批量弹性才执行；旧算法 price×PPI_BASE/PPI(year)×(quantity/STANDARD_QTY)^lambda 的方向和业务含义尚待确认，不沿用其缺年份默认值。

批量初始上限100，请求item_id唯一，保序、单一模型版本、30秒总预算；P1初版原子失败，任一项非法或版本变化整批错误，不伪造部分成功。P2如果支持部分失败必须升级契约并为每项增加status/error。Top10总采样不超过100且字段和数值合法；零基价 change_rate_pct=null。情景走同一BatchRequest。

错误统一 success=false/request_id/error.code/message/details。400 invalid_input；401 authentication_required；403 forbidden；404 not_found；409 version_conflict；413 batch_too_large；422 unsupported_mixed_adjustment/not_configured；503 unavailable；504 timeout。业务计算错误不静默回退本地模型。

解释/重要度/模型摘要用 status=available/unavailable/not_configured 和 metric_kind，data字段P1/P2进一步细化；专家评分与模型重要度必须不同metric_kind。无解释配置时不调用外网。

导出以服务器分配、绑定当前登录会话的 result_id 获取结果，禁止接受客户端伪造预测作为正式报告；预测/批量/敏感性/校正响应返回服务器生成的 result_id，绑定会话、30分钟有效，过期或非本会话访问返回404。文件UTF-8，包含身份/单位/规则版本，CSV转义公式型单元格。导出文件不可充当正式模型发布产物。

普通分析无上传/激活/切换模型接口。服务端登录校验、源检查/CSRF策略沿用Portal并在P1核对；会话cookie复用已核查的 ipdemo_auth。P1完成前须落实发布快照获取和规范化模型SHA来源。
