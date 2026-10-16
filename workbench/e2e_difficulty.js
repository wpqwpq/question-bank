/* docs/13 端到端验证：难度可留空 + 预览就地补难度
 * CDP 驱动本机浏览器（Edge / Chrome，自动探测），独立 profile。
 * 用 2020-jan-wph11 的 Q3（未打标整题）现场走一遍：
 *   打标模式只选标签就保存（琥珀）→ 预览模式灰底可见 → 补难度 + 勾选 → 深绿 → 打标模式变绿
 * 测完把 q03.md 与 _review/confirmed.json 原样还原。
 */
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const os = require('os');
const fs = require('fs');

const BROWSER_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];
const EDGE = BROWSER_CANDIDATES.find((p) => fs.existsSync(p));
if (!EDGE) throw new Error('未找到可用的 Edge / Chrome');

const PORT = 9341;
const BASE = 'http://127.0.0.1:8642';
const PAPER = '2020-jan-wph11';
const QNUM = '3';
const QID = 'edexcel-a-level-physics-2020-jan-wph11-q03';
const REPO = path.resolve(__dirname, '..');
const QFILE = path.join(REPO, 'data/edexcel/a-level/physics', PAPER, 'q03.md');
const REVIEW = path.join(REPO, 'data/_review/confirmed.json');
const SHOT = path.join(REPO, '.workbuddy/tmp/d13');
// 固定 profile 目录：跨次复用，跑完**什么都不删**。
// ⚠️ 不要改回「%TEMP%/edge-e2e-<时间戳> + 收尾递归删」——本机 WorkBuddy 沙箱会把删除
// 重定向成「移入回收站」（safe-delete），递归删一个几百文件的 Chromium profile，
// 回收站就多几百条；多跑几次直接把回收站灌爆（2026-10-16 实测：一次清理 2514 条）。
// 真要清理旧 profile，用 `cmd /c rd /s /q`（每个顶层目录只 +2 条）或直接留着。
const PROFILE = path.join(os.tmpdir(), 'qb-edge-e2e-difficulty');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function httpGet(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => resolve(d));
    }).on('error', reject);
  });
}

async function waitCdp() {
  for (let i = 0; i < 50; i++) {
    try { await httpGet(`http://127.0.0.1:${PORT}/json/version`); return; }
    catch { await sleep(200); }
  }
  throw new Error('CDP 未就绪');
}

let ws, msgId = 0;
const pending = new Map();
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evalJs(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (r.exceptionDetails) {
    throw new Error('页面异常: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text).slice(0, 300));
  }
  return r.result.value;
}

async function waitDom(expr, label, timeout = 25000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await evalJs(expr)) return true;
    await sleep(300);
  }
  throw new Error('等待超时: ' + label);
}

async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  fs.mkdirSync(SHOT, { recursive: true });
  const f = path.join(SHOT, name + '.png');
  fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
  console.log('    截图: ' + f);
}

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  —— ' + detail : ''}`);
}

const gridCls = `(() => {
  const b = [...document.querySelectorAll('aside button')].find(x => (x.getAttribute('title')||'').startsWith('第 ${QNUM} 题'));
  return b ? b.className : null;
})()`;
const progress = `(() => {
  const m = document.querySelector('header').textContent.match(/已打标\\s*(\\d+)\\/(\\d+)/);
  return m ? m[1] + '/' + m[2] : null;
})()`;
const previewNums = `[...document.querySelectorAll('aside')[0].querySelectorAll('button')].map(b => b.textContent.replace(/abc$/, '').trim())`;
// 面板归属锚点（data-qid）：切题后必须等面板换成目标题，否则会点到上一题的标签
// ——上一版脚本就栽在这个竞态上（detail 异步，切题瞬间面板仍是旧题，点击被随后到的
//    detail 覆盖掉，表现成「选了标签但保存仍灰」）。
const annotateOf = (qid) => `document.querySelector('[data-annotate-panel]')?.dataset.qid === '${qid}'`;
const previewOf = (qid) => `document.querySelector('[data-preview-panel]')?.dataset.qid === '${qid}'`;
const tagFoot = `(() => {
  const p = document.querySelector('[data-annotate-panel]');
  const t = [...p.querySelectorAll('p')].map(x => x.textContent.trim()).find(x => x.includes('已选'));
  return t || null;
})()`;
const diffSelected = `(() => {
  const p = document.querySelector('[data-annotate-panel]');
  return [...p.querySelectorAll('button')]
    .filter(x => ['极易','简单','中等','困难','极难'].includes(x.textContent.trim()))
    .filter(x => /bg-(emerald|green|amber|orange|red)-/.test(x.className)).length;
})()`;
const clickMode = (label) => `(() => {
  const b = [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '${label}');
  if (!b) return false; b.click(); return true;
})()`;
const clickTag = (i) => `(() => {
  const b = [...document.querySelectorAll('[data-annotate-panel] button.truncate')][${i}];
  if (!b) return false; b.click(); return true;
})()`;
const clickDifficulty = (label) => `(() => {
  const p = document.querySelector('[data-preview-panel]');
  if (!p) return false;
  const b = [...p.querySelectorAll('button')].find(x => x.textContent.trim() === '${label}');
  if (!b) return false; b.click(); return true;
})()`;

// 备份原文件
const origQ = fs.readFileSync(QFILE);
const origReview = fs.existsSync(REVIEW) ? fs.readFileSync(REVIEW) : null;

(async () => {
  const edge = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, 'about:blank',
  ], { stdio: 'ignore' });

  try {
    await waitCdp();
    const list = JSON.parse(await httpGet(`http://127.0.0.1:${PORT}/json/list`));
    const page = list.find((t) => t.type === 'page');
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
      }
    };
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Page.navigate', { url: BASE });
    await waitDom(`document.querySelectorAll('button').length > 0`, '首屏渲染');

    // 页面级报错收集（React 崩溃会体现在这里）
    await evalJs(`(() => { window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); return true })()`);

    // 选卷
    await waitDom(`(document.querySelectorAll('aside select')[3]?.options.length ?? 0) > 1`, '试卷下拉加载');
    await evalJs(`(() => {
      const setNative = (el, v) => {
        const s = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        s.call(el, v); el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      setNative(document.querySelectorAll('aside select')[3], '${PAPER}');
      return true;
    })()`);
    await waitDom(`${gridCls} !== null`, '题目列表加载');

    const p0 = await evalJs(progress);
    const cls0 = await evalJs(gridCls);
    check('前置：Q3 未打标（灰）', !!cls0 && cls0.includes('bg-gray-100'), cls0 || 'null');

    // ── 1) 打标模式：只选标签、不选难度，保存按钮应可用 ──
    await evalJs(`(() => {
      const b = [...document.querySelectorAll('aside button')].find(x => (x.getAttribute('title')||'').startsWith('第 ${QNUM} 题'));
      b.click(); return true;
    })()`);
    await waitDom(annotateOf(QID), '打标面板切到 Q3（detail 已就位）');
    await waitDom(`!!document.querySelector('[data-annotate-panel] button.truncate')`, '标签面板出现');
    await evalJs(clickTag(0));
    await waitDom(`${tagFoot}?.includes('已选 1 个')`, '标签已选中');
    await sleep(200);

    check('打标模式：标签已选中', ((await evalJs(tagFoot)) || '').includes('已选 1 个'), await evalJs(tagFoot));
    check('打标模式：已选标签但未选难度', (await evalJs(diffSelected)) === 0, `已选难度按钮=${await evalJs(diffSelected)}`);

    const saveEnabled = await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      const b = [...p.querySelectorAll('button')].find(x => x.textContent.trim() === '确定并保存');
      return b ? !b.disabled : null;
    })()`);
    check('打标模式：难度留空也能点「确定并保存」（需求 1）', saveEnabled === true, String(saveEnabled));
    check('难度提示文案已改为「可留空」',
      await evalJs(`document.querySelector('[data-annotate-panel]').innerText.includes('可留空')`));
    await shot('13-1-annotate-tags-only');

    // 保存
    await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      [...p.querySelectorAll('button')].find(x => x.textContent.trim() === '确定并保存').click();
      return true;
    })()`);
    await waitDom(`${gridCls} && ${gridCls}.includes('bg-amber-100')`, 'Q3 变琥珀', 30000);
    const cls1 = await evalJs(gridCls);
    check('保存后 Q3 变琥珀（有标签缺难度，需求 1）', cls1.includes('bg-amber-100'), cls1);
    const p1 = await evalJs(progress);
    check('卷进度「已打标」不因缺难度 +1', p1 === p0, `${p0} -> ${p1}`);
    await sleep(400);
    await shot('13-2-annotate-amber');

    // ── 2) 预览模式：缺难度的题也显示，且为灰 ──
    await evalJs(clickMode('预览模式'));
    await waitDom(`!!document.querySelector('[data-preview-panel]')`, '预览面板出现');
    await waitDom(`${gridCls} !== null`, '预览列表渲染');
    const pv = await evalJs(previewNums);
    check('预览列表包含缺难度的 Q3（需求 2）', pv.includes(QNUM), pv.join(', '));
    const cls2 = await evalJs(gridCls);
    check('预览里缺难度的题为灰底', cls2.includes('bg-gray-100'), cls2);
    check('图例出现「待补难度」', await evalJs(`[...document.querySelectorAll('aside')][0].innerText.includes('待补难度')`));

    // ── 3) 勾选框受难度门控 ──
    await evalJs(`(() => {
      const b = [...document.querySelectorAll('aside')[0].querySelectorAll('button')].find(x => x.textContent.replace(/abc$/,'').trim() === '${QNUM}');
      b.click(); return true;
    })()`);
    await waitDom(previewOf(QID), '预览面板切到 Q3（detail 已就位）');
    await waitDom(`!!document.querySelector('[data-confirm-checkbox]')`, '确认区出现');
    const dis0 = await evalJs(`document.querySelector('[data-confirm-checkbox]').disabled`);
    check('未选难度时确认框禁用（需求 2）', dis0 === true, `disabled=${dis0}`);
    check('提示文案要求先选难度',
      await evalJs(`document.querySelector('[data-confirm-box]').innerText.includes('先选中难度')`));
    await shot('13-3-preview-gray');

    // ── 4) 选难度 → 可勾选 → 勾选 → 深绿 ──
    await evalJs(clickDifficulty('困难'));
    await sleep(300);
    const dis1 = await evalJs(`document.querySelector('[data-confirm-checkbox]').disabled`);
    check('选了难度后确认框解禁', dis1 === false, `disabled=${dis1}`);
    const cb = await evalJs(`(() => {
      const c = document.querySelector('[data-confirm-checkbox]');
      c.click(); return true;
    })()`);
    await sleep(300);
    check('勾选框被点击', cb === true);
    await waitDom(`${gridCls} && ${gridCls}.includes('bg-green-300')`, 'Q3 变深绿', 30000);
    const cls3 = await evalJs(gridCls);
    check('补难度+确认后变深绿（需求 2）', cls3.includes('bg-green-300'), cls3);
    const p2 = await evalJs(progress);
    check('卷进度「已打标」+1（难度补齐）', p2 !== p1, `${p1} -> ${p2}`);
    await sleep(400);
    await shot('13-4-preview-deepgreen');

    // ── 5) 难度同步进题库 ──
    const body = fs.readFileSync(QFILE, 'utf8');
    check('难度已写回 q03.md', /difficulty:\s*hard/.test(body), (body.match(/difficulty:.*/) || [''])[0]);
    check('标签仍在', /tags:/.test(body) && /- u1-/.test(body));

    // ── 6) 切回打标模式：琥珀变绿 ──
    await evalJs(clickMode('打标模式'));
    await sleep(900);
    await waitDom(`${gridCls} !== null`, '打标列表渲染');
    const cls4 = await evalJs(gridCls);
    check('切回打标模式 Q3 由琥珀变绿（需求 2 尾条）',
      cls4.includes('bg-green-100') && !cls4.includes('bg-amber-100'), cls4);
    await shot('13-5-annotate-green');

    // ── 7) 无页面级 JS 报错 ──
    const errs = await evalJs(`window.__errs`);
    check('全程无页面 JS 报错', Array.isArray(errs) && errs.length === 0, JSON.stringify(errs).slice(0, 200));

    const failed = results.filter((r) => !r.ok);
    console.log(`\n===== ${results.length - failed.length}/${results.length} PASS =====`);
    process.exitCode = failed.length ? 1 : 0;
  } catch (e) {
    console.error('E2E ERROR:', e.message);
    process.exitCode = 2;
  } finally {
    try { ws && ws.close(); } catch {}
    try { edge.kill('SIGKILL'); } catch {}
      await sleep(500);
      // 不删 PROFILE —— 见文件顶部说明（沙箱会把删除记账进回收站）
      fs.writeFileSync(QFILE, origQ);
    if (origReview) fs.writeFileSync(REVIEW, origReview);
    else fs.rmSync(REVIEW, { force: true });
    console.log('已还原 q03.md 与 confirmed.json');
  }
})();
