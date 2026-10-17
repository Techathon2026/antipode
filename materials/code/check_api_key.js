#!/usr/bin/env node
/**
 * API 连通性检查 —— 一分钟验证你的 key / base_url / model 对不对
 *
 * 用法：
 *   node code/check_api_key.js
 *   node code/check_api_key.js --base-url https://xxx.example.com/v1 --key 你的key --model deepseek-pro
 *
 * 不带参数时自动读取 code/.env，读不到再回退到系统环境变量。
 */

const fs = require('fs');
const path = require('path');

// ---- 极简 .env 读取（不依赖任何第三方库） ----
function loadDotEnv() {
  const envPath = path.join(__dirname, '.env');
  if (!fs.existsSync(envPath)) return;
  for (const raw of fs.readFileSync(envPath, 'utf-8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const k = line.slice(0, eq).trim();
    const v = line.slice(eq + 1).trim();
    if (!(k in process.env)) process.env[k] = v;
  }
}
loadDotEnv();

const args = process.argv.slice(2);

function getArg(name) {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : null;
}

const baseUrl = getArg('--base-url') || process.env.OPENAI_BASE_URL;
const apiKey = getArg('--key') || process.env.OPENAI_API_KEY;
const model = getArg('--model') || process.env.STANCE_MODEL || 'deepseek-flash';

if (!baseUrl || !apiKey) {
  console.error('缺少 base_url 或 key。');
  console.error('用法: node code/check_api_key.js --base-url <URL> --key <KEY> --model <MODEL>');
  console.error('');
  console.error('当前检测到的环境变量：');
  console.error('  OPENAI_BASE_URL =', process.env.OPENAI_BASE_URL || '(未设置)');
  console.error('  OPENAI_API_KEY  =', process.env.OPENAI_API_KEY ? '(已设置)' : '(未设置)');
  console.error('  STANCE_MODEL    =', process.env.STANCE_MODEL || '(未设置)');
  process.exit(1);
}

if (!/^https?:\/\//i.test(baseUrl)) {
  console.error('❌ 接口地址还不是一个合法 URL：');
  console.error('   OPENAI_BASE_URL =', baseUrl);
  console.error('');
  console.error('请先把 code/.env 里的 OPENAI_BASE_URL 换成真实地址，例如：');
  console.error('   OPENAI_BASE_URL=https://api.deepseek.com');
  console.error('地址需要向现场工作人员 / 赛事群确认（比赛方资源页未给出）。');
  process.exit(1);
}

const root = baseUrl.replace(/\/+$/, '');
const url = root + '/chat/completions';

const body = {
  model,
  messages: [{ role: 'user', content: '只回复两个字：通了' }],
  temperature: 0,
  max_tokens: 16,
};

console.log('目标地址 :', url);
console.log('模型名   :', model);
console.log('密钥     :', apiKey.slice(0, 6) + '...' + apiKey.slice(-4));
console.log('---');

(async () => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 30000);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + apiKey,
      },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    clearTimeout(timer);

    const text = await res.text();
    console.log('HTTP 状态 :', res.status);

    if (!res.ok) {
      console.log('');
      console.log('❌ 失败。原始返回：');
      console.log(text.slice(0, 600));
      console.log('');
      console.log('常见原因：');
      console.log(' 401 密钥无效 → key 写错，或这key不属于这个网关');
      console.log(' 404 地址不对 → base_url 少了或多了 /v1');
      console.log(' 400 模型名不对 → model 名要按平台给的写');
      process.exit(2);
    }

    let json;
    try {
      json = JSON.parse(text);
    } catch (e) {
      console.log('❌ 返回不是 JSON：');
      console.log(text.slice(0, 600));
      process.exit(3);
    }

    const reply = json?.choices?.[0]?.message?.content;
    console.log('');
    console.log('✅ 通了！模型回复：', reply);
    console.log('');
    console.log('把下面三行写进 code/.env 即可：');
    console.log('  OPENAI_BASE_URL=' + root);
    console.log('  OPENAI_API_KEY=' + apiKey);
    console.log('  STANCE_MODEL=' + model);
  } catch (e) {
    clearTimeout(timer);
    console.log('❌ 请求异常 :', e.name, e.message);
    console.log('如果是超时/连接失败，先确认网络能否访问该地址。');
    process.exit(4);
  }
})();
