import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [base,control,out,cases]=process.argv.slice(2),report={checks:{},errors:[]};
await fs.mkdir(out,{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});const context=await browser.newContext({viewport:{width:1240,height:1000}});
context.on('page',p=>p.on('pageerror',e=>report.errors.push(e.message)));
async function check(k,v){report.checks[k]=!!v;await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));if(!v)throw Error(k);}
try{
 const portal=await context.newPage();await portal.goto(base+'/portal',{waitUntil:'networkidle'});
 const gate=portal.frames().find(f=>f.url().includes('/portal/frame'));await gate.locator('.cost-prediction-home').waitFor();
 await check('default_is_home',gate.url().endsWith('#/')&&await gate.locator('.main-functions .function-card').count()===3);
 await portal.screenshot({path:out+'/home.png'});
 // Open supplied front-gate model overview; precision should be controlled in this view too.
 await gate.goto(base+'/portal/frame#/modelManage/main',{waitUntil:'networkidle'});
 // The supplied route name can differ; click its visible sidebar link if needed.
 const routes=await gate.locator('a').evaluateAll(es=>es.map(e=>e.getAttribute('href')));
 const modelChunk=await (await context.request.get(base+'/js/chunk-4ea70ec6.5168a102.js')).text();
 await check('portal_metrics_formatter_updated',modelChunk.includes('toFixed(4)')&&modelChunk.includes('toFixed(2)'));
 await gate.locator('.metric-value').first().waitFor();await check('portal_metric_values_are_short',await gate.locator('.metric-value').evaluateAll(es=>es.every(e=>!/\.\d{5}/.test(e.textContent))));await gate.waitForTimeout(600);await portal.screenshot({path:out+'/portal-models.png'});
 const model=await context.newPage();await model.goto(base+'/model-library',{waitUntil:'networkidle'});await model.locator('#current table').waitFor();
 await check('library_metrics_four_decimals',await model.locator('#current tbody tr td:nth-child(2)').evaluateAll(es=>es.every(e=>/^\d+\.\d{4}$/.test(e.textContent))));
 await model.locator('summary').first().click();await check('mapping_explanation',await model.locator('body').innerText().then(s=>s.includes('不改变训练或业务数据')&&s.includes('无需填写')));
 const deep=await context.newPage();await deep.goto(base+'/price/deep',{waitUntil:'networkidle'});await deep.waitForFunction(()=>DeepPrice.schema&&DeepPrice.charts.radar);
 await check('scenario_interface_removed',await deep.locator('#scenario,#scenarioMode,#runScenarios').count()===0&&!(await deep.locator('nav').innerText()).includes('情景'));
 await deep.waitForFunction(()=>!DeepPrice.radarAnimation);
 const field=await deep.evaluate(()=>DeepPrice.schema.fields.find(f=>f.type==='number'&&f.price_model_input).field_id);
 const radar=await deep.evaluate(async key=>{
  const e=document.getElementById('field_'+key);e.value=Number(e.value)+10;e.dispatchEvent(new Event('input',{bubbles:true}));const frames=[];
  for(let i=0;i<5;i++){await new Promise(r=>setTimeout(r,70));const chart=DeepPrice.charts.radar,view=chart._chartsViews.find(v=>v.type==='radar');const lines=[],symbols=[];view.group.traverse(el=>{if(el.type==='polyline')lines.push(el.shape.points);if(el.type==='path'||el.type==='circle'){const rect=el.getBoundingRect(),x=rect.x+rect.width/2,y=rect.y+rect.height/2,m=el.getComputedTransform();symbols.push(m?[m[0]*x+m[2]*y+m[4],m[1]*x+m[3]*y+m[5]]:[x,y]);}});frames.push({animation:chart.getOption().animation,values:chart.getOption().series[0].data[0].value,lines,symbols});}return frames;
 },field);
 report.radar_frames=radar;
 await check('radar_shared_frame_interpolation',radar.every(f=>f.animation===false)&&radar[0].values[0]!==radar[4].values[0]);
 await check('radar_points_match_polygon_each_frame',radar.every(f=>f.symbols.length===6&&f.symbols.every((p,i)=>Math.hypot(p[0]-f.lines[0][i][0],p[1]-f.lines[0][i][1])<0.1)));
 await deep.locator('a[href="#sensitivity"]').click();const response=deep.waitForResponse(r=>r.url().endsWith('/sensitivity/single'));await deep.locator('#single').click();await (await response).finished();await deep.waitForFunction(()=>!DeepPrice.busy);
 const data=await deep.evaluate(()=>JSON.stringify(DeepPrice.charts.sensitivityChart.getOption().series[0].data));
 await deep.locator('#curveStyle').selectOption('line');await check('line_style_applied',await deep.evaluate(()=>{const s=DeepPrice.charts.sensitivityChart.getOption().series[0];return s.smooth===false&&s.symbol==='circle'&&s.symbolSize===6;}));
 await deep.screenshot({path:out+'/curve-line.png'});
 await deep.locator('#curveStyle').selectOption('smooth');await check('smooth_style_applied',await deep.evaluate(()=>{const s=DeepPrice.charts.sensitivityChart.getOption().series[0];return s.smooth===true&&s.symbol==='none';}));
 await check('curve_switch_does_not_modify_prices',data===await deep.evaluate(()=>JSON.stringify(DeepPrice.charts.sensitivityChart.getOption().series[0].data)));
 await deep.screenshot({path:out+'/curve-smooth.png'});
 await deep.goto(base+'/price/deep#design',{waitUntil:'networkidle'});await deep.waitForFunction(()=>DeepPrice.schema);
 const manager=await context.newPage();await manager.goto(control,{waitUntil:'networkidle'});await manager.waitForFunction(()=>document.getElementById('portNotice').textContent.length>0);
 await check('notice_has_full_width',await manager.locator('#portNotice').evaluate(e=>e.getBoundingClientRect().width>700&&e.getBoundingClientRect().height<180));
 await manager.screenshot({path:out+'/manager.png'});await manager.setViewportSize({width:560,height:900});
 await check('notice_responsive_no_overflow',await manager.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 for(const route of ['/price','/effectiveness','/recommendation','/cost-effectiveness/']){
  const p=await context.newPage();await p.goto(base+route,{waitUntil:'networkidle'});if(await p.locator('#tourOverlay').isVisible())await p.locator('#tourSkip').click();await p.locator(route.includes('cost-effectiveness')?'#recommendationLink':'a[href="/portal"]').first().click();await p.waitForLoadState('networkidle');const f=p.frames().find(f=>f.url().includes('/portal/frame'));await f.locator('.cost-prediction-home').waitFor();await check('home_link_'+route,p.url()===base+'/portal');await p.close();
 }
 if(cases){
  const expected=JSON.parse(await fs.readFile(cases+'/expected_predictions.json','utf8')).rows[0];report.case_predictions={};
  const request=async(path,body)=>{const r=body===undefined?await context.request.get(base+path):await context.request.post(base+path,{data:body});const v=await r.json();if(!r.ok())throw Error(path+JSON.stringify(v));return v;};
  for(const [letter,file,role] of [['A','A_V3_baseline.zip','price'],['B','B_V3_nonlinear.zip','price'],['C','C_three_layer_ready.json','price'],['D','D_effectiveness_demo.json','effectiveness']]){
   await model.bringToFront();await model.locator('#role').selectOption(role);await model.locator('#name').fill('测试案例 '+letter);await model.locator('#file').setInputFiles(cases+'/'+file);await model.locator('#install').click();const card=model.locator('#items article').filter({hasText:'测试案例 '+letter});await card.waitFor();await card.locator('.activate').click();await card.locator('.active').waitFor({timeout:80000});
   await check('case_'+letter+'_ui_import_and_activate',true);
   if(role==='price'){
    const schema=await request('/api/deep-price/v1/schema');let id={};for(const key of ['product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature'])id[key]=schema[key];
    const value=await request('/api/deep-price/v1/predict',{...id,parameters:expected.modified_parameters});report.case_predictions[letter]=value.predicted_price_wan;await check('case_'+letter+'_price_matches_guide',Math.abs(value.predicted_price_wan-expected.modified_prices_wan[letter])<1e-8);
   }else await check('case_D_effect_service_active',(await request('/api/effectiveness-workbench/schema')).model_version==='DEMO-EFFECT-20261111');
  }
  await check('price_cases_produce_distinct_results',new Set(Object.values(report.case_predictions).map(v=>v.toFixed(4))).size===3);
  await model.locator('#items article').filter({hasText:'测试案例 A'}).locator('.activate').click();await model.locator('#items article').filter({hasText:'测试案例 A'}).locator('.active').waitFor();
  await model.screenshot({path:out+'/model-cases.png'});
 }
 await check('no_js_errors',report.errors.length===0);report.status='pass';
}catch(e){report.status='fail';report.error=e.stack;throw e;}finally{await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));await browser.close();}
