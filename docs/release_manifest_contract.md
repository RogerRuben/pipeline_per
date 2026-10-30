# 发行与模型身份契约（P4/P5草案）

发行 Product/ 内含 product.exe、runtime/、web/、models/、config/、manifest.json。可写数据位于独立用户目录（默认建议 LOCALAPPDATA/PipelinePer，最终由P4确定）；SQLite、设置、日志、导出、备份不得在升级时覆盖。发行目录内种子数据库仅首次复制。

manifest 必须包含 format_version、release_id、product_code、source_commit、build_time_utc、price_engine=optuna_v3、price_unit=万元、source_sha256、schema_hash、model_version、price_model_sha256、Python及所有运行依赖版本和文件哈希、静态资源哈希、SQLite迁移版本、effectiveness.backend/version/hash、可加规则版本、功能开关。

本次不生成伪正式 manifest：正式源表和 V3 模型尚未选定。audit/source_baseline.json 仅描述代码基线，不能代替发行身份。

启动：获取实例锁→验证包/模型hash→创建或检查外部数据目录→启动仅本机服务→健康/schema/实际预测就绪→打开 /portal。只终止本实例创建的进程树；服务故障可诊断，不能训练/自动换模型。数据迁移前备份并提供回滚。

EffectivenessProvider 草案：schema() 返回稳定字段/单位/任务ID；evaluate(parameters, task_id, requirement_overrides) 返回既有业务外层结果；health() 返回 available/backend/version；model_manifest() 返回包格式/SHA/来源。trial_snapshot 必须标试用；正式 V13 整包校验并对照专家软件 decision_score、continuous_score、direct_score、product_score、task_transfer、可视化和贡献。V13未到位时保持 trial 或 unavailable。
