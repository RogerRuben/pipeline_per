import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [inputDir, outputDir] = process.argv.slice(2);
await fs.mkdir(outputDir, { recursive: true });
function format(sheet, rows, cols, headers = 1) {
  sheet.showGridLines = false;
  const used = sheet.getRangeByIndexes(0, 0, rows, cols);
  used.format.font = { name: "Arial", size: 10, color: "#23364B" };
  used.format.rowHeight = 26;
  used.format.columnWidth = 26;
  used.format.verticalAlignment = "center";
  sheet.getRangeByIndexes(0, 0, headers, cols).format = {
    fill: "#25496D", font: { name: "Arial", size: 10, color: "#FFFFFF", bold: true }, rowHeight: 32,
  };
  sheet.freezePanes.freezeRows(headers);
}
for (const count of [50, 40]) {
  const payload = JSON.parse(await fs.readFile(path.join(inputDir, `fixture_10x${count}.json`), "utf8"));
  const workbook = Workbook.create();
  const data = workbook.worksheets.add("成品数据");
  const matrix = [payload.labels, payload.field_ids, ...payload.rows];
  data.getRangeByIndexes(0, 0, matrix.length, matrix[0].length).values = matrix;
  format(data, matrix.length, matrix[0].length, 2);
  data.freezePanes.freezeColumns(2);
  data.getRangeByIndexes(1, 0, 1, matrix[0].length).format = { fill: "#E9F0F7", font: { name: "Arial", size: 10, color: "#40556C" } };
  data.getRangeByIndexes(2, 2, 10, 1).setNumberFormat("0.00000");
  data.getRangeByIndexes(0, 0, 12, 1).format.columnWidth = 15;
  data.getRangeByIndexes(0, 1, 12, 1).format.columnWidth = 20;
  for (let i = 3; i < matrix[0].length; i++) data.getRangeByIndexes(2, i, 10, 1).setNumberFormat(payload.field_types[payload.field_ids[i]] === "number" ? "0.000" : "0");

  const ratings = workbook.worksheets.add("价格属性评分");
  const ratingRows = [["英文代号", "中文名称", "价格重要性评分", "属性类型", "专家编号", "价格属性组"],
    ...payload.field_ids.slice(3).map((key, i) => [key, payload.labels[i + 3], payload.price_scores[key], payload.field_types[key], "示例专家", payload.attribute_groups[key]])];
  ratings.getRangeByIndexes(0, 0, ratingRows.length, 6).values = ratingRows;
  format(ratings, ratingRows.length, 6);
  ratings.getRangeByIndexes(1, 2, count, 1).format.fill = "#FFF1CC";
  ratings.getRangeByIndexes(1, 2, count, 1).setNumberFormat("0");
  ratings.dataValidations.add({ range: `C2:C${count + 1}`, rule: { type: "whole", operator: "between", formula1: 1, formula2: 5 } });
  ratings.getRange("H1").values = [["填写原始1—5分，表达属性对价格的重要性。"]];
  ratings.getRange("H2").values = [["此表可删、留空或仅保留表头。未评分属性使用中性先验。"]];
  ratings.getRange("H1:H2").format.columnWidth = 78;

  const effect = workbook.worksheets.add("效能属性配置");
  const preference = { higher: "越大越好", lower: "越小越好", neutral: "中性" };
  const effectRows = [["英文属性代号", "效能偏好方向", "效能权重", "中文属性名"],
    ...payload.field_ids.slice(3).map((key, i) => [key, preference[payload.effectiveness_config.attributes[key].preference], payload.effectiveness_config.attributes[key].weight, payload.labels[i + 3]])];
  effect.getRangeByIndexes(0, 0, effectRows.length, 4).values = effectRows;
  format(effect, effectRows.length, 4);
  effect.getRangeByIndexes(1, 2, count, 1).setNumberFormat("0.0000");
  effect.getRange("F1").values = [["独立的效能偏好配置。价格1—5分不会自动用于效能。"]];
  effect.getRange("F1").format.columnWidth = 78;

  const coupling = workbook.worksheets.add("耦合关系");
  coupling.getRange("A1:E1").values = [["源属性代号", "目标属性代号", "工程系数", "工程偏置", "绝对容差"]];
  format(coupling, 1, 5);
  coupling.getRange("G1").values = [["无已知耦合时保持空表。程序不自动推断物理耦合。"]];
  coupling.getRange("G1").format.columnWidth = 82;

  const notes = workbook.worksheets.add("填写说明");
  const rows = [["操作", "说明"], ["样本", payload.source_note],
    ["成品数据", "第一行中文名，第二行唯一英文代号，第三行起为记录。价格注明单位。"],
    ["价格属性评分", "只填写1—5分。数字编码的控制方式、材料等应声明为enum。"],
    ["可选工作表", "价格评分、效能配置、耦合关系均可为空，只需成品数据即可训练。"],
    ["首次训练", "替换input/products.xlsx后运行TRAIN.bat。"],
    ["日常使用", "运行START.bat，载入已保存模型，不重新训练。"],
    ["重新训练", "运行RETRAIN.bat创建新版本；旧版本保留。"],
    ["导出运行包", "运行EXPORT.bat，把当前已训练版本连同环境一起导出。"]];
  notes.getRangeByIndexes(0, 0, rows.length, 2).values = rows;
  format(notes, rows.length, 2);
  notes.getRangeByIndexes(0, 0, rows.length, 1).format.columnWidth = 20;
  notes.getRangeByIndexes(0, 1, rows.length, 1).format.columnWidth = 100;

  workbook.recalculate();
  const inspected = await workbook.inspect({ kind: "table", range: "价格属性评分!A1:F6", include: "values,formulas", tableMaxRows: 6, tableMaxCols: 6 });
  await fs.writeFile(path.join(outputDir, `inspection_${count}.jsonl`), inspected.ndjson);
  const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!", options: { useRegex: true, maxResults: 10 }, maxChars: 1000 });
  await fs.writeFile(path.join(outputDir, `errors_${count}.jsonl`), errors.ndjson);
  for (const [sheetName, range, code] of [["成品数据", "A1:H7", "data"], ["价格属性评分", "A1:F9", "ratings"], ["效能属性配置", "A1:D8", "effect"], ["耦合关系", "A1:G2", "coupling"], ["填写说明", "A1:B9", "notes"]]) {
    const preview = await workbook.render({ sheetName, range, scale: 1.2, format: "png" });
    await fs.writeFile(path.join(outputDir, `${code}_${count}.png`), new Uint8Array(await preview.arrayBuffer()));
  }
  await (await SpreadsheetFile.exportXlsx(workbook)).save(path.join(outputDir, `virtual_10x${count}_r4.xlsx`));
  console.log(`Saved 10x${count} R4 workbook`);
}
