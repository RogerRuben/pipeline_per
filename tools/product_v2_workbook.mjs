import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [payloadPath, outputPath] = process.argv.slice(2);
const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const workbook = Workbook.create();
function style(sheet, rows, columns, headerRows = 1) {
  const used = sheet.getRangeByIndexes(0, 0, rows, columns);
  sheet.showGridLines = false;
  used.format.font = { name: "Arial", size: 10, color: "#243447" };
  used.format.rowHeight = 26;
  used.format.columnWidth = 28;
  used.format.verticalAlignment = "center";
  sheet.getRangeByIndexes(0, 0, headerRows, columns).format = { fill: "#24466C", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, rowHeight: 32 };
  sheet.freezePanes.freezeRows(headerRows);
}
const data = workbook.worksheets.add("成品数据");
const matrix = [payload.labels, payload.field_ids, ...payload.rows];
data.getRangeByIndexes(0, 0, matrix.length, payload.labels.length).values = matrix;
style(data, matrix.length, payload.labels.length, 2);
data.getRangeByIndexes(1, 0, 1, payload.labels.length).format.fill = "#EDF2F7";
data.getRangeByIndexes(1, 0, 1, payload.labels.length).format.font = { name: "Arial", size: 10, color: "#52677F" };
data.freezePanes.freezeColumns(2);
data.getRangeByIndexes(2, 2, 10, 1).setNumberFormat("0.00000");
data.getRangeByIndexes(2, 11, 10, 40).setNumberFormat("0.000");
data.getRangeByIndexes(0, 0, 12, 1).format.columnWidth = 15;
data.getRangeByIndexes(0, 1, 12, 1).format.columnWidth = 22;
for (let col = 2; col < payload.labels.length; col++) {
  data.getRangeByIndexes(0, col, matrix.length, 1).format.columnWidth = Math.max(22, payload.field_ids[col].length + 3);
}
data.getRangeByIndexes(2, 3, 10, 8).dataValidation = { rule: { type: "list", values: ["有", "无"] } };

const family = workbook.worksheets.add("专家模型权重");
const familyRows = [["模型族代号", "中文名称", "专家权重", "信任系数", "最小份额", "最大份额"],
  ...payload.price_config.ensemble.family_priors.map(x => [x.family, x.label, x.weight, x.confidence, x.lower_bound || 0, x.upper_bound == null ? 1 : x.upper_bound])];
family.getRangeByIndexes(0, 0, familyRows.length, 6).values = familyRows;
style(family, familyRows.length, 6);
family.getRange("C2:F5").setNumberFormat("0.0%");
family.getRange("H1").values = [["演示预设，训练前固定；权重是模型族的总份额。"]];
family.getRange("H2").values = [["本配置开启工作簿先验读取，修改后重新构建即可使用。"]];
family.getRange("H1:H2").format.columnWidth = 70;

const attributes = workbook.worksheets.add("属性配置");
const preferences = { higher: "越大越好", lower: "越小越好", neutral: "中性" };
const attributeRows = [["英文属性代号", "效能偏好方向", "专家效能权重", "中文属性名", "属性分组"],
  ...payload.field_ids.slice(3).map((key, index) => [key, preferences[payload.effectiveness_config.attributes[key].preference],
    payload.effectiveness_config.attributes[key].weight, payload.labels[index + 3], payload.attribute_groups[key]])];
attributes.getRangeByIndexes(0, 0, attributeRows.length, 5).values = attributeRows;
style(attributes, attributeRows.length, 5);
attributes.getRange("C2:C51").setNumberFormat("0.00%");
attributes.getRange("B2:B51").dataValidation = { rule: { type: "list", values: Object.values(preferences) } };
attributes.getRange("G1").values = [["功能30%、性能50%、环境20%；类别暂不计入效能。"]];
attributes.getRange("G2").values = [["效能权重不作为价格回归系数。"]];
attributes.getRange("G1:G2").format.columnWidth = 68;

const cost = workbook.worksheets.add("价格工程先验");
const costRows = [["英文属性代号", "中文名称", "价格系数(万元/单位)", "单位"], ["__intercept__", "基础成本", payload.price_config.engineering.intercept_wan, "万元"],
  ...Object.entries(payload.price_config.engineering.coefficients).map(([key, value]) => [key, payload.labels[payload.field_ids.indexOf(key)], value, "见属性标题"] )];
cost.getRangeByIndexes(0, 0, costRows.length, 4).values = costRows;
style(cost, costRows.length, 4);
cost.getRange(`C2:C${costRows.length}`).setNumberFormat("0.000000");
cost.getRange("F1").values = [["演示成本参数，按万元统一；残差修正独立展示。"]];
cost.getRange("F1").format.columnWidth = 68;

const coupling = workbook.worksheets.add("耦合关系");
const couplingRows = [["源属性代号", "目标属性代号", "工程系数", "工程偏置", "绝对容差"],
  ...payload.effectiveness_config.couplings.map(x => [x.source, x.target, x.coefficient, x.offset, x.tolerance])];
coupling.getRangeByIndexes(0, 0, couplingRows.length, 5).values = couplingRows;
style(coupling, couplingRows.length, 5);
coupling.getRange("C2:E4").setNumberFormat("0.00000");
coupling.getRange("G1").values = [["预设工程关系，用于效能轮廓和推荐生成约束。"]];
coupling.getRange("G2").values = [[payload.source_note]];
coupling.getRange("G1:G2").format.columnWidth = 80;

workbook.recalculate();
const outputDir = path.dirname(outputPath);
await fs.mkdir(outputDir, { recursive: true });
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!", options: { useRegex: true, maxResults: 10 }, maxChars: 500 });
await fs.writeFile(path.join(outputDir, "workbook_inspection.json"), errors.ndjson);
for (const [sheetName, range, filename] of [["成品数据", "A1:K8", "tiny_products.png"], ["专家模型权重", "A1:H5", "expert_models.png"],
  ["属性配置", "A1:G10", "expert_attributes.png"], ["价格工程先验", "A1:F9", "expert_cost.png"], ["耦合关系", "A1:G4", "expert_couplings.png"]]) {
  const preview = await workbook.render({ sheetName, range, scale: 1.1, format: "png" });
  await fs.writeFile(path.join(outputDir, filename), new Uint8Array(await preview.arrayBuffer()));
}
await (await SpreadsheetFile.exportXlsx(workbook)).save(outputPath);
console.log("Workbook saved: " + outputPath);
