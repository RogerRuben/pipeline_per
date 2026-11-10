import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [base,out,effectPath]=process.argv.slice(2);await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({viewport:{width:1480,height:1000}});
const report={checks:{},errors:[]};context.on('page',p=>p.on('pageerror',e=>report.errors.push(e.message)));
async function check(k,v){report.checks[k]=!!v;await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));if(!v)throw Error(k);}
async function api(path,data){const r=data===undefined?await context.request.get(base+path):await context.request.post(base+path,{data});const v=await r.json();if(!r.ok())throw Error(path+': '+JSON.stringify(v));return v;}
const pause=ms=>new Promise(r=>setTimeout(r,ms));
try{
 const portal=await context.newPage();await portal.goto(base+'/portal',{waitUntil:'networkidle'});
 await check('portal_frame',portal.frames().filter(f=>f.url().includes('/portal/frame')).length===1);
 const before=await api('/api/admin/snapshot');const numeric=before.parameters.find(p=>p.value_type==='number');const categorical=before.parameters.find(p=>p.value_type==='enum');
 const pages={};for(const [key,route] of Object.entries({deep:'/price/deep',simple:'/price',effect:'/effectiveness',recommend:'/recommendation',cost:'/cost-effectiveness/'})){
  pages[key]=await context.newPage();await pages[key].goto(base+route,{waitUntil:'networkidle'});
  await check(key+'_theme',await pages[key].locator('body>header').evaluate(e=>getComputedStyle(e).backgroundColor)==='rgb(18, 54, 92)');
  await check(key+'_branding',!(/工业.{0,12}系统/.test(await pages[key].locator('body>header').innerText())));
 }
 const deep=pages.deep;await deep.waitForFunction(()=>DeepPrice.schema&&DeepPrice.selected&&!DeepPrice.busy);
 const original=await deep.evaluate(()=>({price:DeepPrice.selected.historical_price_wan,parameters:DeepPrice.parameters(),identity:DeepPrice.identity(),id:DeepPrice.selected.product_id}));
 await check('selection_shows_historical_price',Number(await deep.locator('#price').textContent())===Number(original.price.toFixed(4)));
 const prediction=deep.waitForResponse(r=>r.url().endsWith('/reference-price'));await deep.locator('#predict').click();const reference=await (await prediction).json();
 await check('historical_price_route',reference.price_source==='historical'&&reference.predicted_price_wan===original.price);
 const pure=await api('/api/deep-price/v1/predict',{...original.identity,parameters:original.parameters});
 await check('pure_model_route_preserved',pure.pricing_mode!=='historical'&&Number.isFinite(pure.predicted_price_wan));
 const edited=await deep.evaluate(id=>{const e=document.getElementById('field_'+id),f=DeepPrice.schema.fields.find(f=>f.field_id===id);let v=Number(e.value),next=v+Math.max(.01,Math.abs(v)*.03);if(f.maximum!=null)next=Math.min(next,f.maximum);if(next===v)next=v-.01;e.value=String(next);e.dispatchEvent(new Event('input',{bubbles:true}));return e.value;},numeric.parameter_id);
 const modified=deep.waitForResponse(r=>r.url().endsWith('/reference-price'));await deep.locator('#predict').click();const model=await (await modified).json();
 await check('modified_inputs_use_model',model.price_source==='model'&&Number.isFinite(model.predicted_price_wan));
 const chart=await deep.evaluate(()=>({animation:DeepPrice.charts.radar.getOption().animation,duration:DeepPrice.charts.radar.getOption().animationDurationUpdate}));
 await check('radar_animation_enabled',chart.animation&&chart.duration>0);
 await deep.locator('#reset').click();await deep.locator('a[href="#scenario"]').click();
 const response=deep.waitForResponse(r=>r.url().endsWith('/sensitivity/top10'));const start=performance.now();await deep.locator('#top10').click();const top=await (await response).json();report.top10_seconds=(performance.now()-start)/1000;
 await deep.waitForFunction(()=>!DeepPrice.busy);
 await check('top10_completes',top.success!==false&&top.series.length>0&&report.top10_seconds<30);
 const options=await deep.evaluate(()=>DeepPrice.charts.sensitivityChart.getOption());
 await check('smooth_relative_sensitivity',options.xAxis[0].type==='value'&&options.series.every(s=>s.smooth===true)&&options.xAxis[0].name.includes('%'));
 await deep.screenshot({path:out+'/sensitivity.png'});
 const admin=await context.newPage();await admin.goto(base+'/admin',{waitUntil:'networkidle'});await admin.locator('[data-section="parameters"]').click();
 report.updates=[];
 for(const item of [numeric,categorical].filter(Boolean)){
  const row=admin.locator('#mainAdminTable tbody tr').filter({has:admin.locator('td:first-child',{hasText:item.parameter_id})});
  await row.locator('.edit-row').click();const name='验收新中文_'+item.parameter_id;await admin.locator('#adminEditForm [name="label"]').fill(name);
  const request=admin.waitForRequest(r=>r.url().endsWith('/api/admin/upsert'));const saved=admin.waitForResponse(r=>r.url().endsWith('/api/admin/upsert'));
  await admin.locator('#saveAdminEdit').click();const payload=(await request).postDataJSON();const result=await saved;
  await check('rename_'+item.parameter_id,result.ok()&&Object.keys(payload.item).sort().join(',')==='label,parameter_id');report.updates.push({id:item.parameter_id,name,payload});
  await pause(500);
 }
 const after=await api('/api/admin/snapshot');
 await check('all_non_label_columns_unchanged',report.updates.every(u=>{const a={...after.parameters.find(p=>p.parameter_id===u.id)},b={...before.parameters.find(p=>p.parameter_id===u.id)};delete a.label;delete b.label;return JSON.stringify(a)===JSON.stringify(b);}));
 // Bring each tab forward: the metadata poll intentionally pauses in hidden tabs.
 for(const key of ['deep','simple','effect']){
  const page=pages[key];await page.bringToFront();await page.waitForTimeout(4800);
  const label=report.updates.find(u=>u.id===numeric.parameter_id).name;
  const locator=key==='deep'?page.locator('#field_'+numeric.parameter_id):page.locator('[name="'+numeric.parameter_id+'"]');
  await check(key+'_live_label',await locator.evaluate(e=>e.closest('label').innerText.includes('验收新中文_')));
 }
 for(const route of ['/api/price-workbench/schema','/api/effectiveness-workbench/schema','/api/deep-price/v1/schema']){
  const schema=await api(route);await check('schema_label_'+route,JSON.stringify(schema).includes('验收新中文_'+numeric.parameter_id));
 }
 const priceModels=await api('/api/portal/models/price');await check('portal_real_metrics',Object.keys(priceModels.model_sets[0].metrics).length>0&&priceModels.model_sets[0].model_count>1);
 await portal.locator('#models').click();const lib=portal.frameLocator('#libraryFrame');await lib.locator('#current').waitFor();await check('editable_model_library',await lib.locator('#install').isVisible()&&(await lib.locator('#current').innerText()).includes('MAPE'));
 await portal.screenshot({path:out+'/model-library.png'});
 if(effectPath){
  const effect=JSON.parse(await fs.readFile(effectPath,'utf8'));effect.model_version=String(effect.model_version)+'-ui';const file=out+'/ui_effectiveness.json';await fs.writeFile(file,JSON.stringify(effect));
  await lib.locator('#role').selectOption('effectiveness');await lib.locator('#name').fill('界面导入效能验收');await lib.locator('#file').setInputFiles(file);await lib.locator('#install').click();
  await lib.locator('#items article').filter({hasText:'界面导入效能验收'}).waitFor();await check('model_library_gui_import',true);
  await lib.locator('#items article').filter({hasText:'界面导入效能验收'}).locator('.activate').click();
  await lib.locator('#status').filter({hasText:'模型已切换并保存'}).waitFor();await lib.locator('#items article .active').waitFor();await check('model_library_gui_activate',await lib.locator('#items article .active').count()===1);
  portal.once('dialog',d=>d.accept('界面改名效能验收'));await lib.locator('#items .rename').first().click();await lib.locator('#items article').filter({hasText:'界面改名效能验收'}).waitFor();await check('model_library_gui_rename',true);
  await portal.screenshot({path:out+'/model-library-activated.png'});
 }
 const reco=pages.recommend;await reco.bringToFront();if(await reco.locator('#tourOverlay').isVisible())await reco.locator('#tourSkip').click();
 report.recommendation_seconds=[];
 for(let i=0;i<3;i++){const recResponse=reco.waitForResponse(r=>r.url().endsWith('/api/recommend'));const began=performance.now();await reco.locator('#recommendBtn').click();await (await recResponse).finished();await reco.locator('.result-card').first().waitFor();report.recommendation_seconds.push((performance.now()-began)/1000);}
 await check('recommendation_under_six_seconds',report.recommendation_seconds.every(v=>v<6));
 await reco.locator('.result-card').first().click();
 if(await reco.locator('#tourOverlay').isVisible())await reco.locator('#tourSkip').click();
 await reco.locator('#rangeDiagnosticsPanel details').evaluate(e=>e.open=true);
 await check('all_reference_ranges_visible',await reco.locator('#rangeDiagnosticsPanel').innerText().then(t=>t.includes('DataMaster')&&t.includes('训练')));
 await reco.screenshot({path:out+'/recommendation-ranges.png'});
 await reco.locator('#closeDetail').click();
 await portal.locator('#workbench').click();const gate=portal.frames().find(f=>f.url().includes('/portal/frame'));
 await gate.goto(base+'/portal/frame#/intelligent',{waitUntil:'networkidle'});await gate.waitForSelector('iframe');
 let embedded;for(let i=0;i<50&&!embedded;i++){embedded=gate.childFrames().find(f=>f.url().includes('/recommendation'));if(!embedded)await pause(100);}
 await embedded.locator('a[href="/portal"]').first().click();await portal.waitForLoadState('networkidle');
 await check('navigation_returns_top_portal',portal.frames().filter(f=>f.url().endsWith('/portal')).length===1&&portal.url()===base+'/portal');
 await admin.screenshot({path:out+'/admin-renamed.png'});
 await check('no_browser_errors',report.errors.length===0);report.status='pass';
}catch(e){report.status='fail';report.error=e.stack;for(const p of context.pages().slice(-1))await p.screenshot({path:out+'/failure.png'});throw e;}
finally{await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));await browser.close();}
console.log(JSON.stringify(report,null,2));
