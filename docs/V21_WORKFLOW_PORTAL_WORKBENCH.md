# V21 推荐工作流、服务导航与 Workbench

V21 只优化操作体验和页面适配层，不改变价格/效能 API 协议、模型、生成搜索、推荐排序，以及 Business / Model / Display Value 语义。

## 推荐工作区

- 左侧条件栏默认宽度 390px，可拖动、双击复位或用方向键调整；范围为 320px 至 `min(680px, 55vw)`，宽度仅保存在浏览器 `localStorage`。
- 技术条件按“指标分组 → 指标 → 条件”编辑，可按中文名或 `parameter_id` 搜索。
- Bootstrap 根据现有 `tag_rules` 提供 UI-only 标签覆盖表。默认隐藏标签已覆盖的候选指标，但已经建立的显式条件始终保留且优先。
- 页面提供常驻流程状态、首次 Tour、独立的历史推荐/智能生成操作卡，以及历史/生成/全部三个来源 Tab。

## 系统导航与认证

- `/portal` 是登录后的默认首页，配置智能推荐、简易价格、价格深度分析、简易效能和数据管理五个模块，并只展示已启用入口。
- 人员导航地址只由 `config/service_portal.json` 管理；`config/model_services.json` 继续只管理机器调用的价格和效能 API。
- Portal URL 仅允许本地绝对路径或 `http` / `https` 地址；价格深度分析默认关闭，取得实际部署地址后再启用。
- 登录保留合法本地 `next` 路径，并拒绝绝对 URL、协议相对 URL等开放重定向输入。
- `run_app.py` 使用自动选择到的实际主服务端口打开 `/portal`。

## 简易 Workbench

- 两套 Workbench 通过同一个选择规则取得历史示例：配置协议优先；否则按当前成品、历史/导入来源、年份倒序、更新时间倒序、协议号升序稳定选择。
- 每个模型字段按“历史值 → 模型默认 → 训练均值 → 首个允许值 → 参考范围中点”补齐。
- Schema adapter 以 DataMaster 的中文名称、单位、业务类型、允许值和 `display_value_mapping_json` 丰富展示数据；外部模型新增字段仍按模型标签或字段 key 降级显示。
- 页面和数据库保持 Business Value，调用模型服务前才通过 `Store.runtime_parameters()` 编码。请求 JSON 的字段 key 始终保持模型英文 key。

## 配置

`config/workbench_defaults.json` 可指定固定历史示例：

```json
{
  "historical_example_agreement_id": "AGR-001"
}
```

值为空或协议不可用时使用上述确定性自动选择规则。

## 验证

V21 专项回归入口：

```powershell
python tests/v21_workflow_portal_workbench_test.py
```

同时应继续运行 V20 的 value semantics、display mapping 和 range nonblocking 测试。
