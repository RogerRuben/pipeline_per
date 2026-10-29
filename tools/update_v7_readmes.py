# -*- coding: utf-8 -*-
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=ROOT/'README.md';s=p.read_text(encoding='utf-8')
    s=s.replace('**推荐系统 V21.6 + AutoML V6**，更新于 **2026-10-27**','**推荐系统 V21.6 + AutoML V7 / V3+**，更新于 **2026-10-30**')
    begin=s.index('当前交付及操作见');end=s.index('\n',begin)
    s=s[:begin]+'当前交付及操作见[V7/V3+使用说明](docs/OPTUNA_V7_PIPELINE.md)。V7保留原V3联合搜索及模型对照，只对少量候选做共同开发复核、训练种子平均和收缩融合；专家1—5分五档错分率可选，默认关闭。所有开发分数参与选择，不称为独立测试。V2/V3/V4/V5/V5.1/V6继续保留切换。'+s[end:]
    s=s.replace('V2—V6','V2—V7').replace('新入口默认使用V6。','新入口默认使用V7/V3+。').replace('`--price-engine optuna_v2`到`optuna_v6`','`--price-engine optuna_v2`到`optuna_v7`')
    s=s.replace('V6新的可学习虚拟样本为`automl_pipeline/examples/virtual_v6_16x36.xlsx`','V7含噪声、无关属性及相关属性的虚拟样本为`automl_pipeline/examples/virtual_v7_16x36.xlsx`')
    s=s.replace('| **V6（默认）** |','| V6 |')
    line='| **V7 / V3+（默认）** | 原V3对照保留，少量候选共同开发复核、训练种子平均、受约束融合；可选专家五档错分率≤10%，默认关闭 |\n'
    if line not in s:s=s.replace('\nV4原混合复核', '\n'+line+'\nV4原混合复核',1)
    s=s.replace('ProductAutoML_V6_Offline_20261027.zip','ProductAutoML_V7_Offline_20261030.zip').replace('ProductAutoML_V6_PATCH_20261027.zip','ProductAutoML_V7_PATCH_20261030.zip').replace('`APPLY_V6.bat`','`APPLY_V7.bat`')
    s=s.replace('同一套环境及15个已保存模式','同一套环境及18个已保存模式').replace('`SELECT_V6.bat`、`V6_GOAL_FIRST.bat` / `V6_BALANCED.bat` / `V6_ROBUST.bat`','`SELECT_V7.bat`、`V7_BALANCED.bat` / `V7_GOAL_FIRST.bat` / `V7_BASELINE_ONLY.bat`')
    s=s.replace('`tools/build_product_v6.py`','`tools/build_product_v7.py`').replace('和[V6说明](docs/OPTUNA_V6_PIPELINE.md)为准','和[V7说明](docs/OPTUNA_V7_PIPELINE.md)为准')
    s=s.replace('V6本轮通过276项','V6历史交付通过276项')
    s=s.replace('python tools/verify_product_v6_tests.py --report outputs/verification/v6.json','python tools/verify_product_v6_tests.py --report outputs/verification/v6.json\npython tools/verify_product_v7_tests.py --report outputs/verification/v7.json')
    s=s.replace('virtual_16x36.xlsx --price-config outputs/my_first_product','virtual_v7_16x36.xlsx --price-config outputs/my_first_product')
    p.write_text(s,encoding='utf-8')
    p=ROOT/'automl_pipeline/README.md';s=p.read_text(encoding='utf-8');start=s.index('本目录是');end=s.index('\n',start)
    s=s[:start]+'本目录是V2—V7快速建模源码，默认V7/V3+：保留原V3六模型联合搜索，以少量候选复核、训练种子平均和收缩融合改善稳定性，专家五档错分率可选。推荐系统为V21.6；完整操作、指标边界和预算见[V7说明](../docs/OPTUNA_V7_PIPELINE.md)。'+s[end:]
    s=s.replace('默认V6','默认V7/V3+').replace('`tools/build_product_v6.py`与`upgrade_product_v6.py`','`tools/build_product_v7.py`与`upgrade_product_v7.py`').replace('V6完整包共用一个固定环境并包含15个已保存模式','V7完整包共用一个固定环境并包含18个已保存模式')
    s=s.replace('virtual_16x36.xlsx --price-config','virtual_v7_16x36.xlsx --price-config')
    begin=s.index('`config`的standard');end=s.index('\n',begin)
    s=s[:begin]+'`config`生成所选版本配置。V7标准为300个V3配置/900秒，quick为40/300秒，deep为5000/1800秒；新增复核另受实际fit及时间上限限制。旧版本使用自己的配置和预算。配置文件已存在时不覆盖，切换引擎须同时指定匹配的`--price-engine`。'+s[end:]
    begin=s.index('输入约定、固定CV');end=s.index('\n',begin)
    s=s[:begin]+'输入、原V3对照、共同开发评分、可选专家五档及预算见[V7说明](../docs/OPTUNA_V7_PIPELINE.md)。默认专家一致性关闭；report不改变选模，soft温和偏好，strict要求价格目标与五档错分率同时满足。'+s[end:]
    s=s.replace('相关`services/`和`tools/`','相关`services/`、`cost_effectiveness_analysis/`和`tools/`')
    p.write_text(s,encoding='utf-8')
    p=ROOT/'docs/CODEBASE_LAYOUT.md';s=p.read_text(encoding='utf-8');s=s.replace('2026-10-27更新至真实推荐系统V21.6与AutoML V6。','2026-10-30更新至推荐系统V21.6与AutoML V7/V3+。V7保留V3主线与原模型对照，增加小规模开发复核及可选五档一致性；')
    s=s.replace('V2—V6','V2—V7').replace('    optuna_v6/','    optuna_v7/             V3+候选复核、种子平均、收缩融合及实际模型五档检查\n    optuna_v6/',1)
    p.write_text(s,encoding='utf-8')
if __name__=='__main__':main()
