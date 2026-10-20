import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const [baseUrl, outputDir, expectedCount = "164", mode = "v1", expectedPriceFields = "50", expectedScoreRows = "50"] = process.argv.slice(2);
if (!baseUrl || !outputDir) throw new Error("Usage: base-url output-directory");
await fs.mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: "msedge" });
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
const errors = [];
page.on("pageerror", error => errors.push(error.message));
try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForFunction(count => document.getElementById("historyCount").textContent === count, expectedCount);
  await page.locator("#addFilterBtn").click();
  const labels = await page.locator(".f-param option").allTextContents();
  if (!labels.some(label => label.includes("额定推力")) || labels.some(label => /rated_thrust_n|power_type/.test(label))) {
    throw new Error("筛选器没有统一显示中文属性名");
  }
  await page.locator(".remove-filter").click();
  await page.locator("#generationCount").fill("1");
  await page.locator("#recommendBtn").click();
  await page.locator(".result-card").first().waitFor({ state: "visible", timeout: 20000 });
  await page.screenshot({ path: path.join(outputDir, "recommendation.png"), fullPage: false });
  await page.locator(".result-card").first().click();
  await page.locator("#detailOverlay").waitFor({ state: "visible" });
  const editorLabels = await page.locator("#parameterEditor .parameter-label > span:first-child").allTextContents();
  if (!editorLabels.includes("额定推力") || !editorLabels.includes("电源类型")) throw new Error("编辑器中文属性名称不完整");
  const field = page.locator('#parameterEditor input[data-key="rated_thrust_n"]');
  const oldPrice = await page.locator("#detailPrice").textContent();
  await field.fill(String(Number(await field.inputValue()) + 500));
  if (!(await page.locator("#evaluationStatus").textContent()).includes("过期")) throw new Error("修改属性后未标记计算过期");
  await page.locator("#evaluateBtn").click();
  await page.waitForFunction(price => document.getElementById("detailPrice").textContent !== price, oldPrice);
  await page.locator("#showPriceAttributes").check();
  await page.locator('#parameterEditor select[data-key="power_type"]').waitFor({ state: "visible" });
  await page.screenshot({ path: path.join(outputDir, "attribute_recalculation.png"), fullPage: false });
  await page.locator("#closeDetail").click();
  await page.goto(baseUrl + "price", { waitUntil: "networkidle" });
  await page.locator('#priceFields input[name="rated_thrust_n"]').waitFor({ state: "visible" });
  if (mode === "v2" || mode === "attribute_scores") {
    if (await page.locator("#priceFields .effect-field").count() !== Number(expectedPriceFields)) throw new Error("价格界面可用属性数量与模型schema不一致");
    await page.locator("#pricePredictBtn").click();
    await page.locator("#priceEnsemblePanel").waitFor({ state: "visible" });
    const panelText = await page.locator("#priceEnsemblePanel").textContent();
    if (mode === "attribute_scores") {
      if (!panelText.includes("价格属性评分")) throw new Error("缺少价格输入属性的1—5分解释");
      const scoreTable = page.locator("#priceEnsemblePanel .ensemble-table").nth(1);
      if (await scoreTable.locator("tbody tr").count() !== Number(expectedScoreRows)) throw new Error("价格属性评分表须包含全部原始属性");
      const scores = await scoreTable.locator("tbody tr td:nth-child(2)").allTextContents();
      if (scores.some(value => !(parseFloat(value) >= 1 && parseFloat(value) <= 5))) throw new Error("价格属性评分超出1—5分");
      if (!panelText.includes("未评分，使用中性")) throw new Error("缺少未评分属性的中性说明");
      if (Number(expectedPriceFields) < Number(expectedScoreRows) && !/整列为空|文本属性|无有效值/.test(panelText)) throw new Error("排除的合法价格属性未展示原因");
    } else if (!/专家意见与学习结果|专家模型份额与学习结果/.test(panelText)) throw new Error("缺少专家权重解释");
    if ((await page.locator("#priceEnsemblePanel .ensemble-table").count()) < 2) throw new Error("缺少融合或先验表");
    await page.locator("#priceEnsemblePanel").screenshot({ path: path.join(outputDir, "v2_expert_explanation.png") });
  }
  await page.screenshot({ path: path.join(outputDir, "price_workbench.png"), fullPage: false });
  if (errors.length) throw new Error(errors.join("\n"));
  await fs.writeFile(path.join(outputDir, "ui_report.json"), JSON.stringify({ status: "PASS", checks: ["中文属性筛选", "推荐结果", "属性编辑", "过期状态", "显式重算", "枚举显示", "价格工作台", ...(["v2", "attribute_scores"].includes(mode) ? [String(expectedPriceFields) + "个有效价格输入（" + String(expectedScoreRows) + "个原始属性）", mode === "attribute_scores" ? String(expectedScoreRows) + "个价格属性的1—5分先验与中性标记" : "专家权重与融合解释"] : [])], pageErrors: errors }, null, 2));
  console.log("PASS: 中文筛选、推荐、属性编辑、显式重算与价格工作台");
} finally {
  await browser.close();
}
