/* Free, owner-deployed Apps Script bridge. See docs/google-sheets.md. */
function doGet() {
  return reply_({ok: true, service: 'Internship Radar application sync'});
}

function reply_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function equal_(a, b) {
  if (typeof a !== 'string' || !b || a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < b.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}

function safeText_(value) {
  const text = String(value == null ? '' : value).slice(0, 10000);
  return /^[=+@-]/.test(text) ? "'" + text : text;
}

function doPost(e) {
  let data;
  try {
    if (!e.postData || e.postData.contents.length > 400000) throw new Error();
    data = JSON.parse(e.postData.contents);
  } catch (_) { return reply_({ok: false}); }
  const properties = PropertiesService.getScriptProperties();
  if (!equal_(data.token, properties.getProperty('SYNC_TOKEN')))
    return reply_({ok: false});
  if (!Array.isArray(data.rows) || data.rows.length > 30 || data.rows.some(row =>
      !Array.isArray(row) || row.length !== 11 || !/^\d+$/.test(String(row[0])) ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/.test(row[10]) ||
      !['new','interested','applied','interview','offer','rejected'].includes(row[5]) ||
      !Number.isFinite(Date.parse(row[7])) || (row[6] && !Number.isFinite(Date.parse(row[6])))))
    return reply_({ok: false});
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return reply_({ok: false});
  try {
    const book = SpreadsheetApp.openById(properties.getProperty('SHEET_ID'));
    const sheet = book.getSheetByName('Applications');
    if (!sheet || sheet.getRange('A1').getValue() !== 'Posting ID')
      return reply_({ok: false});
    const count = Math.max(0, sheet.getLastRow() - 1);
    const existing = count ? sheet.getRange(2, 1, count, 11).getValues() : [];
    const index = new Map(existing.map((r, i) => [String(r[0]), {row: i + 2, version: String(r[10])}]));
    let nextRow = Math.max(2, sheet.getLastRow() + 1);
    const acknowledged = [];
    for (const row of data.rows) {
      const id = String(row[0]), version = row[10], previous = index.get(id);
      if (!previous || previous.version < version) {
        const target = previous ? previous.row : nextRow++;
        if (target > sheet.getMaxRows()) sheet.insertRowsAfter(sheet.getMaxRows(), 100);
        const values = row.map(safeText_);
        values[6] = row[6] ? new Date(row[6]) : '';
        values[7] = new Date(row[7]);
        sheet.getRange(target, 1, 1, 11).setValues([values]);
        sheet.getRange(target, 7, 1, 2).setNumberFormat('yyyy-mm-dd hh:mm:ss');
        sheet.getRange(target, 2, 1, 9).setWrap(true).setVerticalAlignment('top');
        index.set(id, {row: target, version: version});
      }
      acknowledged.push([id, version]);
    }
    book.setSpreadsheetTimeZone('Etc/UTC');
    sheet.setFrozenRows(1);
    sheet.setFrozenColumns(2);
    sheet.hideColumns(11);
    // Reserve filter space for future appends without changing the user's sort.
    if (!sheet.getFilter()) sheet.getRange(1, 1, sheet.getMaxRows(), 10).createFilter();
    SpreadsheetApp.flush();
    return reply_({ok: true, acknowledged: acknowledged});
  } catch (_) {
    return reply_({ok: false});
  } finally { lock.releaseLock(); }
}
