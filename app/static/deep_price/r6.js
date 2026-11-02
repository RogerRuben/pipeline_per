/* R6: original-view coverage over the existing HTTP-only gateway. */
(function(){'use strict';const D=window.DeepPrice,Q=D.q,E=D.esc;let meta=null,plans=[],serial=0,displaySource='current';
const fmt=v=>v==null?'不可用':Number(v).toFixed(4);
function table(head,rows){return '<table><thead><tr>'+head.map(x=>'<th>'+E(x)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(x=>'<td>'+E(x==null?'不可用':x)+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
function view(){const hash=location.hash||'#design',shown=hash==='#factors'?['factors']:hash==='#model'?['model']:hash==='#scenario'?['scenario','sensitivity']:hash==='#sensitivity'?['sensitivity']:['design','sensitivity','mechanism'];
['design','sensitivity','scenario','factors','model','mechanism'].forEach(id=>{if(Q(id))Q(id).hidden=!shown.includes(id);});document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.hash===hash));Object.values(D.charts).forEach(c=>c.resize());}
const mechanism=document.createElement('section');mechanism.id='mechanism';mechanism.className='panel mechanism-wide';mechanism.innerHTML='<h2>当前样本机理分析</h2><p class="notice">未配置机理解释服务。价格分析结果不等于机理或因果报告。</p><textarea disabled placeholder="机理分析服务接入后在此展示"></textarea><button disabled>生成机理分析</button> <button disabled>下载机理 TXT</button>';Q('design').after(mechanism);
window.addEventListener('hashchange',view);view();
const nativeFill=D.fill;D.fill=values=>{nativeFill(values);if(!D.schema)return;D.schema.fields.forEach(f=>{const e=Q('field_'+f.field_id),state=e.parentElement.querySelector('.availability');e.disabled=false;if(state)state.value=e.value===''?'missing':'present';delete e.dataset.priorValue;});};
const dock=document.createElement('section');dock.className='panel';dock.id='exportDock';dock.innerHTML='<h2>会话结果导出</h2><p>结果保留30分钟，价格与版本由服务器记录；机理报告未启用。</p>';dock.appendChild(Q('exportResult').closest('.toolbar'));document.querySelector('main').insertBefore(dock,document.querySelector('main footer'));
const oldClear=D.clear;D.clear=()=>{oldClear();if(!D.schema){meta=null;Q('modelLibrary').textContent='正在刷新模型版本…';Q('importanceProvenance').textContent='正在刷新模型版本…';Q('importanceTable').textContent='';['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});}['scenarioBubble','importanceChart','contributionChart'].forEach(id=>{if(id==='scenarioBubble'&&D.charts[id])D.charts[id].clear();});Q('scenarioDiff').textContent='';Q('scenarioTop5').textContent='';};
function importance(){
 let value=meta&&meta.data&&meta.data.feature_importance;
 if(displaySource==='expert'){
  const scores=(meta&&meta.data.expert_scores)||{};let rows=Array.isArray(scores)?scores:Object.keys(scores).map(k=>({field_id:k,importance:scores[k]}));
  Q('importanceProvenance').textContent=rows.length?'专家评分：人为先验指标，不能视为模型重要度、价格贡献或敏感性。':'当前模型未提供专家评分，不能用重要度或敏感性补造。';
  Q('importanceTable').innerHTML=table(['属性','专家评分'],rows.map(r=>[r.label_zh||r.field_id,r.importance]));
  ['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});return;
 }
 if(displaySource==='legacy')value=D.legacyImportance;
 if(!value||!Array.isArray(value.items)||!value.items.length){Q('importanceProvenance').textContent='当前数据源未提供经发布绑定的重要度；不使用旧模型数值替代。';Q('importanceTable').textContent='未提供';['importanceChart','contributionChart'].forEach(id=>{if(D.charts[id])D.charts[id].clear();});return;}
 const all=value.items,rows=all.slice(0,10);
 Q('importanceProvenance').textContent=(displaySource==='legacy'?'历史排序演示 legacy_demo；不解释当前模型。':'当前模型 SHA '+D.schema.price_model_sha256.slice(0,16)+'；')+(value.note||'')+(displaySource==='legacy'?' 来源：'+value.source_file+'；SHA '+value.source_sha256:'')+' 评分：'+(value.scoring||value.kind)+'；样本范围：'+(value.scope||'原演示模型')+'；样本数 '+(value.sample_ids||[]).length;
 D.chart('importanceChart',{tooltip:{trigger:'axis'},grid:{left:120,right:30,bottom:40},xAxis:{type:'value',name:value.unit||'原始重要度'},yAxis:{type:'category',inverse:true,data:rows.map(r=>r.label_zh||r.field_id)},series:[{type:'bar',data:rows.map(r=>r.importance)}]});
 Q('importanceTable').innerHTML=table(['排名','属性','原始值','标准差','正值占比'],rows.map(r=>[r.rank,r.label_zh||r.field_id,fmt(r.importance),fmt(r.std),((r.ratio||0)*100).toFixed(2)+'%']));
 D.chart('contributionChart',{tooltip:{trigger:'axis'},xAxis:{type:'category',data:rows.map(r=>r.label_zh||r.field_id),axisLabel:{rotate:25}},yAxis:{type:'value',min:0,max:100,name:'全字段正值累计占比 %'},series:[{type:'line',data:rows.map(r=>100*r.cumulative_ratio)}]});
}
Q('importanceSource').add(new Option('历史排序演示 legacy_demo（不解释当前模型）','legacy'));fetch('/deep_price/legacy_importance.json').then(r=>r.json()).then(r=>{D.legacyImportance=r;if(displaySource==='legacy')importance();});
Q('importanceSource').onchange=()=>{displaySource=Q('importanceSource').value;importance();};
function renderLibrary(library){
 Q('modelLibraryNote').textContent=library.note+' 模型安装与激活须经管理员受控发布，不接受本页上传 pickle。';
 Q('modelLibrary').innerHTML=library.items.map(item=>{
 const m=item.metadata||{},metrics=m.metrics||{};
 let html='<article class="model-card"><span class="active-badge">'+(item.active?'正在使用':'已发布历史版本')+'</span><h3>'+E(item.model_version)+'</h3><p>产品 '+E(item.product_code)+' · '+E(item.published_at||'发布时间未提供')+'</p><code>SHA '+E(item.price_model_sha256)+'</code><h4>实际活跃成员与融合权重</h4>'+table(['成员','权重'],(m.active_members||[]).map(r=>[r.name,(r.weight*100).toFixed(2)+'%']))+'<p>活跃成员数：'+(m.active_members||[]).length+'；训练样本 '+E(metrics.training_rows)+'；输入字段 '+E(metrics.feature_count)+'；试验 '+E(metrics.trials_completed)+'</p><p>搜索候选族：'+E((m.search_candidates||[]).join('、')||'未提供')+'</p>';
 Object.keys(metrics).filter(k=>metrics[k]&&typeof metrics[k]==='object'&&!Array.isArray(metrics[k])).forEach(k=>{let v=metrics[k];const labels={selected_training_metrics:'搜索所选模型 · 训练集',validation_metrics:'搜索划分验证（不是独立测试）',full_refit_training_metrics:'全数据重拟合 · 训练集',deployed_training_metrics:'部署模型 · 训练集',test_metrics:'独立测试（以发布协议为准）'};
 html+='<h4>'+E(labels[k]||k)+'</h4><div class="metric-cards">'+['r2','mape','mae','rmse'].map(key=>{let value=v[key];const units=(m.metric_units||{})[k]||{};let text=fmt(value);if(key==='mape'){if(v.mape_percent!=null)text=Number(v.mape_percent).toFixed(2)+'%';else if(value!=null)text=units.mape==='ratio'?(value*100).toFixed(2)+'%':units.mape==='percent'?value.toFixed(2)+'%':fmt(value)+'（原值，单位未声明）';}if(['mae','rmse'].includes(key)&&value!=null)text+=units[key]==='wan_yuan'?' 万元':'（单位未声明）';return '<div><b>'+E(text)+'</b>'+E(key.toUpperCase())+'</div>';}).join('')+'</div>';});
 html+='<p>验证协议：'+E(metrics.validation_protocol||metrics.evaluation_scope||'未提供')+'</p><p>来源：'+E(m.source||'未提供')+' · 报告 SHA '+E(m.training_report_sha256||'未提供')+'</p><button disabled>只读 · 由管理员发布</button><details><summary>管理员发布流程</summary><p>在甲方训练开发包检查输入、训练并通过接口和封装验收；先备份并退出旧软件，再安装已验证的新运行包。字段或价格口径不兼容时须明确迁移。</p><p><a href=/portal>返回服务导航与管理入口</a>。本页不上传或直接启用任意模型文件。</p></details></article>';return html;
 }).join('');
}
function samplePayload(){return Object.assign(D.identity(),{parameters:D.parameters(),range_mode:Q('rangeMode').value});}
function sensitivity(result,ranking){
 if(!result.series.length)throw new Error('没有可用的分析属性');
 Q('sensitivityNote').textContent=ranking+'；'+(result.range_mode==='historical'?'历史 min/max':'当前值 ±20%')+'；'+result.note;
 const top=result.series.length>1;
 D.chart('sensitivityChart',{tooltip:{trigger:'axis',formatter:params=>params.map(p=>{let item=result.series[p.seriesIndex].points[p.dataIndex];return E(p.seriesName)+'：取值 '+E(item.value)+'，价格 '+fmt(item.price_wan)+' 万元，变化 '+fmt(item.change_rate_pct)+'%';}).join('<br>')},legend:{type:'scroll',bottom:0},grid:{left:70,right:25,bottom:80},xAxis:{type:'category',name:top?'独立采样点（悬浮查看真实取值）':'实际属性值',data:top?Array.from({length:Math.max(...result.series.map(s=>s.points.length))},(_,i)=>i+1):result.series[0].points.map(p=>p.value)},yAxis:{type:'value',name:top?'价格变化 %':'价格 · 万元',scale:!top},series:result.series.map(r=>({name:r.label_zh,type:'line',data:r.points.map(p=>top?p.change_rate_pct:p.price_wan)}))});
 Q('sensitivityTable').innerHTML=table(['属性','实际采样值','价格 / 万元','变化率 %'],result.series.flatMap(s=>s.points.map(p=>[s.label_zh,typeof p.value==='number'?Number(p.value.toPrecision(9)):p.value,fmt(p.price_wan),fmt(p.change_rate_pct)])));D.remember(ranking+' · '+(result.range_mode==='historical'?'历史范围':'局部范围'),result);D.status('敏感性分析完成。','success');
}
document.addEventListener('deep:ready',()=>{Q('single').disabled=D.busy||!Q('sensitivityField').options.length;Q('top10').disabled=D.busy||!Q('sensitivityField').options.length;Q('importanceTop10').disabled=D.busy||!(meta&&meta.data.feature_importance&&meta.data.feature_importance.items);});
Q('rangeMode').onchange=()=>{D.epoch++;Q('sensitivityNote').textContent='采样协议已改变，请重新计算';Q('sensitivityTable').textContent='';if(D.charts.sensitivityChart)D.charts.sensitivityChart.clear();};
Q('single').onclick=()=>D.run(async current=>{let r=await D.api('/sensitivity/single',Object.assign(samplePayload(),{field_id:Q('sensitivityField').value}));if(current())sensitivity(r,'单参数敏感性');});
Q('top10').onclick=()=>D.run(async current=>{let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:null}));if(current())sensitivity(r,'扰动敏感性 Top10：按本区间最大价格变化排序');});
Q('importanceTop10').onclick=()=>D.run(async current=>{let data=meta&&meta.data.feature_importance;if(!data||!data.items)throw new Error('当前模型未提供重要度，不能用演示或专家评分选取属性');let keys=data.items.map(r=>r.field_id).filter(k=>D.schema.fields.some(f=>f.field_id===k&&f.analysis_supported)).slice(0,10);if(!keys.length)throw new Error('重要度字段与当前可分析字段不匹配');let r=await D.api('/sensitivity/top10',Object.assign(samplePayload(),{field_ids:keys,ranking_mode:'importance'}));if(current())sensitivity(r,'当前模型重要度先选 Top10，再比较扰动曲线');});
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
 Q('scenarioTop5').innerHTML='<h3>单字段影响 Top5</h3><p>单字段独立预测效应，不可相加为方案总价；联合改动以整套情景预测为准。</p>'+table(['方案','字段','单字段价格差额 / 万元'],effects.slice(0,5).map(c=>[c.variant.name,(D.schema.fields.find(f=>f.field_id===c.key)||{}).label_zh||c.key,fmt(c.delta)]));
 D.chart('scenarioChart',{tooltip:{trigger:'axis'},xAxis:{type:'category',data:variants.map(v=>v.name)},yAxis:{type:'value',name:'价格 / 万元'},series:[{type:'bar',data:result.items.slice(1).map(r=>r.result.predicted_price_wan)}]});
 D.chart('scenarioBubble',{tooltip:{formatter:p=>E(p.data[3])+' · '+E(p.data[4])+'：'+fmt(p.data[1])+' 万元'},xAxis:{type:'value',name:'改动项序号'},yAxis:{type:'value',name:'单字段价格差额 / 万元'},series:[{type:'scatter',symbolSize:v=>Math.min(65,12+Math.sqrt(Math.abs(v[1]))*20),data:effects.map((c,i)=>[i+1,c.delta,Math.abs(c.delta),c.variant.name,(D.schema.fields.find(f=>f.field_id===c.key)||{}).label_zh||c.key])}]});
 D.remember('多属性情景差额与明细',result);D.status('情景比较完成，全部预测来自同一发布模型。','success');
});
document.addEventListener('deep:product',()=>{plans=[];Q('scenarioRows').textContent='';});
document.addEventListener('deep:loaded',async()=>{
 const version=D.schema;Q('modelSummary').hidden=true;Q('importance').hidden=true;meta=null;Q('modelLibrary').textContent='读取当前发布模型…';Q('importanceProvenance').textContent='读取中…';
 Q('analysisAvailability').textContent='自由文本等不可分析字段已从敏感性选项中排除。';
 const oldExtra=Q('r6Adjustment'),adjustment=Q('adjustment');if(oldExtra)oldExtra.remove();Q('fields').hidden=false;
 const group=Q('groups'),first=Q('design').querySelector('article'),extra=document.createElement('div');extra.className='r6-extra';extra.hidden=true;extra.id='r6Adjustment';extra.appendChild(adjustment);first.appendChild(extra);
 const button=document.createElement('button');button.type='button';button.textContent='时间批量校正';button.onclick=()=>{Q('fields').hidden=true;extra.hidden=false;group.querySelectorAll('button').forEach(b=>b.classList.toggle('active',b===button));};group.appendChild(button);['功能参数','性能参数','环境参数'].reverse().forEach(name=>{if(!Array.from(group.querySelectorAll('button')).some(b=>b.textContent===name)){const b=document.createElement('button');b.textContent=name;b.disabled=true;b.title='当前发布产品未配置该分组';group.prepend(b);}});
 group.querySelectorAll('button').forEach(b=>{if(b!==button)b.addEventListener('click',()=>{Q('fields').hidden=false;extra.hidden=true;});});
 D.schema.fields.forEach(f=>{
 const input=Q('field_'+f.field_id),holder=input.parentElement;
 const state=document.createElement('select');state.className='availability';state.setAttribute('aria-label',f.label_zh+'可用状态');state.add(new Option('可用（0仍是有效值）','present'));state.add(new Option('缺失 / 未提供 → null','missing'));
 const unavailable=new Option('不可用：模型未定义专用编码','unavailable');unavailable.disabled=true;state.add(unavailable);state.value=input.value===''?'missing':'present';
 state.onchange=()=>{if(state.value==='missing'){input.dataset.priorValue=input.value;input.value='';input.disabled=true;}else{input.disabled=false;input.value=input.dataset.priorValue||input.value;}input.dispatchEvent(new Event('input'));};
 holder.appendChild(state);
 });
 try{const [importanceData,library]=await Promise.all([D.api('/feature-importance'),D.api('/model-library')]);if(version!==D.schema)return;meta=importanceData;renderLibrary(library);importance();}catch(e){if(version===D.schema)D.status(e.message,'error');}
 document.dispatchEvent(new CustomEvent('deep:ready'));view();
});
})();
