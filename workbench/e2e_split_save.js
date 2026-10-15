/* 打标台「切分保存后即时同步」+「无未保存拦截」端到端验证
 * CDP 驱动本机浏览器（Edge / Chrome，自动探测），独立 profile。
 * 前置：目标题（2026-jan-wph15a Q19）处于 partial 中间态（a 问缺难度），
 *       由外部脚本临时制造，测完还原。
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
const PORT = 9338;
const BASE = 'http://127.0.0.1:8642';
const PAPER = '2026-jan-wph15a';
const QNUM = '19';
const PROFILE = path.join(os.tmpdir(), 'edge-e2e-splitsave-' + Date.now().toString(36));

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

async function waitDom(expr, label, timeout = 20000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await evalJs(expr)) return true;
    await sleep(300);
  }
  throw new Error('等待超时: ' + label);
}

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  —— ' + detail : ''}`);
}

// 题目格子的 class（按 title 精确定位第 N 题）
const gridCls = `(() => {
  const b = [...document.querySelectorAll('aside button')].find(x => (x.getAttribute('title')||'').startsWith('第 ${QNUM} 题'));
  return b ? b.className : null;
})()`;
const headProgress = `(() => {
  const m = document.querySelector('header').textContent.match(/已打标\\s*(\\d+)\\/(\\d+)/);
  return m ? m[1] + '/' + m[2] : null;
})()`;

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

    // 拦截 window.confirm，统计弹窗次数（预期恒为 0）
    await evalJs(`(() => {
      window.__cc = 0;
      const o = window.confirm;
      window.confirm = function (...a) { window.__cc++; return o.apply(this, a); };
      return true;
    })()`);

    // 选卷：aside 里第 4 个 select（考试局/资格/学科/试卷）
    // 注意必须先等卷列表（papers）加载完，否则 option 还没渲染，设值会落空
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

    // 1) 前置：Q19 应为 partial（琥珀）
    const cls0 = await evalJs(gridCls);
    check('前置态：Q19 格子为 partial（琥珀）', !!cls0 && cls0.includes('bg-amber-100'), cls0 || 'null');

    // 2) 头部进度 0/22
    const p0 = await evalJs(headProgress);
    check('前置态：头部卷进度 0/22', p0 === '0/22', String(p0));

    // 3) 打开 Q19 → 小问平铺按钮 a|b
    await evalJs(`(() => {
      const b = [...document.querySelectorAll('aside button')].find(x => (x.getAttribute('title')||'').startsWith('第 ${QNUM} 题'));
      b.click(); return true;
    })()`);
    await waitDom(`[...(document.querySelector('[data-annotate-panel]')?.querySelectorAll('button')||[])].filter(b=>/^[a-z]$/.test(b.textContent.trim())).length >= 2`, '小问按钮出现');
    const labels = await evalJs(`[...document.querySelector('[data-annotate-panel]').querySelectorAll('button')].map(b=>b.textContent.trim()).filter(t=>/^[a-z]$/.test(t))`);
    check('Q19 展开后显示出小问按钮 a|b', labels.join('|') === 'a|b', labels.join(' | '));

    // 4) 选 a 问难度（制造未保存状态）→ 不应出现「有未保存的修改」
    await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      [...p.querySelectorAll('button')].find(b => /^[a-z]$/.test(b.textContent.trim())).click();
      return true;
    })()`);
    await sleep(200);
    await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      [...p.querySelectorAll('button')].find(b => b.textContent.trim() === '简单').click();
      return true;
    })()`);
    await sleep(300);
    const noDirtyHint = await evalJs(`!document.body.innerText.includes('有未保存的修改')`);
    check('改动草稿后不再出现「有未保存的修改」提示', noDirtyHint);

    // 5) 切到 b 问 → 切分并保存（不刷新）
    await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      const btns = [...p.querySelectorAll('button')].filter(b => /^[a-z]$/.test(b.textContent.trim()));
      btns[btns.length - 1].click();
      return true;
    })()`);
    await sleep(300);
    const saveLabel = await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      const b = [...p.querySelectorAll('button')].find(x => x.textContent.includes('切分并保存'));
      return b ? b.textContent.trim() : null;
    })()`);
    check('焦点在末问时按钮为「切分并保存」', !!saveLabel, String(saveLabel));
    await evalJs(`(() => {
      const p = document.querySelector('[data-annotate-panel]');
      [...p.querySelectorAll('button')].find(x => x.textContent.includes('切分并保存')).click();
      return true;
    })()`);

    // 6) 保存完成（不刷新）→ 左侧格子立刻变绿
    const turnedGreen = await waitDom(`${gridCls}.includes('bg-green-100')`, 'Q19 变绿', 40000).then(() => true).catch(() => false);
    const cls1 = await evalJs(gridCls);
    check('保存后左侧 Q19 立即变绿（未刷新）', turnedGreen, String(cls1));

    // 7) 头部进度 +1
    const p1 = await evalJs(headProgress);
    check('保存后头部卷进度变 1/22（未刷新）', p1 === '1/22', String(p1));

    // 8) 全程无 confirm 弹窗
    const cc = await evalJs(`window.__cc`);
    check('全程未弹出任何确认框', cc === 0, `confirm 调用 ${cc} 次`);

    // 9) 不刷新切预览模式 → 立即可见 Q19
    await evalJs(`(() => {
      [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '预览模式').click();
      return true;
    })()`);
    await waitDom(`!!document.querySelector('[data-preview-panel]')`, '预览面板出现');
    const previewNums = await evalJs(`[...document.querySelectorAll('aside')[0].querySelectorAll('button')].map(b => b.textContent.replace(/abc$/, '').trim())`);
    check('切到预览模式立即可见已保存的 Q19（无需刷新）', previewNums.includes(QNUM), previewNums.join(', '));

    // 10) 切回打标模式：点别的题不弹确认框
    await evalJs(`(() => {
      [...document.querySelectorAll('header button')].find(x => x.textContent.trim() === '打标模式').click();
      return true;
    })()`);
    await sleep(400);
    const jumped = await evalJs(`(() => {
      const b = [...document.querySelectorAll('aside button')].find(x => (x.getAttribute('title')||'').startsWith('第 5 题'));
      if (!b) return false;
      b.click(); return true;
    })()`);
    await sleep(800);
    const cc2 = await evalJs(`window.__cc`);
    check('带草稿直接切题成功且不弹确认框', jumped && cc2 === 0, `confirm 调用 ${cc2} 次`);

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
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch {}
  }
})();
