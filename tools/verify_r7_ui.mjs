import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [base,out]=process.argv.slice(2);await fs.mkdir(out,{recursive:true});
const report={checks:{},recommendation_seconds:[],errors:[]};
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
page.on('pageerror',e=>report.errors.push(e.message));
async function check(k,v){report.checks[k]=!!v;await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));if(!v)throw Error(k);}
try{
 await page.goto(base+'/',{waitUntil:'networkidle'});
 if(await page.locator('#tourOverlay').isVisible())await page.locator('#tourSkip').click();
 await page.locator('#recommendBtn').waitFor();
 for(let i=0;i<5;i++){
  const started=performance.now();const response=page.waitForResponse(r=>r.url().endsWith('/api/recommend')&&r.request().method()==='POST');
  await page.locator('#recommendBtn').click();await (await response).finished();
  await page.locator('.result-card').first().waitFor({state:'visible'});
  report.recommendation_seconds.push((performance.now()-started)/1000);
 }
 const before=await page.locator('#results').innerHTML(),count=await page.locator('.result-card').count();
 await page.locator('#maxPrice').fill('15');
 await check('criteria_change_keeps_results',await page.locator('#results').innerHTML()===before&&await page.locator('.result-card').count()===count);
 await check('criteria_change_notice_visible',await page.locator('#recommendationStaleNotice').isVisible());
 await check('ranking_explanation_removed',await page.locator('.ranking-explanation').count()===0&&!(await page.locator('body').innerText()).includes('为什么排在前面'));
 await page.screenshot({path:out+'/recommendation-stale.png',fullPage:false});
 let reached;const intercepted=new Promise(r=>reached=r);let release;const gate=new Promise(r=>release=r);
 await page.route('**/api/recommend',async route=>{const response=await route.fetch();reached();await gate;await route.fulfill({response});});
 await page.locator('#recommendBtn').click();await intercepted;
 await page.locator('#minCapability').fill('99');release();
 await page.waitForTimeout(400);
 await check('inflight_old_response_ignored',await page.locator('#results').innerHTML()===before&&await page.locator('#recommendationStaleNotice').isVisible());
 await page.unroute('**/api/recommend');
 await page.locator('#maxPrice').fill('');await page.locator('#minCapability').fill('');
 const refreshed=page.waitForResponse(r=>r.url().endsWith('/api/recommend'));await page.locator('#recommendBtn').click();await (await refreshed).finished();
 await page.locator('#recommendationStaleNotice').waitFor({state:'hidden'});
 await check('explicit_refresh_clears_notice',!(await page.locator('#recommendationStaleNotice').isVisible()));
 await page.locator('#generationPopover > summary').click();
 await page.getByText('高级探索设置',{exact:true}).click();
 await page.locator('#generationCount').fill('1');
 await page.locator('#generationBudget').fill('30');await page.locator('#generationRounds').fill('1');
 await page.locator('#generationTimeBudget').fill('10');await page.locator('#generateBtn').click();
 await page.waitForFunction(()=>document.getElementById('sourceMode').value==='generated',null,{timeout:120000});
 await page.locator('.result-card').first().waitFor({state:'visible'});
 const generated=await page.locator('#results').innerHTML();
 await page.locator('#maxPrice').fill('16');
 await check('generated_results_kept_after_change',await page.locator('#results').innerHTML()===generated&&await page.locator('#recommendationStaleNotice').isVisible());
 await page.goto(base+'/price/deep#design',{waitUntil:'networkidle'});await page.locator('#predict').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.getElementById('predict').disabled);
 for(const name of ['功能参数','性能参数','环境参数'])await check('group_'+name,await page.locator('#groups button').filter({hasText:name}).isEnabled());
 await page.locator('#predict').click();await page.waitForFunction(()=>document.getElementById('price').textContent!=='—');
 await check('deep_predict_works',Number(await page.locator('#price').textContent())>0);
 await check('technical_hash_collapsed',!(await page.locator('body').innerText()).includes('模型 SHA'));
 await page.screenshot({path:out+'/deep-design.png',fullPage:false});
 await page.locator('nav a[href="#factors"]').click();await page.locator('#importanceSource').selectOption('expert');
 await page.waitForFunction(()=>document.querySelectorAll('#importanceTable tbody tr').length===60);
 await check('workbook_expert_scores_visible',await page.locator('#importanceTable tbody tr').count()===60);
 const scores=await page.locator('#importanceTable tbody tr td:nth-child(2)').allTextContents();
 await check('expert_score_values_correct',scores.every(s=>['1','2','3','4','5'].includes(s)));
 await page.screenshot({path:out+'/expert-scores.png',fullPage:false});
 await page.locator('nav a[href="#model"]').click();await check('model_details_collapsed',await page.locator('#modelLibrary details:not([open])').count()>0);
 await page.screenshot({path:out+'/model.png',fullPage:false});
 await check('no_javascript_errors',report.errors.length===0);report.status='pass';
}catch(e){report.status='fail';report.error=e.stack;await page.screenshot({path:out+'/failure.png',fullPage:false});throw e;}
finally{await fs.writeFile(out+'/browser.json',JSON.stringify(report,null,2));await browser.close();}
console.log(JSON.stringify(report,null,2));
