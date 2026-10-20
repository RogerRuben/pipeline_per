import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [payloadPath, outputPath] = process.argv.slice(2);
if (!payloadPath || !outputPath) throw new Error("Usage: payload.json output.xlsx");
const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const workbook = Workbook.create();
const data = workbook.worksheets.add("成品数据");
const matrix = [payload.labels, payload.field_ids, ...payload.rows];
data.getRangeByIndexes(0, 0, matrix.length, payload.labels.length).values = matrix;
data.showGridLines = false;
data.tabColor = "#24466C";
data.getRange(`A1:P${matrix.length}`).format.font = { name: "Arial", size: 10, color: "#243447" };
data.getRange(`A1:P${matrix.length}`).format.rowHeight = 23;
data.getRange("A1:P1").format = { fill: "#24466C", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center", rowHeight: 30 };
data.getRange("A2:P2").format = { fill: "#EDF2F7", font: { name: "Arial", size: 10, color: "#52677F" }, rowHeight: 25 };
data.getRange("A1:P2").format.wrapText = false;
data.freezePanes.freezeRows(2);
data.freezePanes.freezeColumns(2);
data.getRange(`C3:N${matrix.length}`).setNumberFormat("0.00");
data.getRange(`D3:E${matrix.length}`).setNumberFormat("@");
data.getRange(`C3:N${matrix.length}`).format.horizontalAlignment = "right";
data.getRange(`D3:E${matrix.length}`).format.horizontalAlignment = "center";
data.getRange(`O3:O${matrix.length}`).format.horizontalAlignment = "center";
data.getRange(`D3:E${matrix.length}`).dataValidation = { rule: { type: "list", values: ["有", "无"] } };
data.getRange(`O3:O${matrix.length}`).dataValidation = { rule: { type: "list", values: ["24V", "48V"] } };
const widths = [15, 26, 17, 22, 18, 22, 22, 22, 16, 23, 22, 22, 22, 23, 18, 18];
widths.forEach((width, i) => { data.getRangeByIndexes(0, i, matrix.length, 1).format.columnWidth = width; });

const attributes = workbook.worksheets.add("属性配置");
const preferences = { higher: "越大越好", lower: "越小越好", neutral: "中性" };
const configRows = [["英文属性代号", "效能偏好方向", "试用权重"], ...Object.entries(payload.effectiveness_config.attributes)
  .map(([key, value]) => [key, preferences[value.preference], value.weight])];
attributes.getRangeByIndexes(0, 0, configRows.length, 3).values = configRows;
attributes.getRange(`B2:B${configRows.length}`).dataValidation = { rule: { type: "list", values: Object.values(preferences) } };
attributes.getRange("E1").values = [["虚拟数据，权重用于软件试用"]];
attributes.getRange("E1").format.columnWidth = 42;

const couplings = workbook.worksheets.add("耦合关系");
const couplingRows = [["源属性代号", "目标属性代号", "工程系数(可选)", "工程偏置(可选)", "绝对容差(可选)"],
  ...payload.effectiveness_config.couplings.map(edge => [edge.source, edge.target, null, null, null])];
couplings.getRangeByIndexes(0, 0, couplingRows.length, 5).values = couplingRows;
couplings.getRange("G1").values = [["系数留空时，按指定关系拟合经验区间"]];
couplings.getRange("G2").values = [["统计关系不代表因果或工程可行性"]];
couplings.getRange("G1:G2").format.columnWidth = 48;
for (const [sheet, rows, columns] of [[attributes, configRows, 3], [couplings, couplingRows, 5]]) {
  sheet.showGridLines = false;
  sheet.tabColor = "#AABDD0";
  const range = sheet.getRangeByIndexes(0, 0, rows.length, columns);
  range.format.font = { name: "Arial", size: 10, color: "#243447" };
  range.format.rowHeight = 25;
  range.format.columnWidth = 27;
  sheet.getRangeByIndexes(0, 0, 1, columns).format = { fill: "#24466C", font: { name: "Arial", size: 10, bold: true, color: "#FFFFFF" }, rowHeight: 28 };
  sheet.freezePanes.freezeRows(1);
}
workbook.recalculate();
console.log((await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!", options: { useRegex: true, maxResults: 15 }, maxChars: 600 })).ndjson);
console.log((await workbook.inspect({ kind: "table", range: "成品数据!A1:P4", include: "values", tableMaxRows: 4, tableMaxCols: 16, maxChars: 1600 })).ndjson);
const outputDir = path.dirname(outputPath);
await fs.mkdir(outputDir, { recursive: true });
for (const [sheetName, range, filename] of [["成品数据", "A1:H8", "product_input_left.png"], ["成品数据", "I1:P8", "product_input_right.png"],
  ["属性配置", "A1:E12", "product_attributes.png"], ["耦合关系", "A1:G4", "product_couplings.png"]]) {
  const preview = await workbook.render({ sheetName, range, scale: 1.3, format: "png" });
  await fs.writeFile(path.join(outputDir, filename), new Uint8Array(await preview.arrayBuffer()));
}
const saved = await SpreadsheetFile.exportXlsx(workbook);
await saved.save(outputPath);
console.log(outputPath);
