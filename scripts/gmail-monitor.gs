/** Owner-only Gmail bridge. Enable the Gmail v1 advanced service and readonly scope.
 * Configure RADAR_URL and RADAR_TOKEN in Script Properties, then run connectRadar.
 * Does not send, delete, label or mark mail read. No email body or token is logged.
 */
function radarCall(payload) {
  const p = PropertiesService.getScriptProperties();
  const url = p.getProperty('RADAR_URL');
  if (!/^https:\/\/internship-alerts-1412\.onrender\.com$/.test(url || '')) throw new Error('Check RADAR_URL.');
  const response = UrlFetchApp.fetch(url + '/integrations/mail', {
    method: 'post', contentType: 'application/json',
    headers: {Authorization: 'Bearer ' + p.getProperty('RADAR_TOKEN')},
    payload: JSON.stringify(payload), muteHttpExceptions: true
  });
  if (response.getResponseCode() !== 200) throw new Error('Radar connection failed; check the app mailbox settings.');
  return JSON.parse(response.getContentText());
}
function connectRadar() {
  const email = Gmail.Users.getProfile('me').emailAddress;
  radarCall({action:'config', email:email}); // Rejects the wrong signed-in account.
  ScriptApp.getProjectTriggers().filter(t=>t.getHandlerFunction()==='checkRadarMail').forEach(t=>ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('checkRadarMail').timeBased().everyMinutes(5).create();
  checkRadarMail();
}
function textPart(part) {
  if (part.mimeType === 'text/plain' && part.body && part.body.data) {
    return Utilities.newBlob(Utilities.base64DecodeWebSafe(part.body.data)).getDataAsString();
  }
  return (part.parts || []).map(textPart).filter(Boolean).join('\n');
}
function checkRadarMail() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const email = Gmail.Users.getProfile('me').emailAddress;
    const config = radarCall({action:'config', email:email});
    const companies = [...new Set(config.applications.map(p=>p.company.replace(/["\\{}]/g,' ').trim()).filter(c=>c.length>=3))];
    if (!companies.length) return;
    const props = PropertiesService.getScriptProperties();
    const offset = Number(props.getProperty('RADAR_OFFSET') || 0) % companies.length;
    const batch = companies.slice(offset, offset + 12);
    const query = 'newer_than:30d -in:spam -in:trash -in:sent -in:drafts {' + batch.map(c=>'"'+c+'"').join(' ') + '}';
    const cursorQuery = props.getProperty('RADAR_QUERY');
    const pageToken = cursorQuery === query ? props.getProperty('RADAR_PAGE') : null;
    const options = {q:query, maxResults:30};
    if (pageToken) options.pageToken = pageToken;
    let page;
    try { page = Gmail.Users.Messages.list('me', options); }
    catch (e) { props.deleteProperty('RADAR_PAGE'); throw new Error('Gmail read failed; retry on the next scheduled check.'); }
    const messages = (page.messages || []).map(m=>{
      const data = Gmail.Users.Messages.get('me', m.id, {format:'full'});
      const header = name => (data.payload.headers || []).find(h=>h.name.toLowerCase()===name)?.value || '';
      return {id:data.id, timestamp:data.internalDate, subject:header('subject').slice(0,300),
        sender:header('from').slice(0,300), text:(textPart(data.payload) || data.snippet || '').slice(0,6000)};
    });
    // Keep UTF-8 batches below the app's byte limit, including non-ASCII mail.
    for (let i=0; i<messages.length; i+=8) radarCall({action:'events', email:email, messages:messages.slice(i,i+8)});
    if (page.nextPageToken) {
      props.setProperties({RADAR_QUERY:query, RADAR_PAGE:page.nextPageToken});
    } else {
      props.deleteProperty('RADAR_PAGE');
      props.setProperty('RADAR_OFFSET',String((offset+12)%companies.length));
    }
  } finally { lock.releaseLock(); }
}
function disconnectRadar() {
  ScriptApp.getProjectTriggers().filter(t=>t.getHandlerFunction()==='checkRadarMail').forEach(t=>ScriptApp.deleteTrigger(t));
  PropertiesService.getScriptProperties().deleteProperty('RADAR_TOKEN');
}
