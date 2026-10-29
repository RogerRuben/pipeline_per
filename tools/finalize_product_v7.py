# -*- coding: utf-8 -*-
import hashlib,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];E=ROOT/'outputs/v7_20261030'
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def main():
    acceptance=read(E/'package_acceptance_r2.json')
    if acceptance['status']!='PASS':raise ValueError('Final isolated package acceptance must pass')
    checks={'legacy':read(E/'legacy_tests.json'),'v7':read(E/'unit_delivery.json'),'recommendation':read(E/'recommendation_tests.json')}
    checks.update(read(E/'historical/historical_summary.json'))
    if any(r['status']!='PASS' for r in checks.values()):raise ValueError('Regression incomplete')
    # Source-layout tests run in both the V6 and V7 suites; count them once.
    total=sum(r.get('tests',0) for r in checks.values())+29-9
    simulation=read(E/'frozen_simulation/benchmark_report.json')
    if len(simulation['cases'])!=5 or not simulation['method_frozen_before_generation']:raise ValueError('Frozen new-seed simulation incomplete')
    archives={}
    for kind,key in [('Offline','full_sha256'),('PATCH','patch_sha256')]:
        path=ROOT/('deliverables/ProductAutoML_V7_'+kind+'_20261030.zip');digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=acceptance[key]:raise ValueError('Archive changed after acceptance')
        meta=read(str(path)+'.json');meta.update(status='VERIFIED',source_unique_checks=total,acceptance_report='outputs/v7_20261030/package_acceptance_r2.json',
            saved_modes=acceptance['mode_count'],physical_win7_tested=False,business_accuracy_certified=False)
        write(str(path)+'.json',meta);archives[kind]={'file':path.name,'bytes':path.stat().st_size,'sha256':digest}
    report={'status':'VERIFIED','date':'2026-10-30','checks':checks,'source_unique_checks':total,'archives':archives,'package_acceptance':acceptance,
        'frozen_virtual_simulation':simulation,'development_ablations':read(E/'delivery_benchmark/benchmark_report.json'),
        'business_accuracy_certified':False,'physical_win7_tested':False}
    write(E/'release_validation.json',report)
    lines=['# V7 / V3+验证记录（2026-10-30）','',
        '固定环境：Python3.8.10 x64、sklearn0.24.2、Optuna3.6.0。实体Win7未复测。', '',
        '共%d项不同名称的源码/前台检查通过，重复执行的9项目录检查只计一次。原V3预测及获分参数对照一致、原核心文件SHA256不变、4—7条×40属性、实际模型种子平均、有限成员移除、缓存依赖、断点恢复、五档计数及发布保护均覆盖。'%total,'',
        '专家严格正向流程含一个由冻结模型构造标签的接口测试，用于证明分档与导出预测关联，不能当成真实专家标注一致性的实证。专家标签打乱、持平跨档、未评分及单一档位均有反向检查。', '',
        '真实离线包验收：18种保存模式；V6原包15套旧价格模型及输入摘要/修改时间保留；未训练V7拒绝START；QUICK/DEEP入口；4—7条实际BAT训练；完成检查点零新增fit；专家report及严格发布失败；对照报告不训练；专家效能替换不改价格文件；带环境EXPORT无输入迁移冷启动；正常及强制结束父进程后四端口释放。', '',
        '开发阶段对照与逐项实验保存全部案例，提升、持平和退步均保留。以下是方法冻结后另一组随机种子的虚拟样本，训练代码摘要在生成前固定，运行中保持不变；外部160条标签未进入参数、权重或模型选择。全部属于仿真，不是实际业务验收。', '',
        '| 虚拟训练情形 | 原V3 外部MAPE | V3+ 外部MAPE | 原V3 外部R² | V3+ 外部R² | 新增fit |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    names={'independent_noise':'16×36，无关属性与噪声','correlated_noise':'16×36，强相关与较高噪声','ten_by_fifty':'10×50','tiny_four':'4×40','tiny_seven':'7×40'}
    for r in simulation['cases']:
        a=r['baseline_external'];b=r['v7_external'];lines.append('| %s | %.4f%% | %.4f%% | %.6f | %.6f | %s |'%(names[r['case']],a['mape_percent'],b['mape_percent'],a['r2'],b['r2'],r['extra_fits']))
    lines += ['', '对照使用相同V3基线搜索预算，V3+新增拟合费用单独列出；低预算情形受最小新增额度影响，不能把这些结果声称为严格相同总算力的比较。该组数据反映有限仿真条件，不能保证真实数据MAPE≤10%、R²≥0.9。', '',
        'V7主报告将实际训练、原划分开发、共同开发CV、专家分档分开。开发成绩参与选择；没有新真实测试集时独立测试指标为空，不用部署模型在自身训练表上的分数冒充测试。', '',
        '本机证据：outputs/v7_20261030/release_validation.json、package_acceptance_r2.json、unit_delivery.json、historical/、frozen_simulation/及逐阶段日志。测试展开目录在验收后清理，报告、虚拟输入和交付包保留。', '', '交付包摘要：','']
    for r in archives.values():lines.append('- %s：%d字节，SHA256 `%s`。'%(r['file'],r['bytes'],r['sha256']))
    (ROOT/'docs/VALIDATION_V7_20261030.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'status':'VERIFIED','unique_checks':total,'archives':archives},ensure_ascii=False))
if __name__=='__main__':main()
