import fs from 'node:fs/promises';
import {chromium} from '../outputs/r7_browser/node_modules/playwright/index.mjs';
const [control,portal,preferred,out]=process.argv.slice(2);
const browser=await chromium.launch({channel:'chrome',headless:true});
try{
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 await page.goto(control,{waitUntil:'networkidle'});await page.waitForFunction(()=>document.querySelector('#portal').href.includes('/portal'));
 if(await page.locator('#portal').getAttribute('href')!==portal)throw Error('Wrong portal address');
 if(!(await page.locator('#error').textContent()).includes('被占用'))throw Error('Fallback notice missing');
 await page.screenshot({path:out+'/actual-ports.png'});
 await page.locator('#openPortal').click();await page.waitForURL(portal);await page.waitForLoadState('networkidle');
 if(page.url().includes(':'+preferred+'/'))throw Error('Browser uses occupied preferred port');
 await page.screenshot({path:out+'/actual-portal.png'});
 await fs.writeFile(out+'/browser.json',JSON.stringify({status:'pass',control,portal,configured_web:Number(preferred)},null,2));
}finally{await browser.close();}
