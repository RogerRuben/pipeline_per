/* R6: original-view coverage over the existing HTTP-only gateway. */
(function(){'use strict';const D=window.DeepPrice,Q=D.q,E=D.esc;let meta=null,displaySource='current';
const fmt=v=>v==null?'不可用':Number(v).toFixed(4);
function table(head,rows){return '<table><thead><tr>'+head.map(x=>'<th>'+E(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+E(x==null?'不可用':x)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
function view(){let hash=location.hash||'#design';if(hash==='#scenario'){hash='#sensitivity';history.replaceState(null,'',hash);}const shown=hash==='#factors'?['factors']:hash==='#model'?['model']:hash==='#sensitivity'?['sensitivity']:['design','mechanism'];['design','sensitivity','factors','model','mechanism'].forEach(id=>{if(Q(id))Q(id).hidden=!shown.includes(id);});document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash===hash));Object.values(D.charts).forEach(c=>c.resize());}
const mechanism=document.createElement('section');mechanism.id='mechanism';mechanism.className='panel mechanism-wide';mechanism.innerHTML='<h2>当前样本机理分析</h2><p class="notice">机理分析暂未开放。</p><textarea disabled placeholder="暂无机理分析结果"></textarea><button disabled>生成机理分析</button> <button disabled>下载机理 TXT</button>';Q('design').after(mechanism);
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();history.pushState(null,'',a.hash);view();window.scrollTo(0,0);}));
window.addEventListener('hashchange',view);window.addEventListener('popstate',()=>{view();window.scrollTo(0,0);});view();
const nativeFill=D.fill;D.fill=values=>{nativeFill(values);if(!D.schema)return;D.schema.fields.forEach(f=>{const e=Q('field_'+f.field_id),state=e.parentElement.querySelector('.availability');e.disabled=false;if(state)state.value=e.value===''?'missing':'present';delete e.dataset.priorValue;});};
const dock=document.createElement('section');dock.className='panel';dock.id='exportDock';dock.innerHTML='<h2>会话结果导出</h2><p>请选择需要导出的计算结果，结果保留 30 分钟。</p>';dock.appendChild(Q('exportResult').closest('.toolbar'));document.querySelector('main').insertBefore(dock,document.querySelector('main footer'));
const oldClear=D.clear;D.clear=()=>{if(Q('dsLayers'))Q('dsLayers').remove();oldClear();if(!D.schema){meta=null;Q('modelLibrary').textContent='正在刷新模型版本…';Q('importanceProvenance').textContent='正在刷新模型版本…';Q('importanceTable').textContent='';['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});}};
function importance(){
 let value=meta&&meta.data&&meta.data.feature_importance;
 if(displaySource==='expert'){
  const technical=Q('importanceTechnical');if(technical)technical.remove();const scores=(meta&&meta.data.expert_scores)||{};let rows=Array.isArray(scores)?scores:Object.keys(scores).map(k=>({field_id:k,importance:scores[k]}));
  Q('importanceProvenance').textContent=rows.length?'训练使用的专家评分（1–5分）。':'暂无专家评分。';
  Q('importanceTable').innerHTML=table(['属性','专家评分','来源'],rows.map(r=>[r.label_zh||r.label||r.field_id,r.specified===false?'未评分（默认 '+(r.score==null?'—':r.score)+'）':(r.score==null?r.importance:r.score),r.specified===false?'默认值':'工作簿 / 配置']));
  ['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});return;
 }
 if(displaySource==='legacy')value=D.legacyImportance;
 if(!value||!Array.isArray(value.items)||!value.items.length){Q('importanceProvenance').textContent='暂无属性重要度数据。';Q('importanceTable').textContent='未提供';['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});return;}
 const all=value.items,rows=all.slice(0,10);
 Q('importanceProvenance').textContent=displaySource==='legacy'?'历史演示数据':'当前模型的属性影响程度。';
 const provenance=Q('importanceTechnical')||document.createElement('details');provenance.id='importanceTechnical';provenance.className='technical-details';provenance.innerHTML='<summary>分析详情</summary><p>'+E(value.note||'')+'</p><p>'+E(value.scoring||value.kind)+' · '+E(value.scope||'')+'</p><p>模型 SHA '+E(D.schema.price_model_sha256)+'</p>';Q('importanceProvenance').after(provenance);
 D.chart('importanceChart',{tooltip:{trigger:'axis'},grid:{left:120,right:30,bottom:40},xAxis:{type:'value',name:value.unit||'原始重要度'},yAxis:{type:'category',inverse:true,data:rows.map(r=>r.label_zh||r.field_id)},series:[{type:'bar',data:rows.map(r=>r.importance)}]});
 Q('importanceTable').innerHTML=table(['排名','属性','原始值','标准差','正值占比'],rows.map(r=>[r.rank,r.label_zh||r.field_id,fmt(r.importance),fmt(r.std),((r.ratio||0)*100).toFixed(2)+'%']));
 D.chart('contributionChart',{tooltip:{trigger:'axis'},xAxis:{type:'category',data:rows.map(r=>r.label_zh||r.field_id),axisLabel:{rotate:25}},yAxis:{type:'value',min:0,max:100,name:'全字段正值累计占比 %'},series:[{type:'line',data:rows.map(r=>100*r.cumulative_ratio)}]});
}
Q('importanceSource').add(new Option('历史演示','legacy'));fetch('/deep_price/legacy_importance.json').then(r=>r.json()).then(r=>{D.legacyImportance=r;if(displaySource==='legacy')importance();});
Q('importanceSource').onchange=()=>{displaySource=Q('importanceSource').value;importance();};
function renderLibrary(library){
 Q('modelLibraryNote').innerHTML='<a href="/model-library" target="_blank" rel="noopener">打开模型库，接入或切换模型</a>';
 Q('modelLibrary').innerHTML=library.items.map(item=>{
  const m=item.metadata||{},metrics=m.metrics||{},labels={selected_training_metrics:'所选模型训练集',validation_metrics:'开发验证集',full_refit_training_metrics:'全数据重拟合训练集',deployed_training_metrics:'部署模型训练集',test_metrics:'独立测试集'};
  let html='<article class="model-card"><span class="active-badge">'+(item.active?'正在使用':'历史版本')+'</span><h3>'+E(item.product_code)+' · 价格模型</h3>';
  Object.keys(labels).forEach(k=>{const v=metrics[k];if(!v)return;const u=(m.metric_units||{})[k]||{};let mape=v.mape_percent!=null?v.mape_percent:(v.mape==null?null:(u.mape==='ratio'?v.mape*100:v.mape));html+='<h4>'+E(labels[k])+'</h4>'+table(['R²','平均百分比误差'],[[fmt(v.r2),mape==null?'未提供':Number(mape).toFixed(2)+'%']]);});
  html+='<details class="technical-details"><summary>模型详情</summary><p>版本 '+E(item.model_version)+'</p><p>模型 SHA '+E(item.price_model_sha256)+'</p><p>'+E(metrics.validation_protocol||metrics.evaluation_scope||'')+'</p><h4>模型成员与权重</h4>'+table(['成员','权重'],(m.active_members||[]).map(r=>[r.name,(r.weight*100).toFixed(2)+'%']))+'<p>训练样本 '+E(metrics.training_rows)+' · 属性 '+E(metrics.feature_count)+'</p><p>报告 SHA '+E(m.training_report_sha256||'未提供')+'</p></details></article>';return html;
 }).join('');
}
function samplePayload(){return Object.assign(D.identity(),{parameters:D.parameters(),range_mode:Q('rangeMode').value});}
function sensitivity(result,ranking,remember=true){
 if(!result.series.length)throw new Error('没有可用的分析属性');
 D.lastSensitivity={result,ranking};const top=result.series.length>1,relative=top&&D.sensitivityMode==='relative';
 const weighted=top&&Q('sensitivityWeight')&&Q('sensitivityWeight').value==='expert';
 const experts=meta&&meta.data.expert_scores||[],scoreFor=id=>{const row=Array.isArray(experts)?experts.find(s=>s.field_id===id&&s.specified!==false):null;return row&&row.score!=null?2+(Number(row.score)-1)*.5:1;};
 const smooth=(!Q('curveStyle')||Q('curveStyle').value==='smooth');
 Q('sensitivityNote').textContent=ranking+' · '+(smooth?'平滑曲线':'折线与采样点')+(weighted?' · 专家评分加权（辅助指标）':' · 模型价格变化');
 D.chart('sensitivityChart',{tooltip:{trigger:'axis'},legend:{type:'scroll',bottom:0},grid:{left:70,right:35,bottom:80},
  xAxis:{type:relative||(!top&&result.series[0].points.every(p=>typeof p.value==='number'))?'value':'category',name:relative?'参数相对当前值变化率（%）':top?'独立采样点（实际取值见表）':'属性实际取值',data:relative?undefined:top?Array.from({length:Math.max(...result.series.map(s=>s.points.length))},(_,i)=>i+1):result.series[0].points.map(p=>p.value)},
  yAxis:{type:'value',name:top?(weighted?'专家加权变化率（%）':'价格变化率（%）'):'价格（万元）',scale:true,splitLine:{lineStyle:{type:'dashed'}}},
  series:result.series.map(s=>{const base=Number(D.parameters()[s.field_id]);return {id:s.field_id,name:s.label_zh,type:'line',smooth:smooth&&s.points.every(p=>typeof p.value==='number'),symbol:smooth?'none':'circle',showAllSymbol:true,symbolSize:smooth?0:6,lineStyle:{width:smooth?3:1.5},data:s.points.map(p=>relative?[(p.value-base)/Math.abs(base)*100,p.change_rate_pct*(weighted?scoreFor(s.field_id):1)]:!top&&typeof p.value==='number'?[p.value,p.price_wan]:top?p.change_rate_pct*(weighted?scoreFor(s.field_id):1):p.price_wan)};})});
 Q('sensitivityTable').innerHTML=table(['属性','实际采样值','价格 / 万元','真实变化率 %'],result.series.flatMap(s=>s.points.map(p=>[s.label_zh,p.value,fmt(p.price_wan),fmt(p.change_rate_pct)])));
 if(remember)D.remember(ranking,result);D.status('敏感性分析完成。','success');
}
document.addEventListener('deep:ready',()=>{Q('single').disabled=D.busy||!Q('sensitivityField').options.length;Q('top10').disabled=D.busy||!Q('sensitivityField').options.length;Q('importanceTop10').disabled=D.busy||!(meta&&meta.data.feature_importance&&meta.data.feature_importance.items);});
Q('rangeMode').onchange=()=>{D.lastSensitivity=null;D.epoch++;Q('sensitivityNote').textContent='分析范围已改变，请重新计算';Q('sensitivityTable').textContent='';if(D.charts.sensitivityChart)D.charts.sensitivityChart.clear();};
Q('single').onclick=()=>D.run(async current=>{D.sensitivityMode='single';let r=await D.api('/sensitivity/single',Object.assign(samplePayload(),{field_id:Q('sensitivityField').value}));if(current())sensitivity(r,'单参数敏感性');});
Q('top10').onclick=()=>D.run(async current=>{D.sensitivityMode='relative';const numeric=D.schema.fields.filter(f=>['number','integer'].includes(f.type)&&f.price_model_input&&Number(D.parameters()[f.field_id])!==0).map(f=>f.field_id);if(!numeric.length)throw new Error('没有非零数值属性，请使用单参数或全属性分析');let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:numeric}));if(current())sensitivity(r,'数值属性 Top10');});
Q('importanceTop10').onclick=()=>D.run(async current=>{let data=meta&&meta.data.feature_importance;if(!data||!data.items)throw new Error('暂无属性重要度数据');let keys=data.items.map(r=>r.field_id).filter(k=>D.schema.fields.some(f=>f.field_id===k&&f.analysis_supported)).slice(0,10);if(!keys.length)throw new Error('重要度字段与当前可分析字段不匹配');let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:keys,ranking_mode:'importance'}));if(current())sensitivity(r,'重要属性 Top10');});
document.addEventListener('deep:loaded',async()=>{
 const version=D.schema;Q('modelSummary').hidden=true;Q('importance').hidden=true;meta=null;Q('modelLibrary').textContent='读取当前发布模型…';Q('importanceProvenance').textContent='读取中…';
 Q('analysisAvailability').textContent='仅显示支持分析的属性。';
 const oldExtra=Q('r6Adjustment'),adjustment=Q('adjustment');if(oldExtra)oldExtra.remove();Q('fields').hidden=false;
 const group=Q('groups'),first=Q('design').querySelector('article'),extra=document.createElement('div');extra.className='r6-extra';extra.hidden=true;extra.id='r6Adjustment';extra.appendChild(adjustment);first.appendChild(extra);
 const button=document.createElement('button');button.type='button';button.textContent='时间批量校正';button.onclick=()=>{Q('fields').hidden=true;extra.hidden=false;group.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b===button));};group.appendChild(button);['功能参数','性能参数','环境参数'].reverse().forEach(name=>{if(!Array.from(group.querySelectorAll('button')).some(b=>b.textContent===name)){const b=document.createElement('button');b.textContent=name;b.disabled=true;b.title='当前发布产品未配置该分组';group.prepend(b);}});
 group.querySelectorAll('button').forEach(b=>{if(b!==button)b.addEventListener('click',()=>{Q('fields').hidden=false;extra.hidden=true;});});
 D.schema.fields.forEach(f=>{
 const input=Q('field_'+f.field_id),holder=input.parentElement;
 const state=document.createElement('select');state.className='availability';state.setAttribute('aria-label',f.label_zh+'可用状态');state.add(new Option('已填写','present'));state.add(new Option('未提供','missing'));
 const unavailable=new Option('不可用','unavailable');unavailable.disabled=true;state.add(unavailable);state.value=input.value===''?'missing':'present';
 state.onchange=()=>{if(state.value==='missing'){input.dataset.priorValue=input.value;input.value='';input.disabled=true;}else{input.disabled=false;input.value=input.dataset.priorValue||input.value;}input.dispatchEvent(new Event('input'));};
 holder.appendChild(state);
 });
 try{const [importanceData,library]=await Promise.all([D.api('/feature-importance'),D.api('/model-library')]);if(version!==D.schema)return;meta=importanceData;renderLibrary(library);importance();}catch(e){if(version===D.schema)D.status(e.message,'error');}
 document.dispatchEvent(new CustomEvent('deep:ready'));view();
});

document.addEventListener('deep:prediction',()=>{const old=Q('dsLayers');if(old)old.remove();const value=D.prediction&&D.prediction.ds_pricing;if(!value)return;const section=document.createElement('div');section.id='dsLayers';section.className='result-note';section.innerHTML='<h3>三层价格明细 · 元</h3>'+table(['部件价格','第二层属性','第三层综合金额','总价'],[[fmt(value.layers.stage1),fmt(value.layers.stage2),fmt(value.layers.stage3),fmt(value.total_price)]])+'<p>'+E(value.status)+'</p>'+value.warnings.map(w=>'<p>'+E(w)+'</p>').join('');Q('comparison').parentElement.after(section);});

const styleBar=document.createElement('div');styleBar.className='toolbar';styleBar.innerHTML='<label>曲线样式 <select id="curveStyle"><option value="smooth">平滑曲线</option><option value="line">折线与采样点</option></select></label><label>对比口径 <select id="sensitivityWeight"><option value="price">模型价格变化</option><option value="expert">专家加权对比</option></select></label><button id="allAttributes" class="secondary">全属性采样对比</button>';Q('sensitivityNote').before(styleBar);
['curveStyle','sensitivityWeight'].forEach(id=>Q(id).onchange=()=>{if(D.lastSensitivity)sensitivity(D.lastSensitivity.result,D.lastSensitivity.ranking,false);else D.status('样式已选择，请先运行敏感性分析。');});
Q('sensitivityField').onchange=()=>{D.lastSensitivity=null;D.epoch++;Q('sensitivityTable').textContent='';Q('sensitivityNote').textContent='分析属性已变化，请重新计算';if(D.charts.sensitivityChart)D.charts.sensitivityChart.clear();};
Q('allAttributes').onclick=()=>D.run(async current=>{D.sensitivityMode='all';const r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:null}));if(current())sensitivity(r,'全属性对比');});
document.addEventListener('deep:labels',event=>{const fields=event.detail;if(meta&&meta.data){for(const rows of [meta.data.expert_scores,(meta.data.feature_importance||{}).items])if(Array.isArray(rows))rows.forEach(r=>{if(fields[r.field_id]){r.label=fields[r.field_id].label;r.label_zh=fields[r.field_id].label;}});importance();}if(D.lastSensitivity){D.lastSensitivity.result.series.forEach(s=>{if(fields[s.field_id])s.label_zh=fields[s.field_id].label;});sensitivity(D.lastSensitivity.result,D.lastSensitivity.ranking,false);}});
})();
