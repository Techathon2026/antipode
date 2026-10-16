import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { Workbook } from '@oai/artifact-tool';

const source = 'D:/documents/techathon2026/data/email_headers_20261016_180127.csv';
const output = 'D:/documents/techathon2026/data/email_headers_20261016_T01-T08_labeled.csv';
const sourceBytes = await fs.readFile(source);
const originalHash = crypto.createHash('sha256').update(sourceBytes).digest('hex');
const wb = await Workbook.fromCSV(sourceBytes.toString('utf8').replace(/^\uFEFF/, ''), {sheetName: '邮件分类'});
const sheet = wb.worksheets.getItemAt(0);
const original = sheet.getRange('A1:I541').values.map(row => row.map(value => value == null ? '' : String(value)));
assert.equal(original.length, 541);
assert.deepEqual(original[0], ['id','subject','sender','date','label','folder','needs_body','message_id','group_id']);
assert.equal(new Set(original.slice(1).map(row => row[0])).size, 540);
assert.ok(original.slice(1).every(row => row[4] === ''));

// Each line is a manually reviewed block of ten source records, in source order.
// These assignments are a fixed annotation of this snapshot, not a keyword classifier.
const assignments = `
2 3 2 3 3 2 2 6 3 3
3 8 6 2 2 8 4 2 1 1
6 6 2 3 2 4 8 3 4 1
1 1 2 3 4 8 6 3 3 8
3 2 7 1 7 1 3 6 6 2
3 4 6 5 4 2 3 3 3 3
3 3 3 3 3 2 2 4 2 6
3 2 6 6 3 3 2 4 6 8
1 3 1 7 6 4 4 7 3 8
8 3 3 8 3 6 6 2 1 6
6 1 4 6 1 4 3 6 3 2
3 2 2 2 1 5 6 3 6 3
3 2 3 6 2 5 7 2 3 3
7 2 6 7 3 3 1 6 4 7
6 1 5 1 8 2 8 6 8 6
7 7 6 8 6 6 6 1 6 8
3 1 6 5 6 3 7 6 1 6
5 6 2 2 6 8 7 7 7 7
6 7 6 6 3 7 7 6 1 6
6 1 1 5 8 8 6 2 6 6
1 7 6 6 3 3 6 4 2 1
1 8 8 1 3 8 1 3 3 6
3 2 5 2 4 6 3 6 6 2
2 6 4 2 3 1 3 3 3 3
1 7 6 7 2 2 2 2 2 3
2 1 1 3 6 6 6 1 3 7
6 7 8 6 3 3 6 6 6 3
6 3 6 1 6 3 3 3 3 3
3 1 2 6 2 6 3 3 5 6
3 6 6 2 1 2 1 6 2 6
1 8 6 6 3 2 3 3 2 8
3 1 6 6 6 2 3 1 2 6
5 3 3 1 3 6 6 2 1 8
3 1 6 3 3 6 1 6 6 3
3 2 4 6 8 6 6 1 3 3
1 6 8 6 1 1 1 3 3 8
1 1 8 1 1 6 3 6 3 3
6 3 3 7 3 5 6 6 3 8
3 3 8 3 2 6 7 8 6 6
8 2 7 1 3 3 3 2 3 1
6 6 3 3 6 1 3 8 3 3
3 8 6 6 1 6 5 3 3 3
1 6 7 6 4 3 8 2 6 8
6 8 3 5 7 3 4 8 6 5
5 2 3 8 4 6 8 8 3 6
6 4 6 8 3 3 8 8 6 6
3 6 6 3 8 8 5 7 8 1
2 3 5 3 6 6 1 1 1 7
3 1 7 6 8 1 8 3 7 8
7 7 6 7 2 8 8 2 8 1
3 8 1 2 8 8 3 3 3 3
8 6 8 8 1 7 5 8 8 6
8 8 8 3 8 6 8 8 8 6
8 8 6 8 6 8 8 8 8 6
`.trim().split('\n').map(line => {
  const labels = line.trim().split(/\s+/).map(Number);
  assert.equal(labels.length, 10, 'Each block must contain ten records');
  return labels;
}).flat();
assert.equal(assignments.length, 540);

const names = {
 T01:'课程与教务', T02:'学术与学习培训', T03:'就业与实习', T04:'升学与交流',
 T05:'竞赛与科创', T06:'校园活动与组织', T07:'学生事务与个人服务', T08:'校园运行与安全'
};
const flagged = {
 6:'暂按专利转化讲座归T02；若正文主要为创业项目招募或转化项目申报，可改T05。',
 22:'暂按环保回收活动归T06；若正文主要为常态回收服务安排，可改T08。',
 41:'标题同时涉及教学技能和职业选择，暂按职业发展归T03；若以教学方法培训为主，可改T02。',
 44:'荣誉班开班暨Orientation暂归T01；与T07新生事务或T06典礼活动有交叉，需统一边界。',
 46:'荣誉班开班暨Orientation暂归T01；与T07新生事务或T06典礼活动有交叉，需统一边界。',
 55:'研究生项目和暑期研修实习合并宣讲，暂按国际升学交流归T04；若以就业实习为主，可改T03。',
 66:'标题仅说明思政部讲座，暂归T02；若为形策课程修读环节改T01，党团主题教育改T06。',
 67:'新著思想对谈暂按知识交流归T02；文化艺术季活动属性也可归T06，请确认统一口径。',
 69:'精力管理分享暂按学习指导归T02；若正文主要为心理健康服务，可改T07。',
 79:'标题主题不具体，暂按党支部活动归T06，需查看正文确认实际目的。',
 100:'暂按社团分享活动归T06；如该活动明确用于完成心理必修课程要求，可改T01。',
 110:'文学主题讲座暂归T02；文化艺术季属性与T06有交叉，请确认讲座口径。',
 113:'导师沙龙暂归学习指导T02；标题未说明主题，可能为一般交流活动T06。',
 122:'科技艺术工作坊暂按学习培训归T02；如以艺术体验为主，可改T06。',
 184:'暂按艺术季体验活动归T06；如以心理咨询或治疗服务为主，可改T07。',
 187:'讨论室会议通知暂按个人预约业务归T07；标题未说明会议性质，需核对正文。',
 223:'Game Jam暂按游戏创作挑战归T05；如只是普通社团体验活动，可改T06。',
 228:'新生交流会暂按校园活动归T06；如属于必须办理的入学事务可改T07。',
 241:'标题明确为形策活动，按课程环节归T01；若无修读要求且仅为社会实践展示可改T06。',
 245:'文学历史主题分享暂归T02；文化艺术季属性与T06有交叉，请确认讲座口径。',
 246:'标题仅写企业技术交流，暂归T02；如正文为招聘宣讲或岗位介绍，应改T03。',
 247:'标题仅写企业技术交流，暂归T02；如正文为招聘宣讲或岗位介绍，应改T03。',
 248:'标题仅写企业技术交流，暂归T02；如正文为招聘宣讲或岗位介绍，应改T03。',
 249:'标题仅写企业技术交流，暂归T02；如正文为招聘宣讲或岗位介绍，应改T03。',
 267:'科创巴士参访暂按文化季校园参访归T06；若为项目招募或科创训练可改T05。',
 284:'新生交流会暂按校园活动归T06；若主要为学业适应指导可改T02。',
 296:'标题同时包含研究论坛和学生成果展，暂按论坛归T02；若以展览为主可改T06。',
 300:'学长学姐交流活动暂归T06；若正文主要是系统学业指导，可改T02。',
 304:'科创巴士参访暂按文化季校园参访归T06；若为项目招募或科创训练可改T05。',
 306:'文学历史主题分享暂归T02；文化艺术季属性与T06有交叉，请确认讲座口径。',
 385:'院士与青年面对面暂按学术学习交流归T02；若为一般成长主题活动，可改T06。',
 416:'学院新生开学典礼暂按校园活动归T06；若强调强制入学事务，可改T07。',
 428:'标题仅写讲座和抽象题目，暂归T02，需正文确认是否属于文体活动T06等其他类别。',
 435:'实名认证结果暂按个人业务回执归T07；若是信息系统账号验证，应改T08。',
 453:'新生交流会主讲人招募暂按校园活动归T06；若为有偿校内岗位，应改T03。',
 471:'标题只有新的培训任务，暂归T02；如实际为安全必修培训，应改T08。仅标题信息不足。',
 483:'军训准备暂按新生事务归T07；若明确属于军事课程教学考核通知，可改T01。',
 517:'青年科创节暂按科创主题项目归T05；若仅为参观交流活动，可改T06或T02。'
};

const headers = [...original[0], 'category_name', 'review_status', 'annotation_note'];
const matrix = [headers, ...original.slice(1).map((row, index) => {
  const label = `T0${assignments[index]}`;
  assert.ok(names[label]);
  const copy = [...row];
  copy[4] = label;
  return [...copy, names[label], flagged[index+1] ? '需重点复核' : 'AI预标注', flagged[index+1] || ''];
})];

// CSV has no source formatting; render a small imported sample before editing.
sheet.getRange('B1:B4').format.columnWidth = 70;
sheet.getRange('C1:C4').format.columnWidth = 34;
sheet.getRange('D1:D4').format.columnWidth = 28;
sheet.getRange('E1:E4').format.columnWidth = 12;
sheet.getRange('B1:E4').format.wrapText = true;
sheet.getRange('B1:E4').format.autofitRows();
const before = await wb.render({sheetName:'邮件分类', range:'B1:E4', scale:1, format:'png'});
await fs.writeFile('D:/documents/techathon2026/annotation_work/before.png',new Uint8Array(await before.arrayBuffer()));

sheet.getRange('E2:E541').values = matrix.slice(1).map(row => [row[4]]);
sheet.getRange('J1:L541').values = matrix.map(row => row.slice(9));
sheet.getRange('J1:J541').format.columnWidth = 24;
sheet.getRange('K1:K541').format.columnWidth = 18;
sheet.getRange('L1:L541').format.columnWidth = 74;
sheet.getRange('J1:L541').format.wrapText = true;
sheet.getRange('J1:L8').format.autofitRows();
wb.recalculate();
const actual = sheet.getRange('A1:L541').values.map(row => row.map(value => value == null ? '' : String(value)));
assert.deepEqual(actual, matrix);
const counts = Object.fromEntries(Object.keys(names).map(code => [code,0]));
for (let i = 1; i < actual.length; i++) {
  counts[actual[i][4]]++;
  for (let col = 0; col < 9; col++) {
    if (col !== 4) assert.equal(actual[i][col], original[i][col], `Source mismatch row ${i} col ${col}`);
  }
}
assert.equal(Object.values(counts).reduce((a,b)=>a+b),540);
// Duplicated exact titles must carry the same primary class.
const seenTitles = new Map();
for (const row of actual.slice(1)) {
  if (seenTitles.has(row[1])) assert.equal(row[4],seenTitles.get(row[1]),`Conflicting title: ${row[1]}`);
  seenTitles.set(row[1],row[4]);
}
const preview = await wb.render({sheetName:'邮件分类',range:'J1:L8',scale:1,format:'png'});
await fs.writeFile('D:/documents/techathon2026/annotation_work/labels_preview.png',new Uint8Array(await preview.arrayBuffer()));
const inspect = await wb.inspect({kind:'table',range:'邮件分类!J1:L8',include:'values',tableMaxRows:8,tableMaxCols:3,maxChars:1800});
console.log(inspect.ndjson);

const quote = value => /[",\r\n]/.test(value) ? `"${value.replaceAll('"','""')}"` : value;
const csv = '\uFEFF' + actual.map(row => row.map(quote).join(',')).join('\r\n')+'\r\n';
await fs.writeFile(output,csv,{encoding:'utf8',flag:'wx'});
assert.equal(crypto.createHash('sha256').update(await fs.readFile(source)).digest('hex'),originalHash,'Source changed during annotation');
const audit = {
 source, source_sha256:originalHash, output, count:540, counts,
 review_count:Object.keys(flagged).length, review_record_numbers:Object.keys(flagged).map(Number),
 label_to_id:Object.fromEntries(Object.keys(names).map((code,i)=>[code,i])),
 category_names:names,
 training_input:'subject only', target:'label (T01–T08)',
 status:'AI provisional annotations; manual review required, especially flagged records',
 source_columns_preserved:true, exact_duplicate_titles_consistent:true
};
await fs.writeFile('D:/documents/techathon2026/annotation_work/annotation_audit.json',JSON.stringify(audit,null,2));
console.log(JSON.stringify(audit,null,2));
