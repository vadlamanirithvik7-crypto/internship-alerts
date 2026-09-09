const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('webhook authenticates, upserts IDs, and cannot overwrite a newer stage', () => {
  const rows = [['Posting ID']];
  let opened = 0, locked = false;
  const sheet = {
    getLastRow: () => rows.length, getMaxRows: () => 1000,
    setFrozenRows() {}, setFrozenColumns() {}, hideColumns() {}, getFilter: () => true,
    getRange(row, col, count, cols) {
      if (row === 'A1') return {getValue: () => 'Posting ID'};
      return {getValues: () => rows.slice(row-1,row-1+count).map(r=>r.slice(col-1,col-1+cols)),
        setValues: values => values.forEach((r,i)=>rows[row-1+i]=r),
        setNumberFormat() {}, setWrap() {return this;}, setVerticalAlignment() {}};
    }
  };
  const context = vm.createContext({
    ContentService: {MimeType: {JSON: 'json'}, createTextOutput: text => ({setMimeType: () => JSON.parse(text)})},
    PropertiesService: {getScriptProperties: () => ({getProperty: key => ({SYNC_TOKEN:'secret', SHEET_ID:'owned-sheet'})[key]})},
    SpreadsheetApp: {openById: id => {assert.equal(id,'owned-sheet'); opened++; return {getSheetByName: () => sheet, setSpreadsheetTimeZone() {}};}, flush() {}},
    LockService: {getScriptLock: () => ({tryLock: () => {locked=true;return true;}, releaseLock: () => {locked=false;}})},
  });
  vm.runInContext(fs.readFileSync('scripts/google-sheets.gs','utf8'), context);
  const row = ['42','Example','Intern','Austin, TX','Summer 2027','applied','2026-09-08T00:00:00Z','2026-09-08T00:00:00.000001Z','https://example.com','=1+1','2026-09-08T00:00:00.000001Z'];
  const send = (token, data) => context.doPost({postData:{contents:JSON.stringify({token,rows:data})}});
  assert.equal(send('wrong',[row]).ok,false);
  assert.equal(opened,0);
  assert.equal(send('secret',[row]).ok,true);
  assert.equal(send('secret',[row]).ok,true);
  assert.equal(rows.length,2);
  assert.equal(rows[1][9],"'=1+1");
  const newer = [...row]; newer[5]='interview'; newer[7]=newer[10]='2026-09-08T00:00:00.000002Z';
  assert.equal(send('secret',[newer]).ok,true);
  assert.equal(send('secret',[row]).ok,true);
  assert.equal(rows[1][5],'interview');
  assert.equal(rows.length,2);
  assert.equal(locked,false);
});
