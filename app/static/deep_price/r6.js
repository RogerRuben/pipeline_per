/* R6: original-view coverage over the existing HTTP-only gateway. */
(function(){'use strict';const D=window.DeepPrice,Q=D.q,E=D.esc;let meta=null,plans=[],serial=0,displaySource='current',analysisPage='sensitivity';
const fmt=v=>v==null?'不可用':Number(v).toFixed(4);
function table(head,rows){return '<table><thead><tr>'+head.map(x=>'<th>'+E(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+E(x==null?'不可用':x)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
function view(){const hash=location.hash||'#design';if(hash==='#sensitivity')analysisPage='sensitivity';if(hash==='#scenario')analysisPage='scenario';const shown=hash==='#factors'?['factors']:hash==='#model'?['model']:hash==='#scenario'||hash==='#sensitivity'?[analysisPage]:['design','mechanism'];
Q('analysisModes').hidden=!['#scenario','#sensitivity'].includes(hash);Q('sensitivityMode').classList.toggle('active',analysisPage==='sensitivity');Q('scenarioMode').classList.toggle('active',analysisPage==='scenario');
['design','sensitivity','scenario','factors','model','mechanism'].forEach(id=>{if(Q(id))Q(id).hidden=!shown.includes(id);});document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash===hash||(a.hash==='#scenario'&&hash==='#sensitivity')));Object.values(D.charts).forEach(c=>c.resize());}
const mechanism=document.createElement('section');mechanism.id='mechanism';mechanism.className='panel mechanism-wide';mechanism.innerHTML='<h2>当前样本机理分析</h2><p class="notice">机理分析暂未开放。</p><textarea disabled placeholder="暂无机理分析结果"></textarea><button disabled>生成机理分析</button> <button disabled>下载机理 TXT</button>';Q('design').after(mechanism);
const modes=document.createElement('div');modes.id='analysisModes';modes.className='panel tabs';modes.innerHTML='<button id=sensitivityMode type=button>敏感性曲线</button><button id=scenarioMode type=button>情景方案</button>';Q('sensitivity').before(modes);Q('sensitivityMode').onclick=()=>{analysisPage='sensitivity';history.replaceState(null,'','#sensitivity');view();};Q('scenarioMode').onclick=()=>{analysisPage='scenario';history.replaceState(null,'','#scenario');view();};
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();const target=a.hash==='#scenario'?(analysisPage==='sensitivity'?'#sensitivity':'#scenario'):a.hash;if(location.hash!==target)history.pushState(null,'',target);view();window.scrollTo(0,0);}));
window.addEventListener('hashchange',view);window.addEventListener('popstate',()=>{view();window.scrollTo(0,0);});view();
const nativeFill=D.fill;D.fill=values=>{nativeFill(values);if(!D.schema)return;D.schema.fields.forEach(f=>{const e=Q('field_'+f.field_id),state=e.parentElement.querySelector('.availability');e.disabled=false;if(state)state.value=e.value===''?'missing':'present';delete e.dataset.priorValue;});};
const dock=document.createElement('section');dock.className='panel';dock.id='exportDock';dock.innerHTML='<h2>会话结果导出</h2><p>请选择需要导出的计算结果，结果保留 30 分钟。</p>';dock.appendChild(Q('exportResult').closest('.toolbar'));document.querySelector('main').insertBefore(dock,document.querySelector('main footer'));
const oldClear=D.clear;D.clear=()=>{if(Q('dsLayers'))Q('dsLayers').remove();oldClear();if(!D.schema){meta=null;Q('modelLibrary').textContent='正在刷新模型版本…';Q('importanceProvenance').textContent='正在刷新模型版本…';Q('importanceTable').textContent='';['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});}['scenarioBubble','importanceChart','contributionChart'].forEach(id=>{if(id==='scenarioBubble'&&D.charts[id])D.charts[id].clear();});Q('scenarioDiff').textContent='';Q('scenarioTop5').textContent='';};
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
 Q('sensitivityNote').textContent=ranking+(weighted?' · 专家评分加权（辅助指标）':' · 模型价格变化');
 D.chart('sensitivityChart',{tooltip:{trigger:'axis'},legend:{type:'scroll',bottom:0},grid:{left:70,right:35,bottom:80},
  xAxis:{type:relative||(!top&&result.series[0].points.every(p=>typeof p.value==='number'))?'value':'category',name:relative?'参数相对当前值变化率（%）':top?'独立采样点（实际取值见表）':'属性实际取值',data:relative?undefined:top?Array.from({length:Math.max(...result.series.map(s=>s.points.length))},(_,i)=>i+1):result.series[0].points.map(p=>p.value)},
  yAxis:{type:'value',name:top?(weighted?'专家加权变化率（%）':'价格变化率（%）'):'价格（万元）',scale:true,splitLine:{lineStyle:{type:'dashed'}}},
  series:result.series.map(s=>{const base=Number(D.parameters()[s.field_id]);return {id:s.field_id,name:s.label_zh,type:'line',smooth:smooth&&s.points.every(p=>typeof p.value==='number'),symbol:'circle',symbolSize:6,data:s.points.map(p=>relative?[(p.value-base)/Math.abs(base)*100,p.change_rate_pct*(weighted?scoreFor(s.field_id):1)]:!top&&typeof p.value==='number'?[p.value,p.price_wan]:top?p.change_rate_pct*(weighted?scoreFor(s.field_id):1):p.price_wan)};})});
 Q('sensitivityTable').innerHTML=table(['属性','实际采样值','价格 / 万元','真实变化率 %'],result.series.flatMap(s=>s.points.map(p=>[s.label_zh,p.value,fmt(p.price_wan),fmt(p.change_rate_pct)])));
 if(remember)D.remember(ranking,result);D.status('敏感性分析完成。','success');
}
document.addEventListener('deep:ready',()=>{Q('single').disabled=D.busy||!Q('sensitivityField').options.length;Q('top10').disabled=D.busy||!Q('sensitivityField').options.length;Q('importanceTop10').disabled=D.busy||!(meta&&meta.data.feature_importance&&meta.data.feature_importance.items);});
Q('rangeMode').onchange=()=>{D.epoch++;Q('sensitivityNote').textContent='分析范围已改变，请重新计算';Q('sensitivityTable').textContent='';if(D.charts.sensitivityChart)D.charts.sensitivityChart.clear();};
Q('single').onclick=()=>D.run(async current=>{D.sensitivityMode='single';let r=await D.api('/sensitivity/single',Object.assign(samplePayload(),{field_id:Q('sensitivityField').value}));if(current())sensitivity(r,'单参数敏感性');});
Q('top10').onclick=()=>D.run(async current=>{D.sensitivityMode='relative';const numeric=D.schema.fields.filter(f=>['number','integer'].includes(f.type)&&f.price_model_input&&Number(D.parameters()[f.field_id])!==0).map(f=>f.field_id);if(!numeric.length)throw new Error('没有非零数值属性，请使用单参数或全属性分析');let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:numeric}));if(current())sensitivity(r,'数值属性 Top10');});
Q('importanceTop10').onclick=()=>D.run(async current=>{let data=meta&&meta.data.feature_importance;if(!data||!data.items)throw new Error('暂无属性重要度数据');let keys=data.items.map(r=>r.field_id).filter(k=>D.schema.fields.some(f=>f.field_id===k&&f.analysis_supported)).slice(0,10);if(!keys.length)throw new Error('重要度字段与当前可分析字段不匹配');let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:keys,ranking_mode:'importance'}));if(current())sensitivity(r,'重要属性 Top10');});
function scenarioCard(){
 if(!D.schema)return;D.epoch++;const base=D.parameters(),item={id:'r6-'+(++serial),base,inputs:[],picked:new Set()};plans.push(item);
 const card=document.createElement('div');card.className='scenario-card';
 const name=document.createElement('input');name.value='方案 '+serial;name.setAttribute('aria-label','情景名称');item.name=name;name.oninput=()=>D.clear();
 const toolbar=document.createElement('div');toolbar.className='toolbar';toolbar.appendChild(name);
 const picker=document.createElement('select');picker.className='field-picker';picker.setAttribute('aria-label','选择要修改的属性');D.schema.fields.forEach(f=>picker.add(new Option(f.label_zh,f.field_id)));toolbar.appendChild(picker);
 const add=document.createElement('button');add.textContent='添加改动字段';add.className='secondary';toolbar.appendChild(add);
 const remove=document.createElement('button');remove.textContent='移除方案';remove.className='secondary';remove.onclick=()=>{D.clear();plans=plans.filter(p=>p!==item);card.remove();};toolbar.appendChild(remove);card.appendChild(toolbar);
 const grid=document.createElement('div');grid.className='scenario-params';
 D.schema.fields.forEach(f=>{const label=document.createElement('label');label.textContent=f.label_zh;const input=Q('field_'+f.field_id).cloneNode(true);input.id=item.id+'_'+f.field_id;input.disabled=false;input.value=Q('field_'+f.field_id).value;input.oninput=input.onchange=()=>{D.clear();};label.appendChild(input);grid.appendChild(label);item.inputs.push({field:f,input,label});});
 item.update=()=>item.inputs.forEach(r=>r.label.hidden=!Q('scenarioAdvanced').checked&&!item.picked.has(r.field.field_id));
 add.onclick=()=>{item.picked.add(picker.value);item.update();D.clear();};item.update();card.appendChild(grid);Q('scenarioRows').appendChild(card);
}
Q('addScenario').onclick=scenarioCard;Q('scenarioAdvanced').onchange=()=>plans.forEach(p=>p.update());
Q('runScenarios').onclick=()=>D.run(async current=>{
 if(!plans.length)throw new Error('请先添加情景并选择要修改的字段');
 const base=D.parameters(),identity=D.identity();
 const variants=plans.map(p=>{let parameters={};p.inputs.forEach(({field:f,input:e})=>parameters[f.field_id]=e.value===''?null:e.tagName==='SELECT'?JSON.parse(e.value):['number','integer'].includes(f.type)?Number(e.value):e.value);return {item_id:p.id,name:p.name.value,parameters};});
 const result=await D.api('/scenario',Object.assign({},identity,{items:[{item_id:'baseline',name:'当前属性基准',parameters:base},...variants]}));
 if(!current())return;
 const baseline=result.items[0].result.predicted_price_wan,changes=[];
 variants.forEach(v=>Object.keys(base).forEach(key=>{if(JSON.stringify(v.parameters[key])!==JSON.stringify(base[key]))changes.push({variant:v,key,old:base[key],value:v.parameters[key]});}));
 if(changes.length>1000)throw new Error('单次情景改动超过1000项，请缩小范围');
 const effects=[];
 for(let start=0;start<changes.length;start+=100){
 const chunk=changes.slice(start,start+100),r=await D.api('/predict/batch',Object.assign({},identity,{items:chunk.map((c,i)=>({item_id:'impact-'+(start+i),parameters:Object.assign({},base,{[c.key]:c.value})}))}));
 if(!current())return;chunk.forEach((c,i)=>effects.push(Object.assign({},c,{delta:r.items[i].result.predicted_price_wan-baseline})));
 }
 if(!current())return;
 Q('scenarioDiff').innerHTML='<p class="scenario-delta">基准价格：'+fmt(baseline)+' 万元</p>'+table(['方案','原价 / 万元','新价 / 万元','差额 / 万元','变化率'],result.items.slice(1).map((r,i)=>[variants[i].name,fmt(baseline),fmt(r.result.predicted_price_wan),fmt(r.result.predicted_price_wan-baseline),baseline?((r.result.predicted_price_wan-baseline)/baseline*100).toFixed(2)+'%':'基准为0，不计算']));
 Q('scenarioTable').innerHTML=table(['方案','改动字段','旧值','新值'],changes.map(c=>[c.variant.name,(D.schema.fields.find(f=>f.field_id===c.key)||{}).label_zh||c.key,c.old,c.value]));
 effects.sort((a,b)=>Math.abs(b.delta)-Math.abs(a.delta));
 Q('scenarioTop5').innerHTML='<h3>本次情景改动的单字段影响 Top5</h3><p>只比较这次情景里实际改过的字段，各自单独算出的价格差额。这不是全字段敏感性排名，也不能把这几项加起来当作方案总价。</p>'+table(['方案','字段','单字段价格差额 / 万元'],effects.slice(0,5).map(c=>[c.variant.name,(D.schema.fields.find(f=>f.field_id===c.key)||{}).label_zh||c.key,fmt(c.delta)]));
 D.chart('scenarioChart',{tooltip:{trigger:'axis'},xAxis:{type:'category',data:variants.map(v=>v.name)},yAxis:{type:'value',name:'价格 / 万元'},series:[{type:'bar',data:result.items.slice(1).map(r=>r.result.predicted_price_wan)}]});
 D.chart('scenarioBubble',{tooltip:{formatter:p=>E(p.data[3])+' · '+E(p.data[4])+'：'+fmt(p.data[1])+' 万元'},xAxis:{type:'value',name:'改动项序号'},yAxis:{type:'value',name:'单字段价格差额 / 万元'},series:[{type:'scatter',symbolSize:v=>Math.min(65,12+Math.sqrt(Math.abs(v[1]))*20),data:effects.map((c,i)=>[i+1,c.delta,Math.abs(c.delta),c.variant.name,(D.schema.fields.find(f=>f.field_id===c.key)||{}).label_zh||c.key])}]});
 D.remember('多属性情景差额与明细',result);D.status('情景比较完成。','success');
});
document.addEventListener('deep:product',()=>{plans=[];Q('scenarioRows').textContent='';});
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

const styleBar=document.createElement('div');styleBar.className='toolbar';styleBar.innerHTML='<label>曲线样式 <select id="curveStyle"><option value="smooth">原版平滑曲线</option><option value="line">原始折线</option></select></label><label>对比口径 <select id="sensitivityWeight"><option value="price">模型价格变化</option><option value="expert">原版专家加权对比</option></select></label><button id="allAttributes" class="secondary">全属性采样对比</button>';Q('sensitivityNote').before(styleBar);
['curveStyle','sensitivityWeight'].forEach(id=>Q(id).onchange=()=>{if(D.lastSensitivity)sensitivity(D.lastSensitivity.result,D.lastSensitivity.ranking,false);});
Q('allAttributes').onclick=()=>D.run(async current=>{D.sensitivityMode='all';const r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:null}));if(current())sensitivity(r,'全属性对比');});
document.addEventListener('deep:labels',event=>{const fields=event.detail;if(meta&&meta.data){for(const rows of [meta.data.expert_scores,(meta.data.feature_importance||{}).items])if(Array.isArray(rows))rows.forEach(r=>{if(fields[r.field_id]){r.label=fields[r.field_id].label;r.label_zh=fields[r.field_id].label;}});importance();}if(D.lastSensitivity){D.lastSensitivity.result.series.forEach(s=>{if(fields[s.field_id])s.label_zh=fields[s.field_id].label;});sensitivity(D.lastSensitivity.result,D.lastSensitivity.ranking,false);}});
})();
