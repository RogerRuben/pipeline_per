import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [base,out,backend,expectedName]=process.argv.slice(2);await fs.mkdir(out,{recursive:true});
const report={checks:{},errors:[],recommendation_seconds:[]};
const browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1480,height:1050}});
page.on('pageerror',e=>report.errors.push(e.message));
async function check(name,value){report.checks[name]=!!value;await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));if(!value)throw Error(name);}
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function status(url){return (await page.request.get(url+'/api/status')).json();}
async function until(fn){const end=Date.now()+60000;while(Date.now()<end){if(await fn())return;await pause(250);}throw Error('state timeout');}
try{
 await page.goto(base+'/portal',{waitUntil:'networkidle'});
 await until(async()=>await page.locator('#productName').textContent()===expectedName);
 await check('excel_product_name',await page.title()===expectedName+' · 产品决策平台');
 const gate=page.frames().find(f=>f.url().includes('/portal/frame'));
 await gate.waitForSelector('iframe',{timeout:30000});
 await check('supplied_front_gate_loaded',(await gate.content()).includes('el-menu'));
 const price=gate.childFrames().find(f=>f.url().includes('/price/deep'));
 if(!price)throw Error('price iframe missing');
 await price.waitForFunction(()=>!document.getElementById('predict').disabled);
 await price.locator('#predict').click();await price.waitForFunction(()=>document.getElementById('price').textContent!=='—');
 await check('embedded_price_predicts',Number(await price.locator('#price').textContent())>0);
 if(backend==='ds_three_layer'){
  await price.locator('#dsLayers').waitFor();
  await check('ds_total_and_layers',await price.locator('#price').textContent()==='4.5000'&&(await price.locator('#dsLayers').innerText()).includes('45000'));
 }
 await page.screenshot({path:out+'/portal-price.png',fullPage:false});
 for(const [route,fragment] of [['costPreEval1','/effectiveness'],['costPreEval2','/cost-effectiveness/'],['intelligent','/recommendation']]){
  await gate.goto(base+'/portal/frame#/'+route,{waitUntil:'networkidle'});await gate.waitForSelector('iframe');
  await until(()=>gate.childFrames().some(f=>f.url().includes(fragment)));
  const child=gate.childFrames().find(f=>f.url().includes(fragment));await child.waitForLoadState('domcontentloaded');
  await check('embedded_'+route,(await child.content()).length>300);
 }
 const reco=gate.childFrames().find(f=>f.url().includes('/recommendation'));
 await reco.waitForFunction(()=>document.getElementById('historyCount').textContent!=='-');
 if(await reco.locator('#tourOverlay').isVisible())await reco.locator('#tourSkip').click();
 for(let i=0;i<3;i++){
  const response=page.waitForResponse(r=>r.url().endsWith('/api/recommend')&&r.request().method()==='POST');const started=performance.now();
  await reco.locator('#recommendBtn').click();await (await response).finished();await reco.locator('.result-card').first().waitFor();
  report.recommendation_seconds.push((performance.now()-started)/1000);
 }
 const old=await reco.locator('#results').innerHTML();await reco.locator('#maxPrice').fill('15');
 await check('r7_results_retained',await reco.locator('#results').innerHTML()===old&&await reco.locator('#recommendationStaleNotice').isVisible());
 const portal=await (await page.request.get(base+'/api/portal')).json();const controlUrl=portal.control_url;
 await page.locator('#management').click();await until(()=>page.frames().some(f=>f.url().startsWith(controlUrl)));
 const manager=page.frames().find(f=>f.url().startsWith(controlUrl));await manager.waitForSelector('#services .service');
 await check('management_embedded',await manager.locator('#services .service').count()===3);
 const original=await status(controlUrl);const row=manager.locator('.service').filter({hasText:'统一价格服务'});
 await row.getByRole('button',{name:'停止',exact:true}).click();await until(async()=>{const s=(await status(controlUrl)).services.price;return s.paused&&!s.alive;});
 let now=await status(controlUrl);await check('single_stop_preserves_others',now.services.effectiveness.pid===original.services.effectiveness.pid&&now.services.web.pid===original.services.web.pid&&!now.services.price.alive);
 await row.getByRole('button',{name:'启动',exact:true}).click();await until(async()=>(await status(controlUrl)).services.price.alive);
 now=await status(controlUrl);const first=now.services.price.pid;
 await row.getByRole('button',{name:'重启',exact:true}).click();await until(async()=>{const s=await status(controlUrl);return s.services.price.alive&&s.services.price.pid!==first;});
 await check('single_restart_preserves_web',(await status(controlUrl)).services.web.pid===original.services.web.pid);
 const web=manager.locator('.service').filter({hasText:'网页与业务服务'});
 await web.getByRole('button',{name:'停止',exact:true}).click();await until(async()=>{const s=(await status(controlUrl)).services.web;return s.paused&&!s.alive;});
 await check('controller_survives_web_stop',await manager.locator('#services').isVisible());
 await web.getByRole('button',{name:'启动',exact:true}).click();await until(async()=>(await status(controlUrl)).services.web.alive);
 await check('web_recovers', (await page.request.get(base+'/api/portal')).ok());
 await page.screenshot({path:out+'/portal-management.png',fullPage:false});
 await check('correct_backend_label',(await manager.locator('#classification').innerText()).includes(backend==='ds_three_layer'?'三层定价':'AutoML V3'));
 await check('no_javascript_errors',report.errors.length===0);report.status='pass';
}catch(e){report.status='fail';report.error=e.stack;await page.screenshot({path:out+'/failure.png',fullPage:false});throw e;}
finally{await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));await browser.close();}
console.log(JSON.stringify(report,null,2));
