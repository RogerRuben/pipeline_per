import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const payloadPath = process.argv[2];
const outputPath = process.argv[3];
const previewDir = process.argv[4];
if (!payloadPath || !outputPath || !previewDir) throw new Error("payload, output and preview directory are required");
const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
const wb = Workbook.create();
const names = Object.keys(payload.sheets);
for (const name of names) {
  const ws = wb.worksheets.add(name);
  const rows = payload.sheets[name];
  const cols = Math.max(...rows.map((r) => r.length));
  const normalized = rows.map((r) => Array.from({ length: cols }, (_, i) => r[i] ?? null));
  ws.getRangeByIndexes(0, 0, normalized.length, cols).values = normalized;
  ws.showGridLines = false;
  ws.freezePanes.freezeRows(1);
  const head = ws.getRangeByIndexes(0, 0, 1, cols);
  head.format = { fill: "#17365D", font: { bold: true, color: "#FFFFFF" }, rowHeight: 25 };
  const body = ws.getRangeByIndexes(1, 0, Math.max(1, normalized.length - 1), cols);
  body.format.borders = { preset: "inside", style: "thin", color: "#D9E2EC" };
  ws.getRangeByIndexes(0, 0, normalized.length, cols).format.autofitColumns();
  ws.getRangeByIndexes(0, 0, normalized.length, cols).format.autofitRows();
  ws.getRangeByIndexes(0, 0, normalized.length, cols).format.columnWidth = 18;
  if (name === "encoded_history") {
    ws.getRange(`B2:B${normalized.length}`).format.numberFormat = "0.0000";
    ws.getRange(`C2:I${normalized.length}`).format.numberFormat = "0.000";
    ws.tables.add(`A1:I${normalized.length}`, true, "EncodedHistoryTable");
  }
}
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });
for (const name of names) {
  const preview = await wb.render({ sheetName: name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(path.join(previewDir, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const inspect = await wb.inspect({ kind: "workbook,sheet,table", maxChars: 5000, tableMaxRows: 5, tableMaxCols: 10 });
await fs.writeFile(path.join(previewDir, "inspection.ndjson"), inspect.ndjson, "utf8");
const errors = await wb.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "formula error scan" });
await fs.writeFile(path.join(previewDir, "formula_errors.ndjson"), errors.ndjson, "utf8");
const exported = await SpreadsheetFile.exportXlsx(wb);
await exported.save(outputPath);
