import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [base,file,out]=process.argv.slice(2);const browser=await chromium.launch({channel:'chrome',headless:true});
try{
 const page=await browser.newPage({viewport:{width:1480,height:1050}});await page.goto(base+'/model-library',{waitUntil:'networkidle'});
 await page.locator('#role').selectOption('effectiveness');await page.locator('#name').fill('原泵 V13 一键导出模型');await page.locator('#file').setInputFiles(file);await page.locator('#install').click();
 const card=page.locator('#items article').filter({hasText:'原泵 V13 一键导出模型'});await card.waitFor({timeout:90000});await card.locator('.activate').click();await card.locator('.active').waitFor({timeout:90000});await page.screenshot({path:out+'/model-imported.png'});
 await page.goto(base+'/effectiveness',{waitUntil:'networkidle'});await page.locator('#effectEvaluateBtn').waitFor();await page.locator('#effectEvaluateBtn').click();await page.locator('#effectResults').waitFor({state:'visible'});await page.screenshot({path:out+'/expert-evaluation.png'});
 await fs.writeFile(out+'/browser.json',JSON.stringify({status:'pass',score:await page.locator('#effectScore').textContent()},null,2));
}finally{await browser.close();}
