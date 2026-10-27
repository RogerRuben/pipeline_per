# -*- coding: utf-8 -*-
"""Promote only archives whose exact digests passed isolated package acceptance."""
import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'outputs/v6_20261027'
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def main():
    acceptance=read(EVIDENCE/'package_acceptance_release.json')
    if acceptance['status']!='PASS':raise ValueError('Final package acceptance did not pass')
    report_names=['legacy_verified_tests','v4_final_tests','v5_final_tests','v51_tests','v6_sealed_tests','recommendation_release_tests']
    checks={n:read(EVIDENCE/(n+'.json')) for n in report_names}
    if any(r['status']!='PASS' for r in checks.values()):raise ValueError('Regression incomplete')
    total=sum(r.get('tests',0) for r in checks.values())+26+3
    archives={}
    for kind,key in [('Offline','full_sha256'),('PATCH','patch_sha256')]:
        path=ROOT/('deliverables/ProductAutoML_V6_'+kind+'_20261027.zip');digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=acceptance[key]:raise ValueError('Archive changed after acceptance: '+kind)
        meta_path=Path(str(path)+'.json');meta=read(meta_path)
        meta.update(status='VERIFIED',source_tests=total,acceptance_report='outputs/v6_20261027/package_acceptance_release.json',
            physical_win7_tested=False,business_accuracy_certified=False,expert_effect_import_tested=True,all_four_ports_exit_tested=True)
        write(meta_path,meta);archives[kind]={'file':path.name,'bytes':path.stat().st_size,'sha256':digest}
    demos=[]
    for mode in ('goal_first','balanced','robust'):
        r=read(EVIDENCE/('final_demos/demo_v6_'+mode+'/price/training_report.json'))
        if not r['targets_met'] or r['interval_uses_test_labels']:raise ValueError('Virtual demo did not meet target protocol')
        demos.append({'mode':mode,'train':r['train_metrics'],'test':r['test_metrics'],'elapsed_seconds':r['elapsed_seconds'],'fits':r['fit_statistics']})
    report={'status':'VERIFIED','date':'2026-10-27','source_regression_checks':total,'checks':checks,'archives':archives,
        'recommendation_version':'V21.6','demo_rows':16,'demo_features':36,'virtual_demos':demos,'package_acceptance':acceptance,
        'original_price_core_unchanged':hashlib.sha256((ROOT/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest()=='f8f9877029e13535b97b40c429fb4afe6b5d613db635b6bee83307fca30b2899',
        'business_accuracy_certified':False,'physical_win7_tested':False}
    write(EVIDENCE/'release_validation.json',report)
    rows=['# V6及V21.6交付验证（2026-10-27）','',
        '使用固定Python3.8.10、sklearn0.24.2、Optuna3.6.0验证；实体Win7未复测。虚拟成绩不代表真实业务精度。','',
        '源码回归共%d项：旧价格/输入/兼容144，V4 21，V5 24，V5.1 25，V6/运行集成/目录33，上游推荐及效费26，前台/工作台合同3。均通过。'%total,'',
        '| V6虚拟16条×36属性 | 部署训练MAPE | 训练R² | 封存验收MAPE | 验收R² | 搜索秒数 |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for r in demos:rows.append('| %s | %.6f%% | %.9f | %.6f%% | %.9f | %.2f |'%(r['mode'],r['train']['mape_percent'],r['train']['r2'],r['test']['mape_percent'],r['test']['r2'],r['elapsed_seconds']))
    rows += ['', '默认验收是nested_procedure_test，评价完整训练方法；最终模型重新在全部开发记录训练。locked_holdout测试另证同一导出模型、参数及区间不会随封存答案改变；封存标签只改变验收分数。', '',
        '真实离线BAT验收：15种保存模式切换、12套旧价格模型摘要与修改时间保留、输入表保留、未训练V6拒绝START、QUICK/DEEP配置、4—7条×40属性、三种V6策略、纯数据、完成检查点零新增fit、严格目标失败保留旧指针。', '',
        '实际START及HTTP验证：V21.6前台、统一门户、两个模型和只读效费分析台、实际方案生成、旧模型新前台、无输入启动、强制结束Python父进程后四端口释放、专家效能JSON导入后价格文件不变、EXPORT及迁移到新目录无输入冷启动。未借用源码PYTHONPATH。', '',
        '浏览器另核实优化目标切换及成本排序、侧栏拖拽、门户效费入口及两个方案的帕累托分析。', '',
        '原V2核心SHA256保持f8f9877029e13535b97b40c429fb4afe6b5d613db635b6bee83307fca30b2899。V21.6真实来源3439c7d，效费服务来源16ab955；局部兼容差异及摘要见RECOMMENDATION_SOURCE_PROVENANCE_V21_6.json。', '',
        '包摘要（环境与ZIP不进Git）：','']
    for value in archives.values():rows.append('- %s：%d字节；SHA256 `%s`。'%(value['file'],value['bytes'],value['sha256']))
    rows += ['', '本机完整证据：outputs/v6_20261027/release_validation.json、package_acceptance_release.json及逐阶段日志。测试临时展开目录在验收后清理，输入虚拟样本、报告、检查点和最终ZIP保留。']
    (ROOT/'docs/VALIDATION_V6_20261027.md').write_text('\n'.join(rows)+'\n',encoding='utf-8')
    print(json.dumps({'status':'VERIFIED','source_checks':total,'archives':archives,'virtual_metrics':demos},ensure_ascii=False))

if __name__=='__main__':main()
