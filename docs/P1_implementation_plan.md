# P1 首批实现范围（未实施）

目标：可在浏览器运行的深度分析页；不训练、不打包、不修改泵算法。

| 拟修改/新增 | 内容 | 回归影响 |
|---|---|---|
| app/deep_price/{gateway,schema,analysis,export}.py | 发布数据读取、版本锁定、业务编码、HTTP-only网关、调整和导出 | 价格工作台/业务字段映射 |
| app/server.py | /price/deep 和同源 /api/deep-price/v1 路由；沿用认证 | 登录/静态资源/现有推荐路由 |
| app/model_service_client.py | 价格专用batch，不依赖效能在线 | 推荐单/批价格归一化 |
| app/configuration.py | 深度导航在集成就绪后启用 | Portal配置与旧配置合并 |
| app/static/deep_price/* | index 布局、动态字段、回填、预测、图表、调整、导出、未配置解释 | 原首页交互与离线资产 |
| tests/deep_price_contract/* | HTTP真实服务一致性、映射、权限、超时、无写入、版本变更 | 不写生产库，使用临时SQLite与已知测试模型 |

先完成稳定字段/schema和网关，再接页面；已存在的 _enrich_workbench_schema 可复用，但 price_workbench_predict 的本地兼容回退不能作为新入口。产品读取需显式选择当前激活版本，不能调用会创建/激活版本的管理方法。

实施前证据缺口：正式产品表、当前 V3 模型及其训练环境、分组/单位/类别映射、价格校正基年/PPI来源/批量弹性业务规则。可用虚拟数据开发，但必须明确 fixture 身份；规则缺失返回not_configured，不能复制旧PPI当正式规则。

验收：推荐与深度单/批同价同版本；移动原Excel后运行不受影响；请求前后业务DB/模型hash一致；模型页只读；Portal会话未登录401；计算服务断开503；中途换版409；导出带来源和单位。P2恢复完整敏感性/情景/评价，P3纯可加，P4真EXE，P5正式V13。
