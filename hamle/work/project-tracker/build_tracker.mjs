import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const here = path.dirname(new URL(import.meta.url).pathname);
const data = JSON.parse(await fs.readFile(path.join(here, 'project-plan.json'), 'utf8'));
const out = path.resolve(here, '../../outputs/hamle-yeni-mac-devir-2026-10-10');
await fs.mkdir(out, { recursive: true });
const previewDir = path.join(here, 'xlsx-qa-handoff');
await fs.mkdir(previewDir, { recursive: true });
const wb = Workbook.create();
const summary = wb.worksheets.add('Özet');
const tasks = wb.worksheets.add('İş Listesi');
const inputs = wb.worksheets.add('Girdiler ve Kararlar');
const handoff = wb.worksheets.add('Yeni Mac Devir');
const navy = '#172A46', blue = '#EAF0F7', ink = '#24364B', amber = '#FFF3CB', green = '#DDEDE5', red = '#FBE4E3', grey = '#697A8C';
const fmt = (sheet, range, f) => { sheet.getRange(range).format = f; };
const val = (sheet, cell, value) => { sheet.getRange(cell).values = [[value]]; };
const formula = (sheet, cell, value) => { sheet.getRange(cell).formulas = [[value]]; };
const setWidth = (sheet, widths) => Object.entries(widths).forEach(([col, w]) => { sheet.getRange(`${col}1:${col}200`).format.columnWidth = w; });
function base(sheet, address) {
  sheet.showGridLines = false;
  fmt(sheet, address, { font: { name: 'Arial', size: 10, color: ink }, verticalAlignment: 'center', rowHeight: 24 });
}
function title(sheet, value, edge) {
  val(sheet, 'A2', value);
  fmt(sheet, 'A2', { font: { name: 'Arial', size: 16, bold: true, color: navy } });
  fmt(sheet, `A3:${edge}3`, { borders: { bottom: { style: 'thin', color: navy } } });
}
function header(sheet, range) {
  fmt(sheet, range, { fill: navy, font: { name: 'Arial', size: 10, bold: true, color: '#FFFFFF' }, horizontalAlignment: 'center', verticalAlignment: 'center', wrapText: true, rowHeight: 34, borders: { insideVertical: { style: 'thin', color: '#FFFFFF' } } });
}
const join = x => Array.isArray(x) ? x.join(', ') : (x ?? '');
const phases = data.phases;
const list = data.tasks;
const requests = data.inputs ?? data.decisions ?? [];
const taskStart = 7, taskEnd = taskStart + list.length - 1;
const phaseStart = 14, phaseEnd = phaseStart + phases.length - 1, totalRow = phaseEnd + 1;
const phaseName = new Map(phases.map(x => [x.id, x.name]));
const dateValue = x => /^\d{4}-\d{2}-\d{2}$/.test(x ?? '') ? Date.parse(`${x}T00:00:00Z`) / 86400000 + 25569 : null;
const taskId = x => x.id;
const taskPhase = x => x.phaseId ?? x.phase;
const done = x => x.done;
const humanEvidence = x => join(x.evidence ?? x.source ?? '');
const safeText = x => typeof x === 'object' && x !== null ? JSON.stringify(x) : (x ?? '');

assert.equal(new Set(list.map(taskId)).size, list.length, 'Duplicate task IDs');
assert.equal(list.reduce((sum, x) => sum + x.weight, 0), 100, 'Weights must total 100');
assert(list.every(x => x.done === 0 || x.done === 1), 'Every task needs binary completion');
assert(list.every(x => phaseName.has(taskPhase(x))), 'All task phases must exist');
for (const p of phases) {
  if (typeof p.weight === 'number') assert.equal(list.filter(x => taskPhase(x) === p.id).reduce((sum, x) => sum + x.weight, 0), p.weight, `Phase ${p.id} task weights must reconcile`);
}

base(tasks, `A1:O${taskEnd}`);
tasks.tabColor = '#496786';
setWidth(tasks, { A: 11, B: 49, C: 29, D: 12, E: 25, F: 20, G: 14, H: 11, I: 27, J: 67, K: 15, L: 42, M: 3, N: 75 });
title(tasks, 'Hamle iş listesi', 'L');
val(tasks, 'A4', 'Sarı alanlar güncellenir. Tamamlandı: kabul ölçütü karşılandıysa 1, diğer durumlarda 0.');
val(tasks, 'A5', 'Sorumlular öneridir; kabulü ayrıca doğrulanır. Hedef tarih boşsa henüz taahhüt verilmemiştir.');
fmt(tasks, 'A4:L5', { font: { name: 'Arial', size: 10, color: grey }, rowHeight: 22 });
tasks.getRange('A6:L6').values = [['Kod', 'İş / aksiyon', 'İş paketi', 'Öncelik', 'Sorumlu', 'Çalışma durumu', 'Tamamlandı 0 / 1', 'Ağırlık puanı', 'Önce bitmesi gereken', 'Tamamlanma / kabul ölçütü', 'Hedef tarih', 'Güncel not / sonraki adım']];
val(tasks, 'N6', 'Kayıt / kanıt');
val(tasks, 'M6', ' ');
header(tasks, 'A6:L6');
fmt(tasks, 'N6', { font: { bold: true, color: grey }, rowHeight: 34 });
tasks.getRange(`A${taskStart}:L${taskEnd}`).values = list.map(x => [
  x.id, x.title ?? x.action, `${taskPhase(x)} · ${phaseName.get(taskPhase(x))}`,
  x.priority ?? '', x.owner ?? '', x.status ?? (x.done ? 'Tamamlandı' : 'Başlanmadı'),
  x.done, x.weight, join(x.dependencies ?? x.dependencyIds), x.acceptance ?? '',
  dateValue(x.targetDate ?? x.dueDate), x.nextAction ?? x.note ?? ''
]);
tasks.getRange(`N${taskStart}:N${taskEnd}`).values = list.map(x => [humanEvidence(x)]);
fmt(tasks, `A${taskStart}:L${taskEnd}`, { fill: '#FFFFFF', wrapText: true, verticalAlignment: 'top' });
fmt(tasks, `N${taskStart}:N${taskEnd}`, { wrapText: true, verticalAlignment: 'top', font: { color: grey, name: 'Arial', size: 10 } });
for (let i = 0; i < list.length; i++) {
  const r = taskStart + i;
  if (i % 2 === 1) fmt(tasks, `A${r}:L${r}`, { fill: '#F4F7FA' });
  const x = list[i];
  const maxLines = Math.max(Math.ceil(String(x.title ?? x.action ?? '').length / 46), Math.ceil(String(x.acceptance ?? '').length / 67), Math.ceil(String(x.nextAction ?? x.note ?? '').length / 40), Math.ceil(humanEvidence(x).length / 72), 2);
  tasks.getRange(`A${r}:N${r}`).format.rowHeight = Math.max(46, Math.min(240, maxLines * 14 + 12));
}
fmt(tasks, `E${taskStart}:G${taskEnd}`, { fill: amber });
fmt(tasks, `K${taskStart}:L${taskEnd}`, { fill: amber });
fmt(tasks, `G${taskStart}:H${taskEnd}`, { horizontalAlignment: 'right' });
tasks.getRange(`G${taskStart}:G${taskEnd}`).setNumberFormat('0');
tasks.getRange(`H${taskStart}:H${taskEnd}`).setNumberFormat('0.0');
tasks.getRange(`K${taskStart}:K${taskEnd}`).setNumberFormat('yyyy-mm-dd');
tasks.getRange(`F${taskStart}:F${taskEnd}`).dataValidation = { rule: { type: 'list', values: [...new Set(['Tamamlandı', 'Devam ediyor', 'Bekliyor', 'Başlanmadı', 'Kontrol bekliyor', ...list.map(x => x.status).filter(Boolean)])] } };
tasks.dataValidations.add({ range: `G${taskStart}:G${taskEnd}`, rule: { type: 'whole', operator: 'between', formula1: 0, formula2: 1 } });
tasks.getRange(`G${taskStart}:G${taskEnd}`).conditionalFormats.add('cellIs', { operator: 'equal', formula: 1, format: { fill: green, font: { color: '#285641' } } });
tasks.getRange(`F${taskStart}:F${taskEnd}`).conditionalFormats.add('containsText', { text: 'Bekliyor', format: { fill: '#FCE5CF', font: { color: '#805019' } } });
const taskTable = tasks.tables.add(`A6:N${taskEnd}`, true, 'HamleTasks');
taskTable.style = 'TableStyleLight1';
taskTable.showFilterButton = true;
fmt(tasks, `M6:N${taskEnd}`, { fill: '#FFFFFF' });
tasks.getRange('M6').setNumberFormat(';;;');
tasks.freezePanes.freezeRows(6);
tasks.freezePanes.freezeColumns(2);

base(summary, 'A1:H40');
summary.tabColor = navy;
setWidth(summary, { A: 11, B: 42, C: 15, D: 16, E: 17, F: 14, G: 14, H: 14 });
title(summary, 'Gamechangers Hamle proje takibi', 'H');
val(summary, 'A4', 'Belge tarihi');
val(summary, 'B4', dateValue(data.documentDate ?? data.asOf ?? '2026-10-10'));
summary.getRange('B4').setNumberFormat('yyyy-mm-dd');
fmt(summary, 'B4', { horizontalAlignment: 'center' });
val(summary, 'C4', 'Kanıt tarihi'); val(summary, 'D4', '09.10 · 23.59');
val(summary, 'E4', 'V1 başlangıcı'); val(summary, 'F4', '22 puan / 15 iş');
val(summary, 'A6', 'Tamamlanan');
formula(summary, 'B6', `=D${totalRow}/C${totalRow}`);
val(summary, 'C6', 'Kalan');
formula(summary, 'D6', '=1-B6');
val(summary, 'E6', 'Açık iş');
formula(summary, 'F6', `=SUM(G${phaseStart}:G${phaseEnd})`);
val(summary, 'G6', 'Biten iş');
formula(summary, 'H6', `=SUM(H${phaseStart}:H${phaseEnd})`);
fmt(summary, 'A6:H6', { rowHeight: 34, fill: blue });
fmt(summary, 'B6', { font: { name: 'Arial', size: 16, bold: true, color: navy } });
fmt(summary, 'D6', { font: { name: 'Arial', size: 16, bold: true, color: navy } });
for (const cell of ['B6', 'D6', 'F6', 'H6']) fmt(summary, cell, { horizontalAlignment: 'center' });
summary.getRange('B6').setNumberFormat('0%');
summary.getRange('D6').setNumberFormat('0%');
val(summary, 'A8', 'Canlı ajan');
val(summary, 'B8', data.liveRoles ?? 0);
val(summary, 'C8', 'Hedef ajan');
val(summary, 'D8', data.targetRoles ?? 4);
val(summary, 'E8', 'Cloud çalışması');
val(summary, 'F8', 'Henüz yok');
fmt(summary, 'B8', { fill: amber });
fmt(summary, 'B8', { horizontalAlignment: 'center' });
fmt(summary, 'D8', { horizontalAlignment: 'center' });
fmt(summary, 'F8', { fill: amber });
summary.dataValidations.add({ range: 'B8', rule: { type: 'whole', operator: 'between', formula1: 0, formula2: data.targetRoles ?? 4 } });
summary.getRange('F8').dataValidation = { rule: { type: 'list', values: ['Henüz yok', 'İç test', 'Sınırlı canlı', 'Canlı'] } };
val(summary, 'A10', 'Bu belge için önerilen yönetim başlangıcı: kabulü tamamlanan işlerin 100 puan içindeki ağırlık toplamı.');
val(summary, 'A11', 'Süre, yapay zekâ öğrenme seviyesi veya canlıda çalışma yüzdesi değildir. Açık işler kısmi puan almaz.');
fmt(summary, 'A10:H11', { font: { name: 'Arial', size: 10, color: grey }, rowHeight: 23 });
summary.getRange('A13:H13').values = [['Kod', 'İş paketi', 'Toplam puan', 'Tamam puan', 'Paket ilerleme', 'Kalan puan', 'Açık iş', 'Biten iş']];
header(summary, 'A13:H13');
phases.forEach((p, i) => {
  const r = phaseStart + i;
  val(summary, `A${r}`, p.id);
  val(summary, `B${r}`, p.name);
  const phaseLabel = `${p.id} · ${p.name}`;
  formula(summary, `C${r}`, `=SUMIFS('İş Listesi'!$H$${taskStart}:$H$${taskEnd},'İş Listesi'!$C$${taskStart}:$C$${taskEnd},"${phaseLabel}")`);
  formula(summary, `D${r}`, `=SUMIFS('İş Listesi'!$H$${taskStart}:$H$${taskEnd},'İş Listesi'!$C$${taskStart}:$C$${taskEnd},"${phaseLabel}",'İş Listesi'!$G$${taskStart}:$G$${taskEnd},1)`);
  formula(summary, `E${r}`, `=D${r}/C${r}`);
  formula(summary, `F${r}`, `=C${r}-D${r}`);
  formula(summary, `H${r}`, `=COUNTIFS('İş Listesi'!$C$${taskStart}:$C$${taskEnd},"${phaseLabel}",'İş Listesi'!$G$${taskStart}:$G$${taskEnd},1)`);
  formula(summary, `G${r}`, `=COUNTIFS('İş Listesi'!$C$${taskStart}:$C$${taskEnd},"${phaseLabel}")-H${r}`);
  if (i % 2 === 1) fmt(summary, `A${r}:H${r}`, { fill: '#F4F7FA' });
});
fmt(summary, `A${phaseStart}:H${phaseEnd}`, { rowHeight: 32 });
summary.getRange(`E${phaseStart}:E${phaseEnd}`).setNumberFormat('0.0%');
summary.getRange(`C${phaseStart}:D${totalRow}`).setNumberFormat('0.0');
summary.getRange(`F${phaseStart}:F${totalRow}`).setNumberFormat('0.0');
val(summary, `B${totalRow}`, 'Toplam');
for (const c of ['C', 'D', 'F', 'G', 'H']) formula(summary, `${c}${totalRow}`, `=SUM(${c}${phaseStart}:${c}${phaseEnd})`);
formula(summary, `E${totalRow}`, `=D${totalRow}/C${totalRow}`);
summary.getRange(`E${totalRow}`).setNumberFormat('0%');
fmt(summary, `A${totalRow}:H${totalRow}`, { fill: blue, font: { name: 'Arial', size: 10, bold: true, color: navy }, rowHeight: 32, borders: { top: { style: 'thin', color: navy } } });
const notesRow = totalRow + 3;
val(summary, `A${notesRow}`, 'Güncelleme');
val(summary, `B${notesRow}`, 'İş Listesi: durum, 0/1, sorumlu, hedef tarih ve not alanlarını güncelleyin.');
val(summary, `B${notesRow + 1}`, '1 yazmadan önce ilgili kabul ölçütünü ve kanıtı kontrol edin.');
val(summary, `B${notesRow + 2}`, 'Yeni iş eklemek kapsamı değiştirir. Ağırlıkları ve özet aralıklarını birlikte güncelleyin.');
val(summary, `B${notesRow + 3}`, 'Sarı canlı ajan ve Cloud alanlarını yalnız gerçek çalışma kanıtıyla güncelleyin.');
val(summary, `B${notesRow + 4}`, 'Sorumlular öneridir. Yerel kayıtlar incelendi; bu dosya yeni bir sağlayıcı testi değildir.');
val(summary, `B${notesRow + 5}`, 'Açık işlerde kanıt sütunu tamamlanmayı değil, mevcut eksiği veya gereksinimi gösterir.');
val(summary, `B${notesRow + 6}`, 'Bağımlılıklar açılacak marka ve hizmete uygulanır. Eksik marka, hazır markayı durdurmaz.');
val(summary, `B${notesRow + 7}`, 'A04 / B10 / C05 satırlarında 1, tüm portföyün kabulünü ölçer.');
val(summary, `B${notesRow + 8}`, 'İlk sınırlı canlı için marka ve hizmetin sekiz koşula ait alt kabul kanıtı IN13 içinde tutulur.');
fmt(summary, `A${notesRow}:H${notesRow + 8}`, { font: { name: 'Arial', size: 10, color: grey }, rowHeight: 22 });

const inputStart = 7, inputEnd = Math.max(inputStart, inputStart + requests.length - 1);
base(inputs, `A1:J${inputEnd}`);
inputs.tabColor = '#BC9553';
setWidth(inputs, { A: 12, B: 55, C: 25, D: 27, E: 43, F: 51, G: 30, H: 53, I: 3, J: 68 });
title(inputs, 'Gerekli girdiler ve kararlar', 'H');
val(inputs, 'A4', 'Yanıt / karar alanı sarıdır. Bilinmeyen bilgi için tahmin girilmez.');
val(inputs, 'A5', 'Bu liste kurulum ve canlıya geçişte hangi bilginin kimden beklendiğini gösterir.');
fmt(inputs, 'A4:H5', { font: { name: 'Arial', size: 10, color: grey }, rowHeight: 22 });
inputs.getRange('A6:H6').values = [['Kod', 'Gerekli bilgi / karar', 'Sorumlu', 'Gerekli aşama', 'Mevcut durum', 'Yanıt / karar', 'Etkilenen işler', 'Tamamlanma ölçütü']];
header(inputs, 'A6:H6');
val(inputs, 'J6', 'Kayıt / kaynak');
val(inputs, 'I6', ' ');
fmt(inputs, 'J6', { font: { name: 'Arial', size: 10, bold: true, color: grey } });
if (requests.length) {
  inputs.getRange(`A${inputStart}:H${inputEnd}`).values = requests.map(x => [x.id, x.title ?? x.question ?? '', x.owner ?? '', x.neededBy ?? x.phase ?? '', x.status ?? x.current ?? '', x.answer ?? x.decision ?? '', join(x.taskIds ?? x.relatedTasks ?? x.impact), x.acceptance ?? x.required ?? '']);
  inputs.getRange(`J${inputStart}:J${inputEnd}`).values = requests.map(x => [humanEvidence(x)]);
}
fmt(inputs, `A${inputStart}:H${inputEnd}`, { fill: '#FFFFFF', wrapText: true, verticalAlignment: 'top' });
fmt(inputs, `J${inputStart}:J${inputEnd}`, { wrapText: true, verticalAlignment: 'top', font: { name: 'Arial', size: 10, color: grey } });
for (let i = 0; i < requests.length; i++) {
  const r = inputStart + i;
  if (i % 2 === 1) fmt(inputs, `A${r}:H${r}`, { fill: '#F4F7FA' });
  const x = requests[i];
  const lines = Math.max(Math.ceil(String(x.title ?? x.question ?? '').length / 50), Math.ceil(String(x.current ?? x.status ?? '').length / 40), Math.ceil(String(x.answer ?? '').length / 48), Math.ceil(humanEvidence(x).length / 62), Math.ceil(String(x.acceptance ?? x.required ?? '').length / 50), 3);
  inputs.getRange(`A${r}:J${r}`).format.rowHeight = Math.max(60, Math.min(180, lines * 14 + 12));
}
fmt(inputs, `F${inputStart}:F${inputEnd}`, { fill: amber });
const inputTable = inputs.tables.add(`A6:J${inputEnd}`, true, 'HamleInputs');
inputTable.style = 'TableStyleLight1';
inputTable.showFilterButton = true;
fmt(inputs, `I6:J${inputEnd}`, { fill: '#FFFFFF' });
inputs.getRange('I6').setNumberFormat(';;;');
inputs.freezePanes.freezeRows(6);
inputs.freezePanes.freezeColumns(2);


base(handoff, 'A1:D22');
handoff.tabColor = '#357A71';
setWidth(handoff, { A: 25, B: 66, C: 40, D: 92 });
title(handoff, 'Yeni Mac ve yeni konuşmaya devir', 'D');
val(handoff, 'A4', '10 Ekim 2026 devir sürümü; doğrulanmış işlem durumu 9 Ekim 2026 saat 23.59.');
val(handoff, 'A5', 'Bu belgeler bilgi taşır; yazılımı, erişim sırlarını ve hesap bağlantılarını taşımaz.');
fmt(handoff, 'A4:D5', { font: { name: 'Arial', size: 10, color: grey }, rowHeight: 23 });
handoff.getRange('A7:D7').values = [['Kalem', 'Kaynak veya konum', 'Mevcut durum', 'Yeni Mac adımı']];
header(handoff, 'A7:D7');
handoff.getRange(`A8:D${7 + data.handoff.rows.length}`).values = data.handoff.rows;
fmt(handoff, `A8:D${7 + data.handoff.rows.length}`, { wrapText: true, verticalAlignment: 'top', rowHeight: 72 });
data.handoff.rows.forEach((_, i) => { if (i % 2) fmt(handoff, `A${8+i}:D${8+i}`, { fill: '#F4F7FA' }); });
handoff.freezePanes.freezeRows(7);
val(handoff, 'A20', 'Önceki başlangıç'); val(handoff, 'B20', 'V1: 22 puan / 15 tamam iş. Özgün dosyalar korundu.');
val(handoff, 'A21', 'Güncel durum'); val(handoff, 'B21', `${data.currentSummary.completedWeight} puan / ${data.currentSummary.completedTasks} tamam iş / 0 canlı ajan.`);
val(handoff, 'A22', 'Sonraki sıra'); val(handoff, 'B22', 'Cihaz bağlantıları → model/bütçe → posta yetkisi ve Cloud → iç demo → gölge kabul.');
fmt(handoff, 'A20:D22', { rowHeight: 25, font: { name: 'Arial', size: 10, color: grey } });

wb.recalculate();
const expected = list.reduce((sum, x) => sum + x.weight * x.done, 0) / 100;
const getProgress = () => summary.getRange('B6').values[0][0];
assert.equal(getProgress(), expected, 'Progress must match independent weighted sum');
const openIndex = list.findIndex(x => x.done === 0);
const openRow = taskStart + openIndex;
val(tasks, `G${openRow}`, 1);
wb.recalculate();
assert(Math.abs(getProgress() - (expected + list[openIndex].weight / 100)) < 1e-9, 'Changing completion must update summary');
val(tasks, `G${openRow}`, 0);
const firstDone = list.findIndex(x => x.done === 1);
val(tasks, `G${taskStart + firstDone}`, null);
wb.recalculate();
assert(Math.abs(getProgress() - (expected - list[firstDone].weight / 100)) < 1e-9, 'Blank completion must not count as done');
val(tasks, `G${taskStart + firstDone}`, 1);
wb.recalculate();
assert.equal(getProgress(), expected, 'Test inputs must be restored');
const summaryInspect = await wb.inspect({ kind: 'table', range: `Özet!A13:H${totalRow}`, include: 'values,formulas', tableMaxRows: 15, tableMaxCols: 8, maxChars: 12000 });
const errors = await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options: { useRegex: true, maxResults: 300 }, summary: 'Final formula error scan' });
await fs.writeFile(path.join(previewDir, 'formula-verification.json'), JSON.stringify({ expectedProgress: expected, actualProgress: getProgress(), taskCount: list.length, completedTasks: list.filter(x => x.done).length, inputChangeTest: 'passed and restored', blankCompletionTest: 'passed and restored', summary: summaryInspect.ndjson, errors: errors.ndjson }, null, 2));
console.log('RESULT', JSON.stringify({ progress: getProgress(), taskCount: list.length, completedTasks: list.filter(x => x.done).length, errors: errors.ndjson }));
for (const [sheetName, range, name] of [
  ['Özet', `A1:H${notesRow + 8}`, 'ozet'],
  ['Yeni Mac Devir', 'A1:D22', 'yeni-mac-devir'],
  ['İş Listesi', 'A1:H13', 'is-listesi-sol'],
  ['İş Listesi', 'I6:N13', 'is-listesi-detay'],
  ['İş Listesi', 'A16:H23', 'gmail-basecamp'],
  ['İş Listesi', 'I16:N23', 'gmail-basecamp-detail'],
  ['İş Listesi', 'I24:N26', 'learning-correction'],
  ['Girdiler ve Kararlar', 'A1:F12', 'girdiler-sol'],
  ['Girdiler ve Kararlar', 'G6:J12', 'girdiler-detay'],
]) {
  if (process.argv.includes('--learning-only') && name !== 'learning-correction') continue;
  if (process.argv.includes('--summary-only') && sheetName !== 'Özet') continue;
  const preview = await wb.render({ sheetName, range, scale: 1.2, format: 'png' });
  await fs.writeFile(path.join(previewDir, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}
const output = await SpreadsheetFile.exportXlsx(wb);
const file = path.join(out, 'Hamle_Proje_Takip_2026-10-10_Yeni_Mac_Devir.xlsx');
await output.save(file);
console.log('EXPORTED', file);
