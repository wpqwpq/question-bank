/* 打标台「预览模式」端到端验证（CDP 驱动本机 Edge，独立 profile，只读不改数据） */
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const os = require('os');
const fs = require('fs');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9337;
const BASE = 'http://127.0.0.1:8642';
// 固定 profile 目录：跨次复用，跑完**什么都不删**。
// ⚠️ 不要改回「%TEMP%/edge-e2e-<时间戳> + 收尾递归删」——本机 WorkBuddy 沙箱会把删除
// 重定向成「移入回收站」（safe-delete），递归删一个几百文件的 Chromium profile，
// 回收站就多几百条；多跑几次直接把回收站灌爆（2026-10-16 实测：一次清理 2514 条）。
const PROFILE = path.join(os.tmpdir(), 'qb-edge-e2e-preview');

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
    try {
      await httpGet(`http://127.0.0.1:${PORT}/json/version`);
      return;
    } catch { await sleep(200); }
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
  if (r.exceptionDetails) throw new Error('页面异常: ' + JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails.text).slice(0, 300));
  return r.result.value;
}

async function waitDom(expr, label, timeout = 15000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await evalJs(expr)) return;
    await sleep(300);
  }
  throw new Error('等待超时: ' + label);
}

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  —— ' + detail : ''}`);
}

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

    // 独立通道：期望值从 API 算出
    const papers = JSON.parse(await httpGet(`${BASE}/api/papers`));
    const target = papers.find((p) => p.id === '2026-jan-wph11a');
    const qs = JSON.parse(await httpGet(`${BASE}/api/papers/2026-jan-wph11a/questions`));
    const doneCount = qs.filter((q) => q.status === 'done').length;
    const splitQnums = qs.filter((q) => q.split).map((q) => q.question_number);

    // 1) 头部有模式切换
    const hasToggle = await evalJs(`(() => {
      const btns = [...document.querySelectorAll('header button')].map(b => b.textContent.trim());
      return btns.includes('打标模式') && btns.includes('预览模式');
    })()`);
    check('头部出现「打标模式/预览模式」切换按钮', hasToggle);

    // 2) 打标模式下无预览面板
    const noPreviewInAnnotate = await evalJs(`!document.querySelector('[data-preview-panel]')`);
    check('打标模式（默认）下不显示预览面板', noPreviewInAnnotate);

    // 3) 选中 wph11a 卷（React select 需原生 setter）
    const picked = await evalJs(`(() => {
      const setNative = (el, v) => {
        const s = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        s.call(el, v); el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const sel = document.querySelectorAll('aside select')[3];
      if (!sel) return false;
      const opt = [...sel.options].find(o => o.value === '2026-jan-wph11a');
      if (!opt) return false;
      setNative(sel, '2026-jan-wph11a');
      return true;
    })()`);
    check('左侧选中 2026-jan-wph11a 卷', picked);
    await waitDom(
      `document.querySelectorAll('aside button').length >= ${qs.length}`,
      '题目列表加载',
    );

    // 4) 切到预览模式
    await evalJs(`(() => {
      const b = [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '预览模式');
      b.click(); return true;
    })()`);
    await sleep(500);
    await waitDom(`!!document.querySelector('[data-preview-panel]')`, '预览面板出现');

    // 5) 预览列表只显示已打标题目 —— 用题目格子数量核对（当前卷应正好 doneCount 个）
    const gridInfo = await evalJs(`(() => {
      const aside = document.querySelector('aside');
      const btns = [...aside.querySelectorAll('button')];
      return { count: btns.length, label: (aside.querySelector('label') || {}).textContent || '' };
    })()`);
    check(
      `预览列表只显示已打标题目（UI ${gridInfo.count} 格 = API done ${doneCount} 题）`,
      gridInfo.count === doneCount,
      gridInfo.label.trim(),
    );

    // 6) 右侧预览面板是只读的：无保存按钮、无勾选交互按钮
    const readOnly = await evalJs(`(() => {
      const p = document.querySelector('[data-preview-panel]');
      return {
        noSave: ![...p.querySelectorAll('button')].some(b => b.textContent.includes('保存')),
        hasDifficulty: p.textContent.includes('难度'),
        hasTags: p.textContent.includes('知识点标签'),
      };
    })()`);
    check('预览面板只读（无保存按钮，有难度/标签区块）',
      readOnly.noSave && readOnly.hasDifficulty && readOnly.hasTags, JSON.stringify(readOnly));

    // 7) 跳到一道切分题（wph11a 第 11 题），验证平铺小问按钮
    const firstSplit = splitQnums[0];
    const jumped = await evalJs(`(() => {
      const aside = document.querySelector('aside');
      const btn = [...aside.querySelectorAll('button')].find(b => b.textContent.trim().startsWith('${firstSplit}'));
      if (!btn) return false;
      btn.click(); return true;
    })()`);
    check(`点击第 ${firstSplit} 题（切分题）跳转`, jumped);
    await waitDom(`(() => {
      const p = document.querySelector('[data-preview-panel]');
      return p && p.querySelectorAll('button').length > 0;
    })()`, '切分题平铺按钮出现');
    const splitInfo = await evalJs(`(() => {
      const p = document.querySelector('[data-preview-panel]');
      const labels = [...p.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => /^[a-z]$/.test(t));
      return { labels, noSelect: p.querySelectorAll('select').length === 0 };
    })()`);
    check(
      '切分题显示平铺小问按钮（无下拉）',
      splitInfo.labels.length >= 2 && splitInfo.noSelect,
      splitInfo.labels.join(' | '),
    );

    // 8) 切换小问：中间题图与右侧面板都跟着换
    const imgOf = () => evalJs(`document.querySelector('main img')?.getAttribute('src') ?? null`);
    const panelSnap = () => evalJs(`(() => {
      const p = document.querySelector('[data-preview-panel]');
      let t = p.innerText;
      const sel = p.querySelector('select');
      if (sel && sel.selectedOptions[0]) t = t.replace(sel.selectedOptions[0].textContent, '');
      return t;
    })()`);
    const imgA = await imgOf();
    const panelA = await panelSnap();
    await evalJs(`(() => {
      const p = document.querySelector('[data-preview-panel]');
      const btns = [...p.querySelectorAll('button')].filter(b => /^[a-z]$/.test(b.textContent.trim()));
      btns[btns.length - 1].click();
      return true;
    })()`);
    await sleep(400);
    const imgB = await imgOf();
    const panelB = await panelSnap();
    check('切换小问后中间题图变化', !!imgA && !!imgB && imgA !== imgB, `${imgA} → ${imgB}`);
    check('切换小问后右侧标记变化', panelA !== panelB);

    // 8b) 底部提示带小问标签
    const footer = await evalJs(`(() => {
      const main = document.querySelector('main');
      return main.textContent.includes('问');
    })()`);
    check('底部题号旁显示当前小问标签', footer);

    // 9) 切回打标模式：预览面板消失、打标面板回来
    await evalJs(`(() => {
      const b = [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '打标模式');
      b.click(); return true;
    })()`);
    await sleep(400);
    const backToAnnotate = await evalJs(`({
      previewGone: !document.querySelector('[data-preview-panel]'),
      annotateBack: !!document.querySelector('[data-annotate-panel]'),
    })`);
    check('切回打标模式：预览面板消失、打标面板恢复', backToAnnotate.previewGone && backToAnnotate.annotateBack);

    // 9b) 打标模式下切分题的小问选择也是平铺按钮：先跳到切分题再验
    await evalJs(`(() => {
      const aside = document.querySelector('aside');
      const btn = [...aside.querySelectorAll('button')].find(b => b.textContent.trim().startsWith('${firstSplit}'));
      btn.click(); return true;
    })()`);
    const subBtnsIn = (panelExpr) => `[...(${panelExpr}).querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => /^[a-z]$/.test(t))`;
    await waitDom(`${subBtnsIn(`document.querySelector('[data-annotate-panel]')`)}.length >= 2`, '打标模式平铺按钮出现');
    const annotateSub = await evalJs(`({
      labels: ${subBtnsIn(`document.querySelector('[data-annotate-panel]')`)},
      noSelect: document.querySelector('[data-annotate-panel]').querySelectorAll('select').length === 0,
    })`);
    check(
      '打标模式小问选择也是平铺按钮（无下拉）',
      annotateSub.labels.length >= 2 && annotateSub.noSelect,
      annotateSub.labels.join(' | '),
    );

    // 10) 换一张部分完成的卷，验证预览过滤（wch11 = 2/17，化学卷 → 先切学科）
    const wchPicked = await evalJs(`(() => {
      const setNative = (el, v) => {
        const s = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        s.call(el, v); el.dispatchEvent(new Event('change', { bubbles: true }));
      };
      // 先切回预览模式
      const b = [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '预览模式');
      b.click();
      const sels = document.querySelectorAll('aside select');
      setNative(sels[2], 'chemistry');   // 学科
      setNative(sels[3], '2025-oct-wch11'); // 试卷
      return true;
    })()`);
    await sleep(300);
    await waitDom(
      `document.querySelectorAll('aside button').length === 2 || document.body.innerText.includes('暂无已打标')`,
      'wch11 预览列表加载',
    );
    const wchGrid = await evalJs(`document.querySelectorAll('aside button').length`);
    check('部分完成卷（wch11, 2/17）预览列表只显示 2 格', wchPicked && wchGrid === 2, `UI ${wchGrid} 格`);

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
    }
  })();
