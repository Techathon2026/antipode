import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { Workbook } from '@oai/artifact-tool';

const root = 'D:/documents/techathon2026';
const work = path.join(root, 'annotation_work/merge_20261016');
const target = path.join(root, 'data/email_headers_20261016_T01-T08_labeled.csv');
const source = path.join(root, 'data/emails.csv');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const targetBytes = await fs.readFile(target);
const sourceBytes = await fs.readFile(source);
const originalWb = await Workbook.fromCSV(targetBytes.toString('utf8').replace(/^\uFEFF/, ''), {sheetName:'原标注'});
const incomingWb = await Workbook.fromCSV(sourceBytes.toString('utf8').replace(/^\uFEFF/, ''), {sheetName:'新邮件'});
const read = wb => wb.worksheets.getItemAt(0).getUsedRange().values.map(row => row.map(v => v == null ? '' : String(v)));
const original = read(originalWb);
const incoming = read(incomingWb);
const fields = original[0];
const objects = matrix => matrix.slice(1).filter(row => row.some(Boolean)).map(row => Object.fromEntries(matrix[0].map((key,i)=>[key,row[i] || ''])));
const oldRows = objects(original);
const newRows = objects(incoming);
const clean = text => text.replace(/\\r\\n|\\n|\\r/g, ' ').replace(/[\r\n\t]+/g, ' ').replace(/\s+/g, ' ').trim();
const keyFor = subject => clean(subject).normalize('NFKC').toLowerCase().replace(/^(?:(?:re|fw|fwd|回复|答复|转发)\s*:\s*)+/, '').replace(/\s+/g, '');

const retained = [];
const seenSubjects = new Map();
const seenMessages = new Map();
const duplicateLog = [];
const conflicts = [];
const novel = [];
function add(row, origin, index) {
  const key = keyFor(row.subject);
  assert.ok(key, `Empty title: ${origin} ${index}`);
  const messageId = row.message_id?.trim();
  const old = seenSubjects.get(key) ?? (messageId ? seenMessages.get(messageId) : undefined);
  if (old !== undefined) {
    const kept = retained[old];
    duplicateLog.push({source:origin, source_row:index, removed_id:row.id || '', retained_id:kept.id, reason:seenSubjects.has(key) ? 'normalized_subject' : 'message_id'});
    if (row.label && kept.label && row.label !== kept.label) conflicts.push({retained_id:kept.id, labels:[kept.label,row.label]});
    if (row.review_status === '需重点复核') {
      kept.review_status = '需重点复核';
      if (row.needs_body === '1') kept.needs_body = '1';
      if (row.annotation_note && !kept.annotation_note.includes(row.annotation_note)) {
        kept.annotation_note = [kept.annotation_note,row.annotation_note].filter(Boolean).join('；');
      }
    }
    // Make each key point at the retained row, including alternate message IDs.
    seenSubjects.set(key, old);
    if (messageId) seenMessages.set(messageId,old);
    return false;
  }
  seenSubjects.set(key,retained.length);
  if (messageId) seenMessages.set(messageId,retained.length);
  retained.push({...row});
  return true;
}
for (const [i,row] of oldRows.entries()) add(row,'existing',i+1);
const oldUniqueCount = retained.length;
for (const [i,row] of newRows.entries()) {
  const subject = clean(row.subject);
  const converted = Object.fromEntries(fields.map(field=>[field,'']));
  const owner = sha(row.to.toLowerCase().trim()).slice(0,12);
  Object.assign(converted, {
    id:`import:emails:${owner}:${row.uid || i+1}`, subject,
    sender:row.from, date:row.date,
    group_id:`subject:${sha(keyFor(subject)).slice(0,24)}`,
  });
  if (add(converted,'emails.csv',i+1)) novel.push({source_row:i+1, id:converted.id, subject});
}
console.log(JSON.stringify({existing_count:oldRows.length, existing_unique:oldUniqueCount,
  incoming_count:newRows.length, new_unique:novel.length,
  incoming_duplicates:duplicateLog.filter(d=>d.source==='emails.csv').length,
  merged_count:retained.length, conflicts, novel},null,2));
await fs.writeFile(path.join(work,'analysis.json'),JSON.stringify({
  target_sha256:sha(targetBytes), source_sha256:sha(sourceBytes),
  existing_count:oldRows.length, existing_unique:oldUniqueCount, incoming_count:newRows.length,
  retained, novel, duplicateLog, conflicts,
},null,2));

if (!process.argv.includes('--apply')) process.exit(0);
assert.equal(conflicts.length,0,'Existing duplicate labels conflict; review before overwriting');
const names = {T01:'课程与教务',T02:'学术与学习培训',T03:'就业与实习',T04:'升学与交流',T05:'竞赛与科创',T06:'校园活动与组织',T07:'学生事务与个人服务',T08:'校园运行与安全'};
// Explicit annotations of the 30 novel titles, reviewed against taxonomy v2.
const labels = {
  2:'T01',3:'T01',5:'T07',7:'T07',8:'T01',10:'T07',14:'T07',18:'T01',
  47:'T07',48:'T01',61:'T07',62:'T07',66:'T01',67:'T07',68:'T07',80:'T01',
  81:'T06',86:'T07',100:'T07',105:'T06',111:'T01',115:'T01',125:'T07',
  126:'T01',128:'T01',130:'T07',131:'T07',143:'T01',144:'T01',164:'T06',
};
const flagged = {
  10:'会议通知只注明讨论室，无法仅凭标题确认是个人预约回执还是一般会议安排；暂标T07，请复核正文。',
  61:'会议通知只注明讨论室，无法仅凭标题确认是个人预约回执还是一般会议安排；暂标T07，请复核正文。',
  62:'会议通知只注明讨论室，无法仅凭标题确认是个人预约回执还是一般会议安排；暂标T07，请复核正文。',
  67:'会议通知只注明讨论室，无法仅凭标题确认是个人预约回执还是一般会议安排；暂标T07，请复核正文。',
  68:'会议通知只注明讨论室，无法仅凭标题确认是个人预约回执还是一般会议安排；暂标T07，请复核正文。',
  81:'标题为隐喻表达，未明确活动性质；暂按校园活动T06标注，需查看正文确认。',
};
assert.equal(Object.keys(labels).length,novel.length);
for (const item of novel) {
  const row = retained.find(row=>row.id===item.id);
  row.label = labels[item.source_row];
  assert.ok(names[row.label],`Missing label for new row ${item.source_row}`);
  row.category_name = names[row.label];
  row.review_status = flagged[item.source_row] ? '需重点复核' : 'AI预标注';
  row.needs_body = flagged[item.source_row] ? '1' : '';
  row.annotation_note = `来源：emails.csv 第${item.source_row}条（uid=${newRows[item.source_row-1].uid}）。` + (flagged[item.source_row] || '依据标题按八类标准预标注。');
}
const matrix = [fields,...retained.map(row=>fields.map(field=>row[field] || ''))];
assert.equal(new Set(retained.map(row=>keyFor(row.subject))).size,retained.length);
assert.equal(new Set(retained.map(row=>row.id)).size,retained.length);
assert.ok(retained.every(row=>names[row.label]===row.category_name));

// Hash all running programs and fixed split files before writing data.
const protectedFiles = [
  ...(await fs.readdir(path.join(root,'training'))).filter(name=>name.endsWith('.py')).map(name=>path.join(root,'training',name)),
  ...(await fs.readdir(path.join(root,'data/splits'))).map(name=>path.join(root,'data/splits',name)),
];
const protectedHashes = Object.fromEntries(await Promise.all(protectedFiles.map(async file=>[file,sha(await fs.readFile(file))])));
const sheet = originalWb.worksheets.getItemAt(0);
sheet.getUsedRange().clear({applyTo:'contents'});
sheet.getRange(`A1:L${matrix.length}`).values=matrix;
originalWb.recalculate();
const actual=sheet.getRange(`A1:L${matrix.length}`).values.map(row=>row.map(v=>v==null?'':String(v)));
assert.deepEqual(actual,matrix);
// Preserve every existing retained row's original data and label.
for (const row of retained.slice(0,oldUniqueCount)) {
  const old=oldRows.find(old=>old.id===row.id);
  for (const field of fields.filter(field=>!['review_status','needs_body','annotation_note'].includes(field))) assert.equal(row[field],old[field],`${row.id} ${field}`);
}
sheet.getRange(`B1:B${matrix.length}`).format.columnWidth=65;
sheet.getRange(`C1:C${matrix.length}`).format.columnWidth=28;
sheet.getRange(`D1:D${matrix.length}`).format.columnWidth=28;
sheet.getRange(`E1:E${matrix.length}`).format.columnWidth=10;
sheet.getRange(`B${oldUniqueCount+2}:E${oldUniqueCount+7}`).format.wrapText=true;
sheet.getRange(`B${oldUniqueCount+2}:E${oldUniqueCount+7}`).format.autofitRows();
const preview=await originalWb.render({sheetName:sheet.name,range:`B${oldUniqueCount+2}:E${oldUniqueCount+7}`,scale:1,format:'png'});
await fs.writeFile(path.join(work,'merge_preview.png'),new Uint8Array(await preview.arrayBuffer()));
console.log((await originalWb.inspect({kind:'table',range:`${sheet.name}!E${oldUniqueCount+2}:E${oldUniqueCount+7}`,include:'values',tableMaxRows:6,tableMaxCols:1,maxChars:800})).ndjson);
const quote=value=>/[",\r\n]/.test(value)?`"${value.replaceAll('"','""')}"`:value;
const output='\uFEFF'+actual.map(row=>row.map(quote).join(',')).join('\r\n')+'\r\n';
const timestamp=new Date().toISOString().replace(/[:.]/g,'-');
const backupDir=path.join(root,'data/backups');
await fs.mkdir(backupDir,{recursive:true});
const backup=path.join(backupDir,`email_headers_20261016_T01-T08_labeled_before_merge_${timestamp}.csv`);
assert.equal(sha(await fs.readFile(target)),sha(targetBytes),'Target changed during review');
assert.equal(sha(await fs.readFile(source)),sha(sourceBytes),'Incoming source changed during review');
await fs.writeFile(backup,targetBytes,{flag:'wx'});
const temp=path.join(work,'merged_verified.csv');
await fs.writeFile(temp,output,'utf8');
const checkWb=await Workbook.fromCSV(output.replace(/^\uFEFF/,''),{sheetName:'检查'});
assert.deepEqual(read(checkWb),actual);
await fs.rename(temp,target);
for (const file of protectedFiles) assert.equal(sha(await fs.readFile(file)),protectedHashes[file],`Protected file changed: ${file}`);
const counts=Object.fromEntries(Object.keys(names).map(code=>[code,retained.filter(row=>row.label===code).length]));
const audit={
  source,target,backup,source_sha256:sha(sourceBytes),before_sha256:sha(targetBytes),after_sha256:sha(await fs.readFile(target)),
  original_rows:oldRows.length,original_duplicates_removed:oldRows.length-oldUniqueCount,
  incoming_rows:newRows.length,incoming_duplicates_skipped:newRows.length-novel.length,
  added_rows:novel.length,final_rows:retained.length,counts,
  new_review_rows:novel.filter(item=>flagged[item.source_row]).map(item=>item.source_row),
  total_review_rows:retained.filter(row=>row.review_status==='需重点复核').length,
  duplicates:duplicateLog,conflicts,
  dedup_policy:'Match nonempty Message-ID or NFKC/case/whitespace/linefold normalized subject with leading Re/Fw/Fwd stripped. UID alone is never used for matching. Reminder titles with different wording are retained.',
  notes:'All new labels are AI provisional annotations. Existing labels are preserved. Raw incoming HTML, bodies, links and attachments are not copied to the training CSV. Missing folder/Message-ID are left blank.',
  protected_files_unchanged:true,protected_sha256:protectedHashes,
};
await fs.writeFile(path.join(work,'merge_audit.json'),JSON.stringify(audit,null,2));
console.log(JSON.stringify({...audit,duplicates:`${duplicateLog.length} duplicate mappings saved in audit`,protected_sha256:'recorded'},null,2));
