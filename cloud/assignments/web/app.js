/* 题库选题系统（真实数据版）
 * 数据源：/api/bank/*（本地只读快照）  截图：/api/image?path=...（服务端带会话代理）
 * 对题库服务器只读，不发起任何写操作。
 * 部署在 /assignments 子路径时，自动给所有接口加前缀。
 */

// 本地 http://127.0.0.1:8080 时为空；线上 http://<host>/assignments/ 时为 '/assignments'
const APP_BASE = location.pathname.indexOf('/assignments') === 0 ? '/assignments' : '';
const API = APP_BASE + '/api';

// 阶段一：进入页面即加载，全科目 × 全考试局 × 全阶段 × 两种模式的一级目录（约 24KB）
let CATALOG = { subjects:[], years:{}, units:{}, defaults:{}, unitDisplay:{} };
// 阶段二：选中科目后按当前考试局+阶段加载的二/三/四级目录文字（试卷、题号、小问、标签、题干），不含难度
let PAPERS = [], PREFIXES = {}, TAXONOMY = {}, TAX_V2 = {}, UNIT_DISPLAY = {};
let QUESTIONS = {}, SUBTAGS = {}, TEXT = {}, SUBMETA = {};
// 阶段三：展开某一级目录后才加载的难度颜色（按目录分片，加载后合并进 DIFF）
let DIFF = {};
let diffScopes = new Set();       // 已加载的难度分片：subject|scope
let diffScopeLoading = new Map(); // 加载中的分片 Promise（并发去重）
let allDiffLoadedFor = '';        // 难度筛选用的科目级合并包，按科目记录
let allDiffLoading = null;
const DIFF_CLASSES = ['very-easy','easy','medium','hard','very-hard'];
let paperMap = {}, tagIndex = {}, groupIndex = {}, qidIndex = {};
let loadedTreeKey = '';           // 当前已加载的目录文字：subject|board|level
let treeLoading = null;           // 正在加载中的目录文字 Promise（并发去重）
let loading = true;
// 阶段四：展开二级目录时预取图片（用浏览器图片缓存，不落盘、不重复请求）
const preloadedImgs = new Set();
const imgQueue = [];
let imgPumping = 0;
const IMG_CONCURRENCY = 4;
let diffFilter = 'all';               // 按类别模式下的难度筛选：all/very-easy/easy/medium/hard/very-hard
let keyword = '';                     // 题干关键词（空格分隔，全部命中才显示）
let kwHits = [];                      // 最近一次关键词搜索的原始命中记录（难度切换时直接重筛）
let kwTerm = '';                      // 最近一次关键词搜索的词，重筛时保持空结果提示一致

const DIFF_NAME = {'very-easy':'极易','easy':'容易','medium':'中等','hard':'困难','very-hard':'极难'};
// 收藏夹保存在浏览器本地，不随草稿清除。旧试卷 qid 中带长数字段，统一归一化后匹配。
const FAV_KEY = 'qbank_favorites_v1';
let favorites = [];
function loadFavorites(){ try{ return JSON.parse(localStorage.getItem(FAV_KEY)||'[]'); }catch(e){ return []; } }
function saveFavorites(){ try{ localStorage.setItem(FAV_KEY, JSON.stringify(favorites)); }catch(e){} }
function normQid(qid){ return String(qid||'').replace(/-\d{10,}/g,''); }
function diffOf(qid, fallback){
  const n = normQid(qid);
  return (DIFF[qid] || DIFF[n] || fallback || null);
}

// 模式：paper=按套卷（原「整套」+「拆分」合并），category=按类别（原「分类」）
let mode = 'paper', board = 'edexcel', level = 'a-level', subject = '';
let selected = [];        // {key,label,no,img,ref}
let current = null;
let activeIdx = -1;
let zoom = 0;             // 0=适应宽度; >0 百分比档位

const BOARD_CODE = { edexcel:'E', cie:'C', aqa:'A' };
// 科目显示名（未列出的学科回退为原始 id）。下拉选项由数据中的 subject 动态生成。
const SUBJECT_NAME = { chemistry:'化学', physics:'物理', biology:'生物',
                       mathematics:'数学', maths:'数学', 'further-mathematics':'进阶数学',
                       'further-maths':'进阶数学', accounting:'会计', business:'商务',
                       economics:'经济', 'combined-science':'综合科学', psychology:'心理学',
                       computer_science:'计算机科学' };
function subjectLabel(s){ return SUBJECT_NAME[s] || s || ''; }
// 月份归一化：5/6月统一算作六月(june)，10/11月统一算作十月(oct)。
// 仅用于展示/分组/题号；图片路径(imagePath)必须用原始 session，不能归一化。
const SESSION_NORM = { 'may':'june','may-jun':'june','june':'june','june_july':'june','june-july':'june',
                       'oct':'oct','nov':'oct','oct-nov':'oct','oct_nov':'oct' };
// 月份统一用三位字母缩写（目录标签与题号共用），已归一化：june=5/6月, oct=10/11月
const MONTH_ABBR = { jan:'Jan', feb:'Feb', mar:'Mar', apr:'Apr', may:'May', june:'Jun', july:'Jul', aug:'Aug', sep:'Sep', oct:'Oct', nov:'Nov', dec:'Dec' };
const REVIEW_RE = /复习|review|revision|recap/i;

const $ = id => document.getElementById(id);
function el(tag, cls){ const d=document.createElement(tag); if(cls) d.className=cls; return d; }
function toast(msg){ const t=$('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),1800); }
function busy(on,text){ $('busyText').textContent=text||'正在生成文件…'; $('busy').classList.toggle('show',on); }

/* ---------- 草稿：每次操作自动暂存到本地 .drafts/，点「发布作业」后清除 ---------- */
let draftTimer = null;
let restoring = false;
let pendingDraftSel = null;   // 草稿里的已选题目：等用户选科目后再还原（打开时不加载题目数据）
// 收集当前页面状态。题目 ref 里的 p/q 是运行时对象，只需存可重建的标识，恢复时用 paperMap/QUESTIONS 还原。
function draftSnapshot(){
  return {
    mode, board, level, subject,
    jobName: $('jobName').value,
    classId: $('classSel').value,
    sel: selected.map(s=>({ key:s.key, no:s.no, label:s.label, img:s.img,
      whole: !!(s.ref && s.ref.whole), pid: s.ref&&s.ref.p ? s.ref.p.id : null,
      qid: s.ref&&s.ref.q ? s.ref.q.qid : null,
      sub: s.ref ? s.ref.sub||null : null, subLabel: s.ref ? s.ref.subLabel||null : null }))
  };
}
function saveDraft(){
  if(restoring || loading) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(()=>{
    fetch(API + '/draft',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(draftSnapshot())}).catch(()=>{});
  }, 250);
}
function clearDraft(){
  clearTimeout(draftTimer);
  fetch(API + '/draft',{method:'DELETE'}).catch(()=>{});
}
async function restoreDraft(){
  let d=null;
  try{ d = await fetch(API + '/draft').then(r=>r.json()); }catch(e){}
  if(d && d.sel && Array.isArray(d.sel)){
    restoring=true;
    // 按钮状态始终回到默认：按套卷 / Edexcel / A-Level（草稿只恢复作业名、班级和已选题）
    mode='paper'; board='edexcel'; level='a-level';
    fillSubjectSelect();                  // 科目始终置空，打开时不加载任何科目题目
    $('jobName').value = d.jobName||'';
    if(d.classId){ $('classSel').value=d.classId; }
    setSeg('segMode',mode); setSeg('segBoard',board); setSeg('segLevel',level);
    $('kwSearch').hidden=(mode!=='keyword');
    $('treeHeadTitle').textContent='题库目录';
    $('treeMode').textContent='按套卷';
    if(d.sel.length){
      pendingDraftSel={subject:d.subject||'',board:d.board||'',level:d.level||'',sel:d.sel};
      if(d.subject) toast('检测到未发布的草稿，请选择「'+subjectLabel(d.subject)+'」继续编辑');
    }
    restoring=false;
  }
  renderSel(); updateStatus();
}

// 选科目后还原草稿中属于该科目的已选题目（用标识重建运行时引用）
function restoreDraftSelFor(s){
  if(!pendingDraftSel) return;
  const d=pendingDraftSel;
  pendingDraftSel=null;
  if(d.subject && d.subject!==s) return;    // 草稿属于其他科目，不跨科目还原
  restoring=true;
  selected = d.sel.map(x=>{
    const p = paperMap[x.pid] || null;
    const q = x.qid && QUESTIONS[x.pid] ? QUESTIONS[x.pid].find(y=>y.qid===x.qid) : null;
    if(!p) return null;
    return { key:x.key, no:x.no, label:x.label, img:x.img,
      ref: x.whole ? {p,whole:true} : {p,q,sub:x.sub,subLabel:x.subLabel} };
  }).filter(Boolean);
  restoring=false;
  if(selected.length) toast('已恢复上次未发布的草稿');
}

/* ---------- 操作历史：顶栏「上一步 / 下一步」 ----------
 * 每次会改变页面结果的操作（勾选/移除/排序/清空/筛选/收藏/整题替换等）后记录一份完整快照。
 * 回退只改变前端状态，不触碰题库服务器；草稿同步更新，避免与历史不一致。
 */
let histStack = [];               // 快照数组，[0] 为进入页面时的初始状态
let histIdx = -1;                 // 当前所处快照下标
let histBusy = false;             // 恢复过程中禁止再次入栈
const HIST_MAX = 60;              // 最多保留 60 步

function historySnapshot(){
  return {
    mode, board, level, subject, diffFilter, keyword,
    jobName: $('jobName').value,
    classId: $('classSel').value,
    kwInput: $('kwInput').value,
    sel: draftSnapshot().sel,     // 与草稿同一套可重建标识
    favs: favorites.map(f=>({...f})),
    kwHits: mode==='keyword'?kwHits.slice():[], kwImgPaths: mode==='keyword'?kwImgPaths.slice():[], kwDone, kwTerm,
    activeIdx, zoom,
    preview: (current && current.html) ? {label:current.label, html:current.html} : null
  };
}
function histButtons(){
  const u=$('btnUndo'), r=$('btnRedo');
  if(!u || !r) return;
  u.disabled = histBusy || histIdx<=0;
  r.disabled = histBusy || histIdx<0 || histIdx>=histStack.length-1;
  u.title = u.disabled ? '没有更早的操作' : `退回到上一步之前（已记录 ${histIdx} 步）`;
  r.title = r.disabled ? '没有可重做的操作' : `前进到下一步（还有 ${histStack.length-1-histIdx} 步）`;
}
function pushHistory(){
  if(histBusy || restoring || loading) return;
  const snap=historySnapshot();
  if(histIdx>=0 && JSON.stringify(histStack[histIdx])===JSON.stringify(snap)) return;
  histStack=histStack.slice(0,histIdx+1);   // 回退后产生新操作 → 丢弃原「下一步」分支
  histStack.push(snap);
  if(histStack.length>HIST_MAX) histStack.shift();
  histIdx=histStack.length-1;
  histButtons();
}
// 统一入口：记录历史 + 暂存草稿
function markChange(){ pushHistory(); saveDraft(); }

async function applyHistory(s){
  histBusy=true; restoring=true;
  try{
    kwStop();
    mode=s.mode; board=s.board; level=s.level; subject=s.subject;
    diffFilter=s.diffFilter; keyword=s.keyword; zoom=s.zoom||0;
    favorites=(s.favs||[]).map(f=>({...f})); saveFavorites();
    $('jobName').value=s.jobName||'';
    $('classSel').value=s.classId||'';
    $('kwInput').value=s.kwInput||'';
    $('diffSel').value=s.diffFilter||'all';
    setSeg('segMode',mode); setSeg('segBoard',board); setSeg('segLevel',level);
    $('kwSearch').hidden=(mode!=='keyword');
    $('treeHeadTitle').textContent=mode==='category'?'题目分类':(mode==='keyword'?'关键词检索':'题库目录');
    $('treeMode').textContent=mode==='category'?'按类别':(mode==='keyword'?'按关键词':'按套卷');
    if(subject){
      if(loadedTreeKey!==treeKey(subject,board,level)) await loadTreeData(subject,board,level);
    }else{
      loadedTreeKey=''; QUESTIONS={}; SUBTAGS={}; DIFF={}; TEXT={}; SUBMETA={}; PAPERS=[]; paperMap={};
      diffScopes.clear(); diffScopeLoading.clear(); allDiffLoadedFor=''; allDiffLoading=null;
      imgQueue.length=0; preloadedImgs.clear();
    }
    selected=(s.sel||[]).map(x=>{
      const p=paperMap[x.pid]||null;
      const q=(x.qid && QUESTIONS[x.pid])?QUESTIONS[x.pid].find(y=>y.qid===x.qid):null;
      if(!p) return null;
      return {key:x.key,no:x.no,label:x.label,img:x.img,
        ref:x.whole?{p,whole:true}:{p,q,sub:x.sub||null,subLabel:x.subLabel||null}};
    }).filter(Boolean);
    kwHits=s.kwHits||[]; kwImgPaths=s.kwImgPaths||[]; kwDone=!!s.kwDone; kwTerm=s.kwTerm||'';
    activeIdx=(s.activeIdx>=0 && s.activeIdx<selected.length)?s.activeIdx:-1;
    current=s.preview?{label:s.preview.label,html:s.preview.html}:null;
    restoring=false;
    if(mode==='keyword') renderKeywordHits(false); else rebuildWithDifficulty();
    renderSel(); syncChecks(); renderFavMenu(); updateStatus();
    if(current) showPreview();
    else{
      $('previewLabel').textContent='—';
      $('paperView').innerHTML='<div class="empty-preview">点击左侧或中间的任意题目<br>即可在此预览截图</div>';
    }
    applyZoom();
  }finally{
    histBusy=false; restoring=false; histButtons();
  }
  saveDraft();
}
async function histGo(step){
  if(histBusy) return;
  const t=histIdx+step;
  if(t<0 || t>=histStack.length) return;
  const prev=histIdx;
  histIdx=t;
  try{
    await applyHistory(histStack[histIdx]);
    histButtons();
    toast(step<0?'已退回到上一步':'已前进到下一步');
  }catch(e){
    histIdx=prev; histButtons();
    toast('回退失败：'+e.message);
  }
}

/* ---------- 数据加载：四级按需加载 ----------
 * 阶段一（进入页面，未选科目）：catalog.json —— 全部科目 × 全部考试局 × 全部阶段
 *   × 按套卷/按类别两种模式的一级目录（纯目录名，约 24KB）。
 * 阶段二（下拉选定科目后）：subjects/<科目>/<考试局>-<阶段>/tree.json.gz —— 仅当前组合的
 *   二/三/四级目录文字（卷号、大题号、小题号、标签、题干），不含任何难度颜色。
 *   切换考试局/阶段时再取对应组合；按套卷与按类别共用同一份文字包。
 * 阶段三（展开某个一级目录时）：subjects/<科目>/diff/<id>.json.gz —— 该一级目录下
 *   全部题目的难度颜色；用户主动用难度筛选时才另取科目级合并包。
 * 阶段四（展开某个二级目录时）：预取该目录下全部题目截图，用浏览器图片缓存加速预览。
 */
async function loadCatalog(){
  try{
    CATALOG = await fetch(API + '/bank/catalog.json').then(r=>r.json());
    fillSubjectSelect();
    loading=false;
    await restoreDraft();   // 恢复草稿；不触发题目数据下载
    if(!subject) showSubjectHint();
    histIdx=-1; histStack=[]; pushHistory();   // 以进入页面时的状态作为历史起点
    histButtons();
  }catch(e){
    $('tree').innerHTML='<div class="loading">题库目录加载失败，请确认本地服务与数据文件。</div>';
  }
}

function treeKey(s,b,l){ return s+'|'+b+'|'+l; }

// 阶段二：按「科目 + 考试局 + 阶段」加载二/三/四级目录文字，绝不带难度
async function loadTreeData(s,b,l){
  if(!s) return;
  const key=treeKey(s,b,l);
  if(loadedTreeKey===key) return;               // 已加载该组合，避免重复下载
  if(treeLoading){ await treeLoading; return loadTreeData(s,b,l); }  // 并发切换时重新校验
  treeLoading=(async()=>{
    loading=true;
    showTreeLoading(s,b,l);
    try{
      const url=API+'/bank/subjects/'+encodeURIComponent(s)+'/'+encodeURIComponent(b)+'-'+encodeURIComponent(l)+'/tree.json.gz';
      const d=await fetch(url).then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); });
      if(subject!==s || board!==b || level!==l) return;   // 用户已切走，丢弃过期响应
      PAPERS=d.papers||[]; PREFIXES=d.prefixes||{};
      TAXONOMY=d.taxonomy||{}; UNIT_DISPLAY=d.unitDisplay||CATALOG.unitDisplay||{};
      TAX_V2={};
      Object.entries(d.taxonomyV2||{}).forEach(([combo,obj])=>{ TAX_V2[combo]={[s]:obj}; });
      QUESTIONS=d.questions||{}; SUBTAGS=d.subtags||{};
      TEXT=d.text||{}; SUBMETA=d.submeta||{};
      paperMap={}; PAPERS.forEach(x=>paperMap[x.id]=x);
      buildTagIndex();
      buildQidIndex();
      DIFF={}; diffScopes.clear(); diffScopeLoading.clear();
      allDiffLoadedFor=''; allDiffLoading=null;   // 难度只在展开一级目录时按目录取
      loadedTreeKey=key;
    }catch(e){
      loadedTreeKey='';
      $('tree').innerHTML='<div class="loading">「'+subjectLabel(s)+'」目录文字加载失败：'+e.message+'</div>';
    }finally{
      loading=false;
    }
  })().finally(()=>{ treeLoading=null; });
  return treeLoading;
}

// 一级目录 → 难度分片文件 id（与 scripts/build_staged_data.py 的 scope_slug 一致）
function scopeId(scopeKey){
  const bytes=new TextEncoder().encode(scopeKey);
  let bin=''; for(const b of bytes) bin+=String.fromCharCode(b);
  return btoa(bin).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
// 按套卷模式：一级目录 = 年月分组（gk 已用归一化月份）
function paperScope(gk){ return 'paper|'+board+'|'+level+'|'+gk; }
// 按类别模式：一级目录 = 单元展示名
function categoryScope(unit){ return 'category|'+board+'|'+level+'|'+unit; }

// 阶段三：展开某个一级目录时才下载该目录的难度分片；并发去重，加载完原地补色
function ensureDifficulty(scopeKey){
  if(!subject || !loadedTreeKey || !scopeKey) return Promise.resolve();
  const cacheKey=subject+'|'+scopeKey;
  if(diffScopes.has(cacheKey)) return Promise.resolve();
  if(diffScopeLoading.has(cacheKey)) return diffScopeLoading.get(cacheKey);
  const forSubject=subject;
  const url=API+'/bank/subjects/'+encodeURIComponent(forSubject)+'/diff/'+scopeId(scopeKey)+'.json.gz';
  const pr=fetch(url)
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{ if(subject!==forSubject) return;   // 切换科目后丢弃过期响应
      Object.assign(DIFF,d||{}); diffScopes.add(cacheKey); refreshDiffColors(); })
    .catch(()=>{ toast('难度数据加载失败'); })
    .finally(()=>{ diffScopeLoading.delete(cacheKey); });
  diffScopeLoading.set(cacheKey,pr);
  return pr;
}

// 难度筛选需要全局难度：仅当用户主动筛选时下载科目级合并包（单次/科目）
function ensureAllDifficulty(){
  if(!subject || !loadedTreeKey) return Promise.resolve();
  if(allDiffLoadedFor===subject) return Promise.resolve();
  if(allDiffLoading) return allDiffLoading;
  const forSubject=subject;
  allDiffLoading=fetch(API+'/bank/subjects/'+encodeURIComponent(forSubject)+'-difficulty.json.gz')
    .then(r=>{ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
    .then(d=>{ if(subject!==forSubject) return;   // 切换科目后丢弃过期响应
      Object.assign(DIFF,d||{}); allDiffLoadedFor=subject; refreshDiffColors(); })
    .catch(()=>{ toast('难度数据加载失败'); })
    .finally(()=>{ allDiffLoading=null; });
  return allDiffLoading;
}
// 不重建 DOM，只把已渲染题号行补上难度底色（保留用户当前的展开状态）
function refreshDiffColors(){
  document.querySelectorAll('#tree .label[data-diffkey]').forEach(lab=>{
    const key=lab.dataset.diffkey, fb=lab.dataset.difffb||'';
    const d=diffOf(key, fb||null);
    DIFF_CLASSES.forEach(c=>lab.classList.remove('diff-'+c));
    if(d) lab.classList.add('diff-'+d);
    const base=lab.dataset.basetitle||'';
    lab.title = d ? (base ? base+' · ' : '')+'难度：'+(DIFF_NAME[d]||d) : base;
    if(!lab.title) lab.removeAttribute('title');
  });
}

/* ---------- 阶段四：展开二级目录时预取该目录下全部题目图片 ---------- */
// 用 new Image() 让浏览器把截图放进 HTTP 缓存；同一题的多个小问共用父题图，按路径去重；
// 限制并发数避免一次展开卡住界面，失败也继续队列。
function preloadImages(paths){
  paths.forEach(p=>{
    if(!p || preloadedImgs.has(p)) return;
    preloadedImgs.add(p); imgQueue.push(p);
  });
  pumpImages();
}
function pumpImages(){
  while(imgPumping<IMG_CONCURRENCY && imgQueue.length){
    const p=imgQueue.shift(); imgPumping++;
    const im=new Image();
    im.onload=im.onerror=()=>{ imgPumping--; pumpImages(); };
    im.src=imgUrl(p);
  }
}
function preloadItemsImages(items){
  preloadImages(items.map(it=>imagePath(it.p.id, it.sid||it.q.qid)));
}
// 按套卷模式：二级目录 = 某个卷号 → 该卷所有题目（按图片路径去重）
function preloadPaperImages(p){
  preloadImages((QUESTIONS[p.id]||[]).map(q=>imagePath(p.id,q.qid)));
}
// 按类别模式：二级目录 = 一级标签 → 该标签下所有二级标签的题目
function preloadGroupImages(unit,gid,tagsByGroup,byUnitTag){
  const items=[];
  (tagsByGroup[gid]||[]).forEach(t=>{
    (((byUnitTag[unit]||{})[t.id])||[]).forEach(it=>items.push(it));
  });
  preloadItemsImages(items);
}

function showSubjectHint(){
  $('tree').innerHTML='<div class="list-empty">请先在上方选择「科目」<br>选中后从已加载的年月目录中展开题目</div>';
  $('previewLabel').textContent='—';
  $('paperView').innerHTML='<div class="empty-preview">选择科目并勾选题目后<br>即可在此预览截图</div>';
  current=null;
}
function showTreeLoading(s,b,l){
  const ys=(CATALOG.years[s+'|'+b+'|'+l]||[]).slice(0,8).map(x=>`${x.y} ${monthAbbr(x.s)}`).join(' · ');
  const us=(CATALOG.units[b+'|'+l+'|'+s]||[]).slice(0,8).join(' · ');
  const brief = mode==='category'
    ? (us?'已加载单元目录：'+us:'已加载全部科目分类目录')
    : (ys?'已加载年月目录：'+ys:'已加载全部科目年月目录');
  $('tree').innerHTML='<div class="list-empty">'+brief+'<br>正在加载「'+subjectLabel(s)+' · '+b+' · '+l+'」的二/三/四级目录…</div>';
}

// 科目下拉来自阶段一的全局目录索引，无需等待题目数据
function fillSubjectSelect(){
  const sel=$('subSel');
  const subs=(CATALOG.subjects||[]).slice().sort((a,b)=>
    (a==='chemistry'?-1:b==='chemistry'?1:subjectLabel(a).localeCompare(subjectLabel(b),'zh')));
  sel.innerHTML='<option value="">请选择科目</option>';
  if(!subs.length) subs.push('chemistry');
  subs.forEach(s=>{
    const o=document.createElement('option');
    o.value=s; o.textContent=subjectLabel(s);
    sel.appendChild(o);
  });
  // 默认不选科目：等用户主动选择后才加载该科目题目数据
  subject='';
  sel.value='';
}

function buildTagIndex(){
  tagIndex={}; groupIndex={};
  Object.entries(TAXONOMY).forEach(([combo,obj])=>{
    (obj.groups||[]).forEach(g=>{ groupIndex[g.id]={...g,_combo:combo}; });
    (obj.tags||[]).forEach(t=>{ tagIndex[t.id]={...t,_combo:combo}; });
  });
}
// 关键词检索命中后：由 qid 反查所属试卷与题目对象，便于复用勾选/预览/导出逻辑
function buildQidIndex(){
  qidIndex={};
  Object.entries(QUESTIONS).forEach(([pid,qs])=>{
    qs.forEach(q=>{ qidIndex[normQid(q.qid)] = {p:paperMap[pid]||null, q}; });
  });
}

/* ---------- 归一化 ---------- */
function normLevel(l){ return l==='alevel'||l==='a-level'?'a-level':l; }
// 切换科目时自动落到该科目有数据的考试局/阶段，避免下拉切换后一片空白
function firstComboForSubject(s){
  const d=(CATALOG.defaults||{})[s];
  if(d) return {board:d.board,level:normLevel(d.level)};
  const p=PAPERS.find(p=>p.subject===s);
  return p?{board:p.board,level:normLevel(p.qualification)}:null;
}
// 当前「考试局+阶段」组合下该科目是否有目录数据；用于选科目时保留默认组合
function comboHasData(s,b,l){
  if(!s) return false;
  const key=b+'|'+l+'|'+s;
  return !!((CATALOG.units||{})[key]||[]).length;
}
function setSeg(id,val){
  document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===val));
}
function normSession(s){ return SESSION_NORM[s]||s; }
function monthAbbr(s){ const n=normSession(s); return MONTH_ABBR[n]||n; }
function sessionMonth(s){ return monthAbbr(s); }  // 题号里的月份：三位字母
function qNo(q){ return String(q.question_number ?? q.qid.split('-').pop().replace(/^q/,'')); }
function comboKey(){ return (subject||'chemistry')+'|'+board+'|'+level; }
function taxonomy(){
  // 兼容老数据结构：优先按「科目|考试局|阶段」取，回退到旧的「考试局|阶段」键。
  return TAXONOMY[comboKey()] || TAXONOMY[board+'|'+level] || {groups:[],tags:[]};
}
function groupOf(tagId){ return tagIndex[tagId] ? groupIndex[tagIndex[tagId].group] : null; }
function isReviewGroup(g){ return !!g && REVIEW_RE.test((g.zh||'')+' '+(g.en||'')+' '+(g.id||'')); }
// 题目是否属于复习类（任一标签所属一级组为复习）
function isReviewQuestion(q){
  return (q.tags||[]).some(t=>isReviewGroup(groupOf(t)));
}
function isReviewTag(tagId){ return isReviewGroup(groupOf(tagId)); }
// 标签所属单元（可能多个）。tag 带 unit 字段，group 带 units/unit。
function tagUnits(tagId){
  const t=tagIndex[tagId];
  if(!t) return [];
  const g=groupOf(tagId)||{};
  let us = (g.units&&g.units.length) ? g.units : (t.unit ? [t.unit] : (g.unit?[g.unit]:[]));
  return us.filter(Boolean);
}
// 标签 id → 中文显示名（用于题号后的浅色备注）
function tagName(tagId){
  const t=tagIndex[tagId];
  return t ? (t.zh||t.en||tagId) : '';
}
// 主标签备注 HTML（浅色小字）
function tagNote(tagId){
  const n=tagName(tagId);
  return n ? `<span class="tagnote">${n}</span>` : '';
}
function subPrimary(sid){ const e=SUBTAGS[sid]; return e ? e.primary : null; }
// 大题主标签：各小问一致才给出；不一致返回 null（大题不备注）
function wholeQuestionPrimary(q){
  if(!q.primary_tag) return null;
  if(q.split && q.sub_ids && q.sub_ids.length){
    const prims=q.sub_ids.map(subPrimary).filter(Boolean);
    if(prims.length){
      const first=prims[0];
      if(!prims.every(x=>x===first)) return null;  // 小问标签不一致
      return first;
    }
  }
  return q.primary_tag;
}

function imagePath(paperId,qid){
  // qid 形如 {paperId}-q21（大题）或 {paperId}-q21-a（小问）。
  // 先剥离 paperId 前缀，再从剩余部分提取大题号 qNN：
  // 不能用 qid.split('-').pop()，否则小问末段(a/b/1)匹配不到 q\d+ 会错误回退到 01。
  let tail=String(qid);
  if(paperId && tail.startsWith(paperId+'-')) tail=tail.slice(paperId.length+1);
  const m=tail.match(/q(\d+)/i);
  const qnum=m?m[1]:'01';
  const p=paperMap[paperId]||{};
  let prefix=PREFIXES[paperId];
  if(!prefix){
    const code=(p.paper_code||'').toLowerCase().replace(/-(\d+)$/,'p$1');
    // 用试卷自身的 subject，避免多学科时把非化学卷拼成 chemistry 路径
    const subj=p.subject||'chemistry';
    prefix=`/files/${p.board}/${p.qualification}/${subj}/${p.year}-${p.session}-${code}/assets/`;
  }
  return prefix+`q${qnum.padStart(2,'0')}.webp`;
}
function layoutPath(imgPath){ return imgPath.replace(/\.webp$/i,'.layout.json'); }
function imgUrl(path){ return API + '/image?path='+encodeURIComponent(path); }
/* 打标台「按小问分别打标」的 Edexcel 题，每个小问有独立截图 qNN-<label>.webp：
 * 只含大题干 + 本小问题干，不显示其他小问。CIE/AQA 无此图，回退到父题整图。
 * 照搬打标台预览模式时优先用这张小问图。多段标签（如 a i）文件名用连字符：qNN-a-i.webp */
function subImagePath(p,q,sid,subLabel){
  if(!sid || !p || p.board!=='edexcel' || !isSplitTagged(q)) return null;
  if(!subLabel) return null;
  const fname=String(subLabel).trim().toLowerCase().replace(/\s+/g,'-');
  if(!/^[a-z0-9-]+$/.test(fname)) return null;
  const base=imagePath(p.id,q.qid);   // .../qNN.webp
  return base.replace(/\.webp$/i,'-'+fname+'.webp');
}
function previewImagePath(p,q,sid,subLabel){
  return subImagePath(p,q,sid,subLabel) || imagePath(p.id, sid||q.qid);
}

/* ---------- 小问可视内容解析（Edexcel 按小问打标专用） ----------
 * 核查要求：选中部分小问时绝不允许把整题图放上。
 * 1) 优先用小问独立截图 qNN-<label>.webp（探测其真实存在，缓存结果）；
 * 2) 缺失时用 layout.json（只读）裁出「题干 + 本小问」区域合成展示/导出；
 * 3) layout 也缺失时返回 null（调用方跳过该小问并明确提示，不退回整题图）。
 */
const layoutCache = new Map();          // 父图路径 -> layout JSON 的 Promise
function loadLayout(imgPath){
  if(!imgPath) return Promise.resolve(null);
  if(layoutCache.has(imgPath)) return layoutCache.get(imgPath);
  const pr=fetch(API+'/layout?path='+encodeURIComponent(layoutPath(imgPath)))
    .then(r=>r.ok?r.json():null).catch(()=>null);
  layoutCache.set(imgPath,pr);
  return pr;
}
// 小问独立截图是否存在（loadImgDim 有缓存，探测只发生一次）
function probeImgExists(path){
  if(!path) return Promise.resolve(false);
  return loadImgDim(path).then(d=>d.w>0);
}
function flatSubs(layout){
  return ((layout&&layout.subquestions)||[]).filter(s=>s&&s.box&&typeof s.box.y==='number')
    .map(s=>({label:String(s.label||'').trim(),y:Math.max(0,Math.min(1,s.box.y))}));
}
function labelSegs(l){ return l.split(/\s+/).filter(Boolean); }
function isDescLabel(l,base){ const a=labelSegs(base),b=labelSegs(l); return b.length>a.length && b.slice(0,a.length).join(' ')===a.join(' '); }
// 小问 subLabel 的内容区域 [y0,y1)：起点=自身标记线，终点=下一个「非后代」小问的标记线（末小问到图底）
function subRegionBounds(layout,subLabel){
  const subs=flatSubs(layout); if(!subs.length) return null;
  const norm=s=>s.toLowerCase().replace(/\s+/g,' ');
  const t=norm(String(subLabel||'').trim());
  let idx=subs.findIndex(s=>norm(s.label)===t);
  if(idx<0) return null;
  const y0=subs[idx].y;
  let y1=1;
  for(let j=idx+1;j<subs.length;j++){
    if(!isDescLabel(subs[j].label,subs[idx].label)){ y1=subs[j].y; break; }
  }
  return y1>y0?[y0,y1]:null;
}
// 题干区域 [0, 第一个小问标记线)
function stemRegion(layout){
  const subs=flatSubs(layout);
  if(!subs.length) return null;
  return subs[0].y>0.004?[0,subs[0].y]:null;
}
// 整题长图的安全切点（归一化）：所有小问标记线处，保证任一小问不被切到两页
function layoutCutsNorm(layout){
  return flatSubs(layout).map(s=>s.y).filter(y=>y>0.01&&y<0.99);
}
// 解析某小问的可视内容：{type:'img',img} | {type:'crop',img,regions} | null
// opts.noStem=true：相邻续接（同一大题的题干已由上一条显示），只取本小问区域；
// 优先用父图 layout 裁剪（无题干），布局缺失时回退小问独立截图（含题干，明确降级）。
async function resolveSubVisual(p,q,sid,subLabel,opts){
  const noStem=!!(opts&&opts.noStem);
  if(!sid || !p || p.board!=='edexcel' || !isSplitTagged(q)){
    return {type:'img',img:imagePath(p.id,sid||q.qid)};
  }
  const parent=imagePath(p.id,q.qid);
  const m=String(sid||'').match(/-q\d+-(.+)$/);   // sid 尾部即小问标签（a-i → a i）
  const label=subLabel || (m?m[1].replace(/-/g,' '):null);
  if(noStem){
    const layout=await loadLayout(parent);
    const sub=subRegionBounds(layout,label);
    if(sub) return {type:'crop',img:parent,regions:[sub]};   // 只取本小问，不再重复题干
    const subImg=subImagePath(p,q,sid,subLabel);
    if(subImg && await probeImgExists(subImg)) return {type:'img',img:subImg};
    return null;   // 布局与小问截图都缺失：交由调用方跳过并提示
  }
  const subImg=subImagePath(p,q,sid,subLabel);
  if(subImg && await probeImgExists(subImg)) return {type:'img',img:subImg};
  // 小问独立截图缺失（如 a-level 数学/会计/商务全部缺失）：layout 裁剪「题干+本小问」
  const layout=await loadLayout(parent);
  const stem=stemRegion(layout), sub=subRegionBounds(layout,label);
  if(sub && stem) return {type:'crop',img:parent,regions:[stem,sub]};
  if(sub) return {type:'crop',img:parent,regions:[sub]};   // 无题干区域时只取本小问
  return null;   // 布局缺失：无法单独截取，交由调用方跳过并提示
}
function makeNo(p,qnum,sub){
  let s=`${BOARD_CODE[p.board]||'?'}-${p.year}-${sessionMonth(p.session)}-${p.paper_code}`;
  if(qnum){ const n=String(qnum); s+=`-Q${/^Q/i.test(n)?n.slice(1):n}`; }  // 大题号前加 Q（幂等）
  if(sub)s+=`-${sub}`;  // 小题号不加 Q
  return s;
}

/* ---------- 筛选 ---------- */
function filteredPapers(){
  return PAPERS.filter(p=>p.board===board && normLevel(p.qualification)===level
    && (!subject || p.subject===subject));
}

/* ---------- 树基础构件（默认折叠） ----------
 * 每一行都由「固定宽度勾选框槽 + 固定宽度展开槽 + 标签」组成：
 * 无论该层级是否有勾选框、是否可展开，标签起始位置完全一致，题号自然左对齐。
 */
function nodeRow(opts){
  const row=el('div','row'+(opts.onClick?' pick':''));
  // 槽一：勾选框（无勾选框的行留空占位）
  const cbSlot=el('span','cb-slot');
  if(opts.checkbox) makeCheckbox(opts,cbSlot);
  row.appendChild(cbSlot);
  // 槽二：展开箭头（无子节点的行留空占位）
  const twSlot=el('span','tw-slot');
  if(opts.twist){
    const tw=el('span','twist'); tw.textContent=opts.open?'▾':'▸';
    if(opts.open && opts.children) opts.children.classList.remove('hidden');
    tw.dataset.twist='1';
    tw.onclick=e=>{
      e.stopPropagation();
      toggleNode(row,opts.children,tw);
      if(opts.onOpen && tw.textContent==='▾') opts.onOpen();
    };
    twSlot.appendChild(tw);
  }
  row.appendChild(twSlot);
  // 槽三：标签（题号本体）
  const lab=el('span','label '+(opts.cls||'')+(opts.diff?' diff-'+opts.diff:''));
  if(opts.diffKey){
    lab.dataset.diffkey=opts.diffKey;
    if(opts.diffFallback) lab.dataset.difffb=opts.diffFallback;
    if(opts.title) lab.dataset.basetitle=opts.title;
  }
  if(opts.title) lab.title=opts.title;
  else if(opts.diff) lab.title='难度：'+(DIFF_NAME[opts.diff]||opts.diff);
  if(opts.labelHtml) lab.innerHTML=opts.labelHtml; else lab.textContent=opts.label;
  if(opts.onClick){ lab.onclick=e=>{e.stopPropagation();opts.onClick();}; }
  row.appendChild(lab);
  if(opts.onRowClick){ row.onclick=e=>opts.onRowClick(e); }
  return row;
}
function makeCheckbox(opts,parent){
  const cb=el('input'); cb.type='checkbox'; cb.dataset.key=opts.key||'';
  if(opts.groupKeys && opts.groupKeys.length) cb.dataset.groupKeys=opts.groupKeys.join(',');
  if(cb.dataset.groupKeys){
    // 大题勾选框：全部小题选中→勾选；部分选中→半选
    const keys=cb.dataset.groupKeys.split(',');
    const hit=keys.filter(k=>selected.some(s=>s.key===k)).length;
    cb.checked=keys.length>0 && hit===keys.length;
    cb.indeterminate=hit>0 && hit<keys.length;
  } else {
    cb.checked=opts.checked!==undefined?opts.checked:selected.some(s=>s.key===opts.key);
  }
  cb.onchange=e=>{e.stopPropagation();opts.onCheck&&opts.onCheck(cb);};
  parent.appendChild(cb);
  return cb;
}
function toggleNode(row,kids,tw){
  const hidden=kids.classList.toggle('hidden');
  tw.textContent=hidden?'▸':'▾';
  if(row) row.classList.toggle('collapsed',hidden);
}
function childrenBox(hidden=true){ const c=el('div','children'+(hidden?' hidden':'')); return c; }

function buildTree(){
  const root=$('tree'); root.innerHTML='';
  if(loading) return;
  if(!subject){ showSubjectHint(); return; }
  if(loadedTreeKey!==treeKey(subject,board,level)){ showTreeLoading(subject,board,level); return; }
  const papers=filteredPapers();
  if(!papers.length){ root.innerHTML='<div class="list-empty">当前筛选下没有试卷</div>'; return; }
  if(keyword && !papers.some(p=>(QUESTIONS[p.id]||[]).some(matchKeyword))){
    const msg=el('div','list-empty'); msg.textContent='没有找到题干包含「'+keyword+'」的题目'; root.appendChild(msg); return;
  }
  if(mode==='category'){ buildCategoryTree(root,papers); return; }

  // 先按难度+关键词过滤题目；空卷/空年月不再出现在左栏
  const visibleByPaper={};
  papers.forEach(p=>{
    visibleByPaper[p.id]=(QUESTIONS[p.id]||[]).filter(q=>matchDifficulty(q)&&matchKeyword(q)).slice()
      .sort((a,b)=>(isReviewQuestion(b)?1:0)-(isReviewQuestion(a)?1:0));
  });
  const shownPapers=papers.filter(p=>visibleByPaper[p.id].length);
  if(!shownPapers.length){ root.innerHTML='<div class="list-empty">当前难度/关键词筛选下没有题目</div>'; return; }
  const groups={};
  shownPapers.forEach(p=>{ const key=`${p.year}-${normSession(p.session)}`; (groups[key]=groups[key]||[]).push(p); });
  const keys=Object.keys(groups).sort((a,b)=>b.localeCompare(a));
  keys.forEach(gk=>{
    const wrap=el('div','node');
    const [y,s]=gk.split('-');
    const yKids=childrenBox();
    const yRow=nodeRow({twist:true,label:`${y} ${sessionLabel(s)}`,cls:'year',children:yKids,
      onOpen:()=>ensureDifficulty(paperScope(gk))});   // 展开年月（一级目录）时加载该目录难度
    wrap.append(yRow,yKids);
    groups[gk].forEach(p=>{
      const qs=visibleByPaper[p.id];
      const pWrap=el('div','node');
      // 按套卷：套卷层既能整卷勾选，又能展开浏览大题/小问；套卷本身保持折叠
      const pKids=childrenBox();
      const pRow=nodeRow({
        twist:true, checkbox:true, key:`${p.id}|WHOLE`,
        label:`${p.paper_code} · ${qs.length}题`, cls:'paper', children:pKids,
        onCheck:cb=>toggleWhole(cb,p), onClick:()=>previewPaper(p),
        onOpen:()=>preloadPaperImages(p)   // 展开卷号（二级目录）→ 预取本卷全部题目图片
        // 难度已在展开年月（一级目录）时按目录加载，无需在卷号层重复请求
      });
      pWrap.append(pRow,pKids);
      qs.forEach(q=>{
        const qWrap=el('div','node');
        // 点开套卷后，大题下的小问默认展开
        const qKids=childrenBox(false);
        const qLabel=`Q${qNo(q)}${isReviewQuestion(q)?' ★复习':''}`;
        if(q.split && q.sub_ids && q.sub_ids.length){
          const subIds=visibleSubIds(q);
          const wp=wholeQuestionPrimary(q);
          const qLabelHtml=qLabel+(wp?tagNote(wp):'');
          const qRow=nodeRow({
            twist:true, checkbox:true,
            key:`Q|${q.qid}`, groupKeys:subIds,
            label:qLabel, labelHtml:qLabelHtml, cls:'qno', children:qKids, open:true,
            onCheck:cb=>toggleWholeQuestion(cb,p,q,subIds),
            onClick:()=>previewQuestion(p,q,null,null)
          });
          qWrap.append(qRow,qKids);
          subIds.forEach(sid=>{
            const i=q.sub_ids.indexOf(sid);
            const subLabel=q.sub_labels&&q.sub_labels[i]?q.sub_labels[i]:String.fromCharCode(97+i);
            const sp=subPrimary(sid);
            const sLabel=`(${subLabel})`;
            const sRow=nodeRow({
              checkbox:true,key:sid,label:sLabel,labelHtml:sLabel+(sp?tagNote(sp):''),cls:'part',indent:true,
              diff:diffOf(sid), diffKey:sid,
              onCheck:cb=>toggleQuestion(cb,p,q,sid,subLabel),onClick:()=>previewQuestion(p,q,sid,subLabel)
            });
            qKids.appendChild(sRow);
          });
        } else {
          const wp=wholeQuestionPrimary(q);
          const qRow=nodeRow({
            checkbox:true,key:q.qid,label:qLabel,labelHtml:qLabel+(wp?tagNote(wp):''),cls:'qno',
            diff:diffOf(q.qid,q.difficulty), diffKey:q.qid, diffFallback:q.difficulty,
            onCheck:cb=>toggleQuestion(cb,p,q,null,null),onClick:()=>previewQuestion(p,q,null,null)
          });
          qWrap.appendChild(qRow);
        }
        pKids.appendChild(qWrap);
      });
      yKids.appendChild(pWrap);
    });
    root.appendChild(wrap);
  });
}
function sessionLabel(s){
  return monthAbbr(s);  // 与题号一致，统一三位字母
}

/* ---------- 分类模式（v2）：单元(卷) → 一级标签 → 二级标签(带勾选) → 题号 ----------
 * 数据源 taxonomy_v2.json：combo|subject|unit → {groups, tags}，来自打标台真实试卷 unit。
 * - 单元取题目所属试卷的真实 unit，展示名经 UNIT_DISPLAY 映射（edexcel-igcse 全并为 U12；CIE P14→P1~P4）。
 * - 同一标签/题目在多个单元出现时，复制到每个所属单元。
 * - 无标签题归入各单元下的「未分配题目」三级类目，不再有顶层「未分类」单元。
 */
function unitOfPaper(p){
  const combo=board+'|'+level;
  const raw=p.unit||'';
  const disp=(UNIT_DISPLAY[combo]||{})[raw]||raw||'其他';
  return disp;
}
// 题目是否匹配当前难度筛选：整题难度优先；按小问打标的题，任一小问命中即算匹配
function matchDifficulty(q){
  if(diffFilter==='all') return true;
  if(diffOf(q.qid,q.difficulty)===diffFilter) return true;
  if(q.split && q.sub_ids && q.sub_ids.some(sid=>diffOf(sid)===diffFilter)) return true;
  return false;
}
// 纸卷模式下实际展示的小问：整题命中则全显；仅小问命中则只显示命中的小问
function visibleSubIds(q){
  const ids=(q && q.sub_ids)||[];
  if(diffFilter==='all' || diffOf(q.qid,q.difficulty)===diffFilter) return ids;
  return ids.filter(sid=>diffOf(sid)===diffFilter);
}
// 题干关键词：题干取自题库 Markdown（含各小问），旧 qid 归一化后匹配；空格分隔的词需全部命中
function matchKeyword(q){
  if(!keyword) return true;
  const t=(TEXT[normQid(q.qid)]||'').toLowerCase();
  if(!t) return false;
  return keyword.split(/\s+/).every(w=>t.includes(w));
}
/* 按类别模式的条目：整题打标→整题一条；按小问分别打标→每个小问一条。
 * 小问的标签/难度取自只读快照 submeta.json.gz；缺失时退回整题标签，避免题目在分类树里消失。
 * 展示形式参照打标台预览模式：小问用 Q1(a) 紧凑标签，完整题号放在鼠标提示里。
 */
function subMetaOf(sid){ return SUBMETA[normQid(sid)] || null; }
function isSplitTagged(q){ return !!(q && q.split && q.sub_ids && q.sub_ids.length); }
function makeCategoryItem(p,q,sid,subLabel,fallbackTags){
  const key=sid||q.qid;
  const no=makeNo(p,qNo(q),subLabel);
  const sm=sid?subMetaOf(sid):null;
  const tags=sid ? ((sm && sm.tags && sm.tags.length)?sm.tags:(fallbackTags||[])) : (q.tags||[]);
  const diff=sid ? diffOf(sid) : (isSplitTagged(q)?null:diffOf(q.qid,q.difficulty));
  return {
    p,q,sid:sid||null,subLabel:subLabel||null,key,no,tags,diff,
    diffKey:sid||q.qid, diffFallback:sid?null:(isSplitTagged(q)?null:q.difficulty),
    // 所有题号统一完整格式；小问在末尾用 -a / -b 标注，整题与小问左对齐
    label:no+(isReviewQuestion(q)?' ★复习':'')
  };
}
function categoryItems(p,q){
  if(!isSplitTagged(q)) return [makeCategoryItem(p,q,null,null)];
  return q.sub_ids.map((sid,i)=>{
    const sm=subMetaOf(sid);
    const label=q.sub_labels&&q.sub_labels[i] ? q.sub_labels[i] : String.fromCharCode(97+i);
    return makeCategoryItem(p,q,sid,label,sm?null:q.tags);
  });
}
function itemToSelected(it){
  return {
    key:it.key, label:it.sid?it.no:it.label, no:it.no,
    img:previewImagePath(it.p,it.q,it.sid,it.subLabel),
    ref:{p:it.p,q:it.q,sub:it.sid,subLabel:it.subLabel}
  };
}
function matchItemDifficulty(it){ return diffFilter==='all' || it.diff===diffFilter; }
function buildCategoryTree(root,papers){
  const combo=board+'|'+level;
  const subjObj=(TAX_V2[combo]||{})[subject]||{};   // rawUnit -> {groups,tags}

  // 按「展示单元」聚合：合并同一展示单元下所有 raw unit 的标签体系（id 去重）
  const unitGroups={}, unitTags={};   // dispUnit -> {gid:tag...} / {tid:tag...}
  const dispOfRaw={};
  Object.keys(subjObj).forEach(raw=>{
    const disp=unitOfPaper({unit:raw});
    dispOfRaw[raw]=disp;
    const obj=subjObj[raw]||{};
    unitGroups[disp]=unitGroups[disp]||{};
    unitTags[disp]=unitTags[disp]||{};
    (obj.groups||[]).forEach(g=>unitGroups[disp][g.id]=g);
    (obj.tags||[]).forEach(t=>unitTags[disp][t.id]=t);
  });

  // 词表顺序：打标台 taxonomy 词表里 tag 的先后次序（tags 数组下标）。
  // 一道题标注多个分类标签时，只归入其中「词表顺序最靠后」的那个条目，保证题号仅出现一次。
  // 用全局 taxonomy 顺序而非单元内拼接顺序，确保与打标台词表完全一致。
  const wordOrder={};
  (taxonomy().tags||[]).forEach((t,i)=>{ wordOrder[t.id]=i; });
  const wordRank=tid=>(tid in wordOrder)?wordOrder[tid]:-1;

  // 收集题目：按展示单元归属；byUnitTag[disp][tid] = [题...]；无标签题 → untaggedByUnit[disp]
  const byUnitTag={}, untaggedByUnit={};
  papers.forEach(p=>{
    const disp=unitOfPaper(p);
    (QUESTIONS[p.id]||[]).forEach(q=>{
      if(!matchKeyword(q)) return;   // 关键词按整题（含小问）题干过滤
      categoryItems(p,q).forEach(item=>{
        if(!matchItemDifficulty(item)) return;   // 小问级难度筛选
        const list=item.tags||[];
        if(!list.length){ (untaggedByUnit[disp]=untaggedByUnit[disp]||[]).push(item); return; }
        // 候选仅取本单元词表中存在的标签；在其中选词表顺序最靠后的一个，只放一次
        const valid=list.filter(tid=>unitTags[disp] && unitTags[disp][tid]);
        let tid;
        if(!valid.length){
          tid=list[0];   // 标签不在本单元词表：仍归首个标签，避免题目丢失
        } else {
          tid=valid[0];
          valid.forEach(t=>{ if(wordRank(t)>=wordRank(tid)) tid=t; });
        }
        const bucket=(byUnitTag[disp]=byUnitTag[disp]||{});
        (bucket[tid]=bucket[tid]||[]).push(item);
      });
    });
  });

  // 单元列表 = 标签体系里的单元 ∪ 有题的单元（保证有题但无标签体系的单元也可见）
  const uSet=new Set(Object.keys(unitGroups));
  Object.keys(byUnitTag).forEach(u=>uSet.add(u));
  Object.keys(untaggedByUnit).forEach(u=>uSet.add(u));
  const units=Array.from(uSet).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));

  if(!units.length){ root.innerHTML='<div class="list-empty">当前范围没有分类数据</div>'; return; }

  units.forEach(unit=>{
    const uWrap=el('div','node');
    const uKids=childrenBox();
    const uRow=nodeRow({twist:true,label:`单元 ${unit}`,cls:'year',children:uKids,
      onOpen:()=>ensureDifficulty(categoryScope(unit))});   // 展开单元（一级目录）时加载该目录难度
    uWrap.append(uRow,uKids);

    const groups=Object.values(unitGroups[unit]||{});
    const tags=Object.values(unitTags[unit]||{});
    const tagsByGroup={};
    tags.forEach(t=>{ (tagsByGroup[t.group]=tagsByGroup[t.group]||[]).push(t); });

    // 一级标签；复习类排最前
    groups.sort((a,b)=>(isReviewGroup(b)?1:0)-(isReviewGroup(a)?1:0));
    groups.forEach(g=>{
      const gItems=[];
      (tagsByGroup[g.id]||[]).forEach(t=>{
        const items=(byUnitTag[unit]&&byUnitTag[unit][t.id])||[];
        items.forEach(it=>gItems.push({t,item:it}));
      });
      if(!gItems.length) return;   // 只显示有题的一级标签
      const gWrap=el('div','node');
      const gKids=childrenBox();
      const gRow=nodeRow({twist:true,label:(isReviewGroup(g)?'★ ':'')+(g.zh||g.id),cls:'paper',children:gKids,
        onOpen:()=>preloadGroupImages(unit,g.id,tagsByGroup,byUnitTag)});  // 展开一级标签（二级目录）→ 预取图片
      gWrap.append(gRow,gKids);
      (tagsByGroup[g.id]||[]).forEach(t=>{
        const items=(byUnitTag[unit]&&byUnitTag[unit][t.id])||[];
        if(!items.length) return;  // 只显示有题的二级标签
        const tWrap=el('div','node');
        const tKids=childrenBox();
        const tagKey='TAGSEL|'+unit+'|'+t.id;
        const allSel=items.every(it=>selected.some(s=>s.key===it.key));
        const tRow=nodeRow({
          twist:true, checkbox:true, key:tagKey, checked:allSel,
          label:(isReviewGroup(tagIndex[t.id]?groupOf(t.id):null)||REVIEW_RE.test((t.zh||'')+' '+(t.en||'')+' '+(t.id||''))?'★ ':'')+(t.zh||t.id)+(items.length?` (${items.length})`:''),
          cls:'qno', children:tKids,
          onCheck:cb=>toggleTagAll(cb,t.id,items),
          onClick:()=>{}
          // 难度已在展开单元（一级目录）时按目录加载
        });
        tWrap.append(tRow,tKids);
        items.sort((a,b)=>(isReviewQuestion(b.q)?1:0)-(isReviewQuestion(a.q)?1:0));
        items.forEach(it=>addCategoryLeaf(tKids,it));
        gKids.appendChild(tWrap);
      });
      uKids.appendChild(gWrap);
    });

    // 无标签题：该单元下的普通三级类目（非顶层「未分类」）
    const untagged=untaggedByUnit[unit]||[];
    if(untagged.length){
      const gWrap=el('div','node');
      const gKids=childrenBox();
      const gRow=nodeRow({twist:true,label:'未分配题目',cls:'paper',children:gKids,
        onOpen:()=>preloadItemsImages(untagged)});
      gWrap.append(gRow,gKids);
      const tWrap=el('div','node');
      const tKids=childrenBox();
      const tagKey='TAGSEL|'+unit+'|__unassigned__';
      const allSel=untagged.every(it=>selected.some(s=>s.key===it.key));
      const tRow=nodeRow({twist:true,checkbox:true,key:tagKey,checked:allSel,
        label:`暂无二级标签 (${untagged.length})`,cls:'qno',children:tKids,
        onCheck:cb=>toggleTagAll(cb,'__unassigned__',untagged),onClick:()=>{} });
      tWrap.append(tRow,tKids);
      untagged.forEach(it=>addCategoryLeaf(tKids,it));
      gKids.appendChild(tWrap);
      uKids.appendChild(gWrap);
    }
    root.appendChild(uWrap);
  });
}
function addCategoryLeaf(parent,it){
  const leaf=nodeRow({
    checkbox:true,key:it.key,
    label:it.label, cls:'leafno'+(it.sid?' subleaf':''),
    diff:it.diff, diffKey:it.diffKey, diffFallback:it.diffFallback,
    title:it.no+(it.diff?' · 难度：'+(DIFF_NAME[it.diff]||it.diff):''),
    onCheck:cb=>{ if(cb.checked) addSelected(itemToSelected(it)); else removeSelected(it.key); },
    onClick:()=>previewQuestion(it.p,it.q,it.sid,it.subLabel)
  });
  parent.appendChild(leaf);
}
function toggleTagAll(cb,tagId,items){
  if(cb.checked){
    items.forEach(it=>{
      if(!selected.some(s=>s.key===it.key)) selected.push(itemToSelected(it));
    });
  } else {
    const keys=new Set(items.map(it=>it.key));
    selected=selected.filter(s=>!keys.has(s.key));
  }
  renderSel(); syncChecks(); updateStatus(); markChange();
}

/* ---------- 关键词模式：从题目文字库流式检索，结果按整题题号显示 ---------- *
 * 后端 /api/search 按 科目/考试局/阶段 选中对应 JSONL，命中即逐条返回（NDJSON），
 * 先按单元（卷）、再按年月排序。这里收到一条就渲染一条，不等全部完成。
 */
let kwSeq = 0;                 // 检索序号：切换条件/模式时自增，丢弃过期响应
let kwAbort = null;            // 当前 fetch 控制器
let kwImgPaths = [];           // 当前结果需要预载的题图路径
let kwDone = false;            // 当前搜索是否已全部接收完成
function kwStop(){ kwSeq++; if(kwAbort){ try{kwAbort.abort();}catch(e){} kwAbort=null; } }
// 由检索记录构造整题题号：E-2019-Jan-wch11-Q16（与小问无关，整题口径）
function kwWholeNo(r){
  const p = {board:r.board, year:r.year, session:r.session, paper_code:r.paper_code};
  return makeNo(p, r.question_number, null);
}
// 关键词结果是否匹配当前难度：优先用目录题对象（支持小问级难度），否则用文字库字段
function keywordHitMatchDifficulty(r){
  if(diffFilter==='all') return true;
  const hit=qidIndex[normQid(r.qid)]||null;
  if(hit && hit.q && matchDifficulty(hit.q)) return true;
  const d=r.difficulty||(hit&&hit.q&&hit.q.difficulty)||'';
  return d===diffFilter;
}
// 构造一条关键词结果行；顺带收集题图路径（供搜索完成后统一预载）
// 按小问分别打标的题（q.split）：大题行可展开、可整组勾选，小问行逐个勾选/预览
// （与按套卷模式同一套勾选逻辑）；目录数据缺失时退回整题一行。
function keywordHitNode(r){
  const no=kwWholeNo(r);
  const hit=qidIndex[normQid(r.qid)]||null;
  const p=hit?hit.p:null, q=hit?hit.q:null;
  const key='KW|'+normQid(r.qid);
  const img=r.image_path||(p&&q?imagePath(p.id,q.qid):'');
  const d=(q&&diffOf(q.qid,q.difficulty))||r.difficulty||null;
  if(p&&q&&isSplitTagged(q)&&q.sub_ids&&q.sub_ids.length){
    const subIds=visibleSubIds(q);
    const wp=wholeQuestionPrimary(q);
    const wrap=el('div','node');
    const kids=childrenBox();   // 默认折叠：点展开箭头显示小问
    const row=nodeRow({
      twist:true, checkbox:true, key:'Q|'+q.qid, groupKeys:subIds,
      label:no, labelHtml:no+(wp?tagNote(wp):''), cls:'leafno', children:kids,
      diff:d, diffKey:q.qid, diffFallback:r.difficulty||(q.difficulty||''),
      title:no+(d?' · 难度：'+(DIFF_NAME[d]||d):''),
      onCheck:cb=>toggleWholeQuestion(cb,p,q,subIds),
      onClick:()=>previewQuestion(p,q,null,null),
      onOpen:()=>{   // 展开大题：预取小问独立截图候选（Edexcel，缺失的路径静默失败）
        preloadImages(subIds.map((sid,i)=>{
          const idx=q.sub_ids.indexOf(sid);
          const lb=q.sub_labels&&q.sub_labels[idx]?q.sub_labels[idx]:String.fromCharCode(97+idx);
          return subImagePath(p,q,sid,lb);
        }).filter(Boolean));
      }
    });
    wrap.append(row,kids);
    subIds.forEach((sid,i)=>{
      const idx=q.sub_ids.indexOf(sid);
      const subLabel=q.sub_labels&&q.sub_labels[idx]?q.sub_labels[idx]:String.fromCharCode(97+idx);
      const sp=subPrimary(sid);
      const sLabel=`(${subLabel})`;
      const sRow=nodeRow({
        checkbox:true,key:sid,label:sLabel,labelHtml:sLabel+(sp?tagNote(sp):''),cls:'part',
        diff:diffOf(sid),diffKey:sid,
        onCheck:cb=>toggleQuestion(cb,p,q,sid,subLabel),
        onClick:()=>previewQuestion(p,q,sid,subLabel)
      });
      kids.appendChild(sRow);
    });
    if(img && kwImgPaths.indexOf(img)<0) kwImgPaths.push(img);
    return wrap;
  }
  const leaf=nodeRow({
    checkbox:true, key, label:no, cls:'leafno',
    diff:d, diffKey:q?q.qid:normQid(r.qid), diffFallback:r.difficulty||(q&&q.difficulty)||'',
    title:no+(d?' · 难度：'+(DIFF_NAME[d]||d):''),
    onCheck:cb=>{
      if(cb.checked){
        if(p&&q) addSelected({key,label:no,no,img,ref:{p,q,sub:null,subLabel:null}});
        else toast('该题目录数据未加载，无法勾选');
      } else removeSelected(key);
    },
    onClick:()=>{
      if(!img){ toast('暂无题图'); return; }
      current={label:no,html:sheetHTML(img)}; showPreview();
    }
  });
  if(img && kwImgPaths.indexOf(img)<0) kwImgPaths.push(img);
  return leaf;
}
// 对已保存的关键词结果按当前难度重筛：不重新请求，原有排序不变
function renderKeywordHits(preload){
  const root=$('tree'); root.innerHTML='';
  let count=0;
  const paths=[];
  kwHits.forEach(r=>{
    if(!keywordHitMatchDifficulty(r)) return;
    const before=kwImgPaths.length;
    root.appendChild(keywordHitNode(r));
    if(kwImgPaths.length>before) paths.push(kwImgPaths[kwImgPaths.length-1]);
    count++;
  });
  if(!count){
    root.innerHTML='<div class="list-empty">'+(kwHits.length
      ? '当前难度下没有匹配的题目'
      : '输入关键词后点击「搜索」<br>结果按单元（卷）、年月排序')+'</div>';
  }
  $('treeMode').textContent='按关键词';
  if(preload && paths.length) preloadImages(paths);
  updateStatus();
}
function showKeywordBlank(){
  kwHits=[]; kwImgPaths=[]; kwDone=false; kwTerm='';
  $('tree').innerHTML='<div class="list-empty">输入关键词后点击「搜索」<br>结果按单元（卷）、年月排序</div>';
}
// 流式执行检索：命中一条立即追加一条；全部检索完成后再统一预载命中题图
async function runKeywordSearch(){
  const term = $('kwInput').value.trim();
  if(!term){ toast('请输入关键词'); return; }
  if(!subject){ toast('请先在上方选择「科目」'); return; }
  kwStop();
  const seq = kwSeq;
  const root = $('tree');
  root.innerHTML='';
  selected=[]; activeIdx=-1; current=null; renderSel(); updateStatus();
  $('previewLabel').textContent='—';
  $('paperView').innerHTML='<div class="empty-preview">点击左侧题号<br>即可在此预览截图</div>';
  kwHits=[]; kwImgPaths=[]; kwDone=false; kwTerm=term;
  const ctrl = new AbortController(); kwAbort = ctrl;
  const url = API+'/search?q='+encodeURIComponent(term)
    +'&subject='+encodeURIComponent(subject)
    +'&board='+encodeURIComponent(board)
    +'&level='+encodeURIComponent(level);
  let count=0, loadingRow=null;
  const ensureLoading=()=>{
    if(!loadingRow){ loadingRow=el('div','list-empty'); loadingRow.textContent='正在检索…'; root.appendChild(loadingRow); }
  };
  const appendHit=(r)=>{
    kwHits.push(r);                       // 先存原始命中，难度切换时可直接重筛
    if(!keywordHitMatchDifficulty(r)) return;
    if(loadingRow && loadingRow.parentNode) loadingRow.remove();
    loadingRow=null;
    count++;
    root.appendChild(keywordHitNode(r));
  };
  ensureLoading();
  try{
    const resp = await fetch(url,{signal:ctrl.signal});
    if(!resp.ok) throw new Error('HTTP '+resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf='';
    while(true){
      const {done,value} = await reader.read();
      if(seq!==kwSeq) return;   // 已被新一轮检索/模式切换取代
      if(done) break;
      buf += dec.decode(value,{stream:true});
      let nl;
      while((nl=buf.indexOf('\n'))>=0){
        const line=buf.slice(0,nl); buf=buf.slice(nl+1);
        if(!line.trim()) continue;
        let d; try{ d=JSON.parse(line); }catch(e){ continue; }
        if(d.__done) continue;
        appendHit(d);
      }
    }
    if(seq!==kwSeq) return;
    if(buf.trim()){
      let d; try{ d=JSON.parse(buf); }catch(e){}
      if(d && !d.__done) appendHit(d);
    }
    kwDone=true;
    $('treeMode').textContent = '按关键词';
    if(!count){
      root.innerHTML='<div class="list-empty">'+(kwHits.length
        ? '当前难度下没有匹配的题目'
        : '没有找到题干包含「'+term+'」的题目')+'</div>';
    } else if(loadingRow && loadingRow.parentNode){ loadingRow.remove(); }
    // 检索全部完成后再启动图片预载：先显示结果列表，随后后台按并发队列预载题图
    if(kwImgPaths.length) preloadImages(kwImgPaths);
    updateStatus(); markChange();
  }catch(e){
    if(e.name==='AbortError' || seq!==kwSeq) return;
    kwDone=false;
    root.innerHTML='<div class="list-empty">检索失败：'+e.message+'</div>';
  }
}

/* ---------- 选择逻辑 ---------- */
function addSelected(item){ selected.push(item); renderSel(); syncChecks(); updateStatus(); markChange(); }
function removeSelected(key){ selected=selected.filter(s=>s.key!==key); renderSel(); syncChecks(); updateStatus(); markChange(); }
function toggleWhole(cb,p){
  const key=`${p.id}|WHOLE`;
  if(cb.checked){
    const no=makeNo(p,null,null);
    addSelected({key,label:`${p.year} ${sessionLabel(p.session)} ${p.paper_code} 整卷`,no,img:'',ref:{p,whole:true}});
  } else removeSelected(key);
}
function toggleQuestion(cb,p,q,sid,subLabel){
  const key=sid||q.qid;
  if(cb.checked){
    const no=makeNo(p,qNo(q),subLabel);
    addSelected({key,label:no,no,img:imagePath(p.id,sid||q.qid),ref:{p,q,sub:sid,subLabel}});
  } else removeSelected(key);
}
// 按套卷模式下勾选大题：把该题所有小问一次性加入/移出清单
function toggleWholeQuestion(cb,p,q,visible){
  const subs=visible&&visible.length?visible:(q.sub_ids||[]);   // 难度筛选下只勾选当前可见小问
  if(cb.checked){
    const items=[];
    subs.forEach((sid,i)=>{
      if(!sid || selected.some(s=>s.key===sid)) return;
      const subLabel=q.sub_labels&&q.sub_labels[i]?q.sub_labels[i]:String.fromCharCode(97+i);
      const no=makeNo(p,qNo(q),subLabel);
      items.push({key:sid,label:no,no,img:imagePath(p.id,sid),ref:{p,q,sub:sid,subLabel}});
    });
    if(items.length){ selected.push(...items); renderSel(); updateStatus(); markChange(); }
  } else {
    const keys=new Set(subs);
    selected=selected.filter(s=>!keys.has(s.key));
    renderSel(); updateStatus(); markChange();
  }
  syncChecks();
}
function syncChecks(){
  document.querySelectorAll('#tree input[type=checkbox]').forEach(cb=>{
    if(!cb.dataset.key) return;
    if(cb.dataset.groupKeys){
      const keys=cb.dataset.groupKeys.split(',');
      const hit=keys.filter(k=>selected.some(s=>s.key===k)).length;
      cb.checked=keys.length>0 && hit===keys.length;
      cb.indeterminate=hit>0 && hit<keys.length;
    } else {
      cb.checked=selected.some(s=>s.key===cb.dataset.key);
    }
  });
}

/* ---------- 收藏夹：题号后的心形 + 顶部下拉 ---------- */
function isFav(key){ return favorites.some(f=>f.key===key); }
function favFromSelected(s){
  return {
    key:s.key, no:s.no, label:s.label, img:s.img||'',
    whole:!!(s.ref && s.ref.whole),
    pid:s.ref&&s.ref.p ? s.ref.p.id : null,
    qid:s.ref&&s.ref.q ? s.ref.q.qid : null,
    sub:s.ref ? (s.ref.sub||null) : null,
    subLabel:s.ref ? (s.ref.subLabel||null) : null,
    subject:s.ref&&s.ref.p ? s.ref.p.subject : null,
    board:s.ref&&s.ref.p ? s.ref.p.board : null,
    level:s.ref&&s.ref.p ? normLevel(s.ref.p.qualification) : null
  };
}
function toggleFav(s){
  if(isFav(s.key)){
    favorites=favorites.filter(f=>f.key!==s.key); toast('已取消收藏');
  }else{
    favorites.push(favFromSelected(s)); toast('已收藏');
  }
  saveFavorites(); renderSel(); renderFavMenu(); markChange();
}
function favToSelected(f){
  const p=paperMap[f.pid];
  if(!p) return null;
  const q=f.qid&&QUESTIONS[f.pid] ? QUESTIONS[f.pid].find(x=>x.qid===f.qid) : null;
  if(!f.whole && !q) return null;
  return {
    key:f.key, label:f.label||f.no, no:f.no,
    img:f.img||imagePath(f.pid, f.sub||f.qid||''),
    ref:f.whole ? {p,whole:true} : {p,q,sub:f.sub||null,subLabel:f.subLabel||null}
  };
}
function renderFavMenu(){
  const cnt=$('favCount'); if(!cnt) return;
  cnt.textContent=favorites.length+' 题';
  const list=$('favList'); list.innerHTML='';
  if(!favorites.length){ list.innerHTML='<div class="fav-empty">还没有收藏题目<br>在「已选题目」中点击 ♡ 收藏</div>'; return; }
  favorites.forEach(f=>{
    const row=el('div','fav-row');
    const label=el('label','fav-pick');
    const cb=el('input'); cb.type='checkbox'; cb.checked=selected.some(s=>s.key===f.key);
    cb.title='勾选加入/移出已选题目';
    cb.onchange=async()=>{
      if(cb.checked){
        // 收藏可能来自其他考试局/阶段：先切到该组合并加载对应目录文字
        const need=treeKey(f.subject||subject,f.board||board,f.level||level);
        if(loadedTreeKey!==need){
          if(f.subject&&f.board&&f.level){
            subject=f.subject; board=f.board; level=f.level;
            $('subSel').value=subject; setSeg('segBoard',board); setSeg('segLevel',level);
            await loadTreeData(subject,board,level); rebuildWithDifficulty(); updateStatus();
          }else{
            toast('请在下拉菜单选择该收藏题所属科目后重试'); cb.checked=false; return;
          }
        }
        const s=favToSelected(f);
        if(s && !selected.some(x=>x.key===s.key)){
          selected.push(s); renderSel(); updateStatus(); markChange();
        }
      }else{
        selected=selected.filter(s=>s.key!==f.key); renderSel(); updateStatus(); markChange();
      }
      syncChecks(); renderFavMenu();
    };
    const txt=el('span'); txt.textContent='勾选';
    label.append(cb,txt);
    const no=el('span','fav-no'); no.textContent=f.no||f.label||'';
    const rm=el('button','fav-rm'); rm.textContent='取消收藏';
    rm.onclick=()=>{
      favorites=favorites.filter(x=>x.key!==f.key);
      saveFavorites(); renderFavMenu(); renderSel(); markChange(); toast('已取消收藏');
    };
    row.append(no,label,rm); list.appendChild(row);
  });
}

// 按小问分割的题目：用整题替换当前小问（同一大题已选的小问合并为一条整题，避免导出重复）
function replaceWithWhole(s){
  const p=s.ref&&s.ref.p, q=s.ref&&s.ref.q;
  if(!p||!q||!s.ref.sub||!isSplitTagged(q)){ toast('该题已经是整题'); return; }
  const keys=new Set([q.qid].concat((q.sub_ids||[]).filter(Boolean)));
  const at=selected.findIndex(x=>keys.has(x.key));
  const removed=selected.filter(x=>keys.has(x.key)).length;
  selected=selected.filter(x=>!keys.has(x.key));
  const no=makeNo(p,qNo(q),null);
  const whole={key:q.qid,label:no,no,img:imagePath(p.id,q.qid),ref:{p,q,sub:null,subLabel:null}};
  selected.splice(at<0?selected.length:at,0,whole);
  activeIdx=at<0?selected.length-1:at;
  renderSel(); syncChecks(); updateStatus(); markChange();
  previewSelected(whole); highlight(activeIdx);
  toast(removed>1?`已用整题替换该大题的 ${removed} 个小问`:'已用整题替换当前小问');
}

function renderSel(){
  const body=$('selBody'); $('selCount').textContent=selected.length;
  if(!selected.length){ body.innerHTML='<div class="list-empty">还没有选择题目<br>从左侧题库勾选吧</div>'; return; }
  body.innerHTML='';
  selected.forEach((s,i)=>{
    const d=el('div','sel-item'); d.dataset.idx=i;
    const fav=isFav(s.key);
    // 心形旁的「▷整题」只在按小问分割的题目上出现
    const canWhole=!!(s.ref && s.ref.q && s.ref.sub && isSplitTagged(s.ref.q));
    const wholeBtn=canWhole?'<button class="whole-btn" data-act="whole" title="用整题替换当前小问（同一大题的其他小问会一并移除）">▷整题</button>':'';
    // 题号只显示一次：完整题号（精确至小问）单独成行，不再上下并排显示两次
    d.innerHTML=`<span class="idx">${i+1}</span><span class="txt"><span class="qline"><span class="qnum">${s.no}</span><button class="fav-heart${fav?' on':''}" data-act="fav" title="${fav?'取消收藏':'收藏'}">${fav?'♥':'♡'}</button>${wholeBtn}</span></span>
      <span class="mv"><button data-act="up" title="上移">▲</button><button data-act="down" title="下移">▼</button></span><button class="del" data-act="del" title="删除">×</button>`;
    d.onclick=e=>{
      const act=e.target.dataset.act;
      if(act==='fav'){ toggleFav(s); return; }
      if(act==='whole'){ replaceWithWhole(s); return; }
      if(act==='up'&&i>0){ [selected[i-1],selected[i]]=[selected[i],selected[i-1]]; activeIdx=i-1; renderSel(); markChange(); return; }
      if(act==='down'&&i<selected.length-1){ [selected[i+1],selected[i]]=[selected[i],selected[i+1]]; activeIdx=i+1; renderSel(); markChange(); return; }
      if(act==='del'){ removeSelected(s.key); return; }
      previewSelected(s); highlight(i);
    };
    body.appendChild(d);
  });
  if(activeIdx>=0) highlight(Math.min(activeIdx,selected.length-1));
}
function highlight(idx){ activeIdx=idx; document.querySelectorAll('.sel-item').forEach((e,i)=>e.classList.toggle('active',i===idx)); }

/* ---------- 预览：标准 A4 纸，图片适合宽度、整图不跨页 ---------- */
// A4 逻辑像素（96DPI）：794×1123，页边距 12mm≈45px；比例与导出引擎(150DPI)一致
const A4_LOGIC_W = 794, A4_LOGIC_H = 1123, A4_PAD = 45;
const A4_AVAIL_W = A4_LOGIC_W - 2*A4_PAD, A4_AVAIL_H = A4_LOGIC_H - 2*A4_PAD;
const CAP_H = 18;                       // 题号标题占位高度（含 caption 时）
let a4RenderSeq = 0;                    // 防止过期异步结果覆盖当前预览
const imgDimCache = new Map();          // 图片自然尺寸缓存：{w,h} 的 Promise
function sheetHTML(path,caption){       // 发布页仍用卡片式，保留
  return `<div class="sheet">${caption?`<div class="cap">${caption}</div>`:''}<img src="${imgUrl(path)}" alt="题目截图" loading="lazy"></div>`;
}
function loadImgDim(path){
  if(imgDimCache.has(path)) return imgDimCache.get(path);
  const p=new Promise(res=>{
    const im=new Image(); let done=false;
    // 同时保留 Image 对象：整题长图需要用 canvas 找空白切点
    const finish=(w,h)=>{ if(!done){ done=true; res({w:w||0,h:h||0,img:im}); } };
    im.onload=()=>finish(im.naturalWidth, im.naturalHeight);
    im.onerror=()=>finish(0,0);
    im.src=imgUrl(path);
    if(im.complete && im.naturalWidth) finish(im.naturalWidth, im.naturalHeight);
  });
  imgDimCache.set(path,p);
  return p;
}
/* 整题长图：按页切片并把切点尽量落在图片空白行，避免切断小问文字。
 * 返回显示坐标 y（px）的切点数组；检测失败时退回按页高等分。 */
async function detectSliceCuts(item, dim, fullH){
  const cap=item.caption?CAP_H:0;
  const seg=A4_AVAIL_H-cap;
  if(!dim || !dim.w || !dim.h || fullH<=seg) return [];
  const ideal=[];
  for(let y=seg;y<fullH-120;y+=seg) ideal.push(y);
  if(!ideal.length) return [];
  let blanks=[];
  try{
    const im=dim.img;
    if(!im || !im.naturalWidth) throw new Error('no image');
    const cw=360, ch=Math.max(1,Math.round(cw*dim.h/dim.w));
    const cv=document.createElement('canvas'); cv.width=cw; cv.height=ch;
    const cx=cv.getContext('2d',{willReadFrequently:true});
    cx.drawImage(im,0,0,cw,ch);
    const data=cx.getImageData(0,0,cw,ch).data;
    const row=[];
    for(let y=0;y<ch;y++){
      let dark=0, total=0;
      for(let x=0;x<cw;x+=3){
        const i=(y*cw+x)*4;
        const lum=(data[i]*299+data[i+1]*587+data[i+2]*114)/1000;
        if(lum<205) dark++;
        total++;
      }
      row.push(dark/total);
    }
    let start=-1;
    for(let y=0;y<=ch;y++){
      const blank=(y<ch && row[y]<0.02);
      if(blank && start<0) start=y;
      if(!blank && start>=0){
        if(y-start>=5) blanks.push((start+y)/2/ch);  // 归一化空白带中心
        start=-1;
      }
    }
  }catch(e){ /* 图片不可用/跨域：按页高等分 */ }
  if(!blanks.length) return ideal;
  const tol=seg*0.15/fullH;   // 只在理想切点附近寻找空白带
  return ideal.map(y=>{
    const norm=y/fullH;
    let best=norm, dist=tol;
    blanks.forEach(b=>{ const d=Math.abs(b-norm); if(d<dist){ dist=d; best=b; } });
    return Math.round(best*fullH);
  }).filter((y,i,arr)=>y>80 && y<fullH-80 && arr.indexOf(y)===i);
}
/* ---------- A4 分页 ----------
 * 铁律：题目图片恒定适合页面宽度（按可用宽度等比缩放），绝不缩小宽度迁就页高。
 * 不需要一页一道题：按块装页，块放不下就在该块前分页（页底留适当空白）。
 * 每个小问是不可分割块：绝不被切到两页上。整题长图超过一页时，
 * 按 layout 小问边界切段（每段恰含一个完整小问），无 layout 时用空白行检测，最后退回等分。
 */
// items:[{img,caption,regions?}] -> [[{img,caption,capFirst,w,h,sliceY,fullH,whole,part}], ...]
function spansFromCuts(cuts,fullH,seg){
  const uniq=[];
  cuts.slice().sort((a,b)=>a-b).forEach(c=>{
    if(c>60 && c<fullH-60 && (!uniq.length || c-uniq[uniq.length-1]>40)) uniq.push(c);
  });
  const spans=[]; let start=0;
  uniq.forEach(c=>{ if(c-start>=100){ spans.push([start,c]); start=c; } });
  if(fullH-start>=60) spans.push([start,fullH]);
  return spans.length?spans:[[0,fullH]];
}
// 单段超过一页可用高度时等分（极端情况；仍保持完整宽度，不缩小）
function splitOversizeSpans(spans,seg){
  const out=[];
  spans.forEach(([a,b])=>{
    const len=b-a;
    if(len<=seg){ out.push([a,b]); return; }
    const n=Math.ceil(len/seg), step=len/n;
    for(let k=0;k<n;k++) out.push([Math.round(a+k*step),Math.round(a+(k+1)*step)]);
  });
  return out;
}
function a4Paginate(items, dims, cutsList){
  // 1) 展开为块：每块是同一张图的 [y0,y1) 显示像素区间，宽度恒为可用宽度
  const blocks=[];
  items.forEach((it,i)=>{
    const d=dims[i]||{w:0,h:0};
    const cap=it.caption?CAP_H:0;
    const seg=A4_AVAIL_H-cap;
    const fullH=d.w>0&&d.h>0?Math.round(A4_AVAIL_W*d.h/d.w):Math.round(A4_AVAIL_W*0.65);
    let spans;
    if(it.regions && it.regions.length){
      // 裁剪合成（题干+本小问）：段与段之间可分页，段内绝不切
      spans=splitOversizeSpans(it.regions
        .map(([a,b])=>[Math.round(a*fullH),Math.round(b*fullH)])
        .filter(s=>s[1]>s[0]),seg);
    } else if(fullH>seg){
      spans=splitOversizeSpans(spansFromCuts((cutsList&&cutsList[i])||[],fullH,seg),seg);
    } else spans=[[0,fullH]];
    spans.forEach(([y0,y1],si)=>{
      blocks.push({img:it.img,caption:it.caption,capFirst:si===0,
        h:y1-y0,sliceY:y0,fullH,
        whole:!(it.regions&&it.regions.length)&&spans.length===1,
        part:spans.length>1?`${si+1}/${spans.length}`:''});
    });
  });
  // 2) 装页：块不可分割，当前页放不下就在该块前分页（页底留白）
  const pages=[]; let cur=[], used=0;
  blocks.forEach(b=>{
    const need=b.h+(b.capFirst?CAP_H:0);
    if(cur.length && used+need>A4_AVAIL_H){ pages.push(cur); cur=[]; used=0; }
    cur.push({...b,w:A4_AVAIL_W});
    used+=need+2;
  });
  if(cur.length) pages.push(cur);
  if(!pages.length) pages.push([]);
  return pages;
}
function a4PreviewHTML(pages){
  return '<div class="a4-stack">'+pages.map((pg,pi)=>{
    const inner=pg.map(it=>{
      const cap=it.capFirst&&it.caption?`<div class="a4-cap">${it.caption}${it.part?` · ${it.part}`:''}</div>`:'';
      // 切片：外层裁切到本块高度，内层整图按宽度铺满并上移，只露出本块区间
      const img=it.whole
        ? `<img src="${imgUrl(it.img)}" alt="题目截图" style="width:${it.w}px;height:${it.h}px">`
        : `<div class="a4-slice" style="height:${it.h}px"><img src="${imgUrl(it.img)}" alt="题目截图" style="width:${it.w}px;height:${it.fullH}px;margin-top:-${it.sliceY}px"></div>`;
      return `<div class="a4-item">${cap}${img}</div>`;
    }).join('');
    return `<div class="a4-page"><div class="a4-inner">${inner}</div><div class="a4-pgno">第 ${pi+1} / ${pages.length} 页</div></div>`;
  }).join('')+'</div>';
}
async function renderA4Preview(items, label){
  const seq=++a4RenderSeq;
  $('previewLabel').textContent=label;
  $('paperView').innerHTML='<div class="empty-preview">正在按 A4 排版…</div>';
  const dims=await Promise.all(items.map(it=>loadImgDim(it.img)));
  if(seq!==a4RenderSeq || $('previewLabel').textContent!==label) return; // 期间被切换/清空则丢弃
  const cutsList=await Promise.all(items.map(async(it,i)=>{
    if(it.regions && it.regions.length) return [];   // 裁剪合成块不需要切点
    const d=dims[i]||{w:0,h:0};
    const fullH=d.w>0&&d.h>0?Math.round(A4_AVAIL_W*d.h/d.w):0;
    if(fullH<=A4_AVAIL_H-(it.caption?CAP_H:0)) return [];   // 一页内放得下：不切
    // 优先用 layout 小问边界（保证小问不被切到两页）；无 layout 再用空白行检测
    const layout=await loadLayout(it.img);
    let cuts=layoutCutsNorm(layout).map(y=>Math.round(y*fullH));
    if(!cuts.length) cuts=await detectSliceCuts(it,d,fullH);
    return cuts;
  }));
  if(seq!==a4RenderSeq || $('previewLabel').textContent!==label) return;
  current={label, html:a4PreviewHTML(a4Paginate(items,dims,cutsList))};
  showPreview();
}
function applyZoom(){
  const pv=$('paperView');
  const avail=Math.max(240, pv.clientWidth-40);
  const base=Math.min(1.6, avail/A4_LOGIC_W);
  const s=zoom===0?base:base*zoom/100;
  pv.style.setProperty('--a4zoom', s);
  $('zoomVal').textContent=zoom===0?'适应宽度':zoom+'%';
}
function previewPaper(p){
  const qs=QUESTIONS[p.id]||[];
  const items=qs.map(q=>({img:imagePath(p.id,q.qid),caption:makeNo(p,qNo(q),null)}));
  renderA4Preview(items, `${p.year} ${sessionLabel(p.session)} · ${p.paper_code}（整卷 ${qs.length} 题）`);
}
async function previewQuestion(p,q,sid,subLabel){
  const no=makeNo(p,qNo(q),subLabel);
  // Edexcel 按小问打标的题：独立小问图（题干+本小问）；截图缺失时用 layout 裁剪合成，绝不放整题图
  const r=await resolveSubVisual(p,q,sid,subLabel);
  if(!r){
    $('previewLabel').textContent=no;
    $('paperView').innerHTML='<div class="empty-preview">该小问缺少布局数据，无法单独截取<br>（已阻止回退到整题图）</div>';
    return;
  }
  renderA4Preview([{img:r.img,caption:no,regions:r.type==='crop'?r.regions:undefined}], no);
}
function previewSelected(s){
  if(s.ref.whole){ previewPaper(s.ref.p); return; }
  previewQuestion(s.ref.p,s.ref.q,s.ref.sub,s.ref.subLabel);
}
function showPreview(){ $('previewLabel').textContent=current.label; $('paperView').innerHTML=current.html; $('paperView').scrollTop=0; applyZoom(); }
async function overallPreview(){
  if(!selected.length){ toast('请先勾选题目'); return; }
  const {items,problems}=await buildExportItems();
  if(!items.length){ toast(problems.length?'所选小问均无法单独截取':'没有可预览的题目'); return; }
  if(problems.length){
    toast('已跳过无法单独截取的小问：'+problems.slice(0,3).join('、')+(problems.length>3?' 等':''));
  }
  renderA4Preview(items.map((it,i)=>({img:it.img,caption:`${i+1}/${items.length} ${it.no}`,
    regions:it.crop?it.crop.regions:undefined})), `预览卷面 · 共 ${items.length} 题`);
}
// 窗口尺寸变化时重算「适应宽度」缩放
let a4ResizeTimer=null;
window.addEventListener('resize',()=>{ clearTimeout(a4ResizeTimer); a4ResizeTimer=setTimeout(applyZoom,120); });
function updateStatus(){
  if(loading){ $('statusInfo').textContent='加载中…'; return; }
  const modeName = mode==='category'?'按类别':(mode==='keyword'?'按关键词':'按套卷');
  const diffName = diffFilter==='all'?'':` · 难度：${DIFF_NAME[diffFilter]||diffFilter}`;
  $('statusInfo').textContent=`已选 ${selected.length} 题 · ${modeName} · ${subjectLabel(subject)||'—'} · ${({edexcel:'Edexcel',cie:'CIE',aqa:'AQA'})[board]} · ${level==='a-level'?'A-Level':'IGCSE'}${diffName}`;
}

/* ---------- 导出 ---------- */
/* 核查机制：Edexcel「按小问分别打标」的题，用户选择部分小问时，
 * 绝不允许把整道题（父图 qNN.webp）放上。每个所选小问必须对应：
 *   1) 小问独立截图 qNN-<label>.webp；或
 *   2) layout 裁剪合成条目（crop.regions，仅题干+本小问）。
 * 两者都不可得时跳过该小问并记入 problems（导出时明确提示），绝不静默回退整题图。
 * 同时核查数量：Edexcel 分拆题导出的小问条数必须等于所选小问数（防止被按图去重合并）。
 */
const SUBIMG_FILE_RE = /\/q\d+-[a-z0-9-]+\.webp$/i;   // 小问独立截图（qNN-<label>.webp）
async function buildExportItems(){
  const map=new Map(); const problems=[]; let subSelCount=0;
  let stemShownFor='';   // 最近一次已显示大题干的大题标识 pid|qid；已选清单中相邻续接的小问不再重复题干
  for(const s of selected){
    if(s.ref.whole){
      (QUESTIONS[s.ref.p.id]||[]).forEach(q=>{
        const img=imagePath(s.ref.p.id,q.qid);
        if(map.has(img)) return;
        map.set(img,{img,layout:layoutPath(img),no:makeNo(s.ref.p,qNo(q),null),qtype:q.question_type||'',marks:q.total_marks||0});
      });
      stemShownFor='';
    } else {
      const {p,q}=s.ref, sid=s.ref.sub, subLabel=s.ref.subLabel;
      if(p.board==='edexcel' && isSplitTagged(q) && sid){
        subSelCount++;
        // 相邻判定：本小问与上一条产出的大题相同 → 题干已显示，本条只取小问区
        const parentKey=p.id+'|'+q.qid;
        const noStem=stemShownFor===parentKey;
        const r=await resolveSubVisual(p,q,sid,subLabel,{noStem});
        if(!r){ problems.push(s.no); continue; }   // 无法单独截取：跳过并报告
        const cropKey=r.type==='crop'?JSON.stringify(r.regions):'';
        const key=r.img+'#'+cropKey;
        if(!map.has(key)) map.set(key,{img:r.img,layout:layoutPath(r.img),no:s.no,
          qtype:s.ref.q.question_type||'',marks:s.ref.q.total_marks||0,
          board:'edexcel',sub_no:subLabel||null,
          crop:r.type==='crop'?{regions:r.regions}:undefined});
        stemShownFor=parentKey;   // 首条含题干；续接条不含，但大题仍是「题干已显示」状态
      } else {
        // CIE/AQA 等无小问独立截图的考试局：保留原有按父图去重（题干共享，避免重复）
        const img=previewImagePath(p,q,sid,subLabel);
        if(map.has(img)) continue;
        map.set(img,{img,layout:layoutPath(img),no:s.no,qtype:s.ref.q.question_type||'',marks:s.ref.q.total_marks||0});
        stemShownFor=p.id+'|'+q.qid;   // 父图条目本身含题干
      }
    }
  }
  const items=Array.from(map.values());
  // 核查 1：Edexcel 小问条目必须是独立截图或裁剪合成，绝不允许出现无 crop 的父图
  const violations=[];
  items.forEach(it=>{
    if(!it.sub_no || it.crop) return;
    if(SUBIMG_FILE_RE.test(it.img)) return;
    violations.push(it.no);
  });
  // 核查 2：数量一致（排除因缺布局被跳过的）
  const subExpCount=items.filter(it=>it.sub_no).length;
  if(subExpCount!==subSelCount-problems.length){
    violations.push(`小问条数不一致（选 ${subSelCount-problems.length} / 出 ${subExpCount}）`);
  }
  return {items,problems,violations};
}
async function doExport(fmt){
  busy(true, fmt==='pdf'?'正在生成 PDF，请稍候…':'正在生成 Word，请稍候…');
  try{
    const {items,problems,violations}=await buildExportItems();
    // 核查未通过 → 阻断导出（绝不允许整题图冒充小问）
    if(violations.length){
      toast('核查未通过，已阻止导出：'+violations[0]);
      return;
    }
    if(!items.length){ toast('请先勾选题目'); return; }
    if(problems.length){
      toast('已跳过无法单独截取的小问：'+problems.slice(0,3).join('、')+(problems.length>3?' 等':''));
    }
    const r=await fetch(API + '/export',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({format:fmt,items})});
    if(!r.ok){ const j=await r.json().catch(()=>({})); throw new Error(j.error||('HTTP '+r.status)); }
    const blob=await r.blob();
    const cd=r.headers.get('Content-Disposition')||'';
    let name=(cd.match(/filename="([^"]+)"/)||[])[1]||('export.'+(fmt==='pdf'?'pdf':'docx'));
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),3000);
    toast((fmt==='pdf'?'PDF':'Word')+' 已生成并开始下载');
  }catch(e){ toast('生成失败：'+e.message); }
  finally{ busy(false); }
}

/* ---------- 发布页 ---------- */
// 裁剪合成条目的发布页展示：外层按 aspect-ratio 裁切，内层整图上移只露所选区域
async function cropSheetHTML(it){
  const d=await loadImgDim(it.img);
  const W=d.w||1, H=d.h||1;
  return (it.crop.regions||[]).map(([a,b])=>{
    const segH=(b-a)*H;
    if(segH<=0) return '';
    return `<div class="sheet-crop" style="aspect-ratio:${W} / ${segH.toFixed(1)}">`+
      `<img src="${imgUrl(it.img)}" style="margin-top:${(-(a*H/W)*100).toFixed(3)}%" alt="题目截图"></div>`;
  }).join('');
}
async function gotoPublish(){
  if(!selected.length){ toast('请先勾选题目再发布'); return; }
  clearDraft();   // 点击「发布作业」后草稿使命完成，清除本地暂存
  $('pubName').textContent=$('jobName').value.trim()||'（未填写）';
  $('pubClass').textContent=$('classSel').value||'（未选择）';
  $('pubSubject').textContent=subjectLabel($('subSel').value)||'（未选择）';
  $('pubCount').textContent=selected.length;
  const {items,problems}=await buildExportItems();
  const body=$('pubBody'); body.innerHTML='';
  if(problems.length){
    const warn=el('div','pub-warn');
    warn.textContent=`注意：${problems.length} 个所选小问因缺少布局数据无法单独截取，已从发布内容中略去：`+problems.join('、');
    body.appendChild(warn);
  }
  for(const it of items){
    const item=el('div','pub-item');
    let imgs;
    if(it.crop){ imgs=await cropSheetHTML(it); }
    else { imgs=sheetHTML(it.img); }
    item.innerHTML=`<div class="pub-no">题号 ${it.no}</div>${imgs}`;
    body.appendChild(item);
  }
  $('pageSelect').hidden=true; $('pagePublish').hidden=false;
}
function backToSelect(){ $('pagePublish').hidden=true; $('pageSelect').hidden=false; }

/* ---------- 事件 ---------- */
document.querySelectorAll('.seg').forEach(seg=>{
  seg.querySelectorAll('button').forEach(b=>b.onclick=()=>{
    seg.querySelectorAll('button').forEach(x=>x.classList.remove('on')); b.classList.add('on');
    if(seg.id==='segMode'){
      mode=b.dataset.v;
      kwStop();
      $('treeHeadTitle').textContent=mode==='category'?'题目分类':(mode==='keyword'?'关键词检索':'题库目录');
      $('treeMode').textContent=mode==='category'?'按类别':(mode==='keyword'?'按关键词':'按套卷');
      $('kwSearch').hidden=(mode!=='keyword');
      selected=[]; activeIdx=-1; current=null; renderSel();
      if(mode==='keyword'){
        $('previewLabel').textContent='—';
        $('paperView').innerHTML='<div class="empty-preview">搜索题目后<br>点击题号即可预览截图</div>';
        showKeywordBlank();
      } else rebuildWithDifficulty();
      markChange();
    } else {
      if(seg.id==='segBoard') board=b.dataset.v;
      else level=b.dataset.v;
      kwStop();
      selected=[]; activeIdx=-1; renderSel(); updateStatus();
      // 换考试局/阶段 → 按新组合加载二/三/四级文字（不含难度）
      const done=()=>{ renderSel(); if(mode==='keyword') showKeywordBlank(); else rebuildWithDifficulty(); updateStatus(); markChange(); };
      if(subject) loadTreeData(subject,board,level).then(done);
      else { if(mode==='keyword') showKeywordBlank(); else rebuildWithDifficulty(); markChange(); }
    }
    updateStatus();
  });
});
$('subSel').onchange=async e=>{
  kwStop();
  subject=e.target.value; selected=[]; activeIdx=-1; current=null;
  $('previewLabel').textContent='—';
  $('paperView').innerHTML='<div class="empty-preview">点击左侧或中间的任意题目<br>即可在此预览截图</div>';
  if(!subject){ loadedTreeKey=''; QUESTIONS={}; SUBTAGS={}; DIFF={}; TEXT={}; SUBMETA={}; PAPERS=[]; paperMap={};
    diffScopes.clear(); diffScopeLoading.clear(); allDiffLoadedFor=''; allDiffLoading=null;
    imgQueue.length=0;
    showSubjectHint(); renderSel(); updateStatus(); markChange(); return; }
  // 默认组合 edexcel + a-level 优先；只有该科目在此组合无数据时才回退到它自己的默认组合
  if(!comboHasData(subject,board,level)){
    const fallbackCombo=(pendingDraftSel && pendingDraftSel.subject===subject && pendingDraftSel.board)
      ? {board:pendingDraftSel.board,level:pendingDraftSel.level} : firstComboForSubject(subject);
    if(fallbackCombo){
      board=fallbackCombo.board; level=fallbackCombo.level;
      setSeg('segBoard',board); setSeg('segLevel',level);
    }
  }
  await loadTreeData(subject,board,level);
  restoreDraftSelFor(subject);
  renderSel(); if(mode==='keyword') showKeywordBlank(); else rebuildWithDifficulty(); updateStatus(); markChange();
};
$('jobName').oninput=saveDraft;
$('classSel').onchange=saveDraft;
// 关键词模式：点「搜索」或按回车触发流式检索；不再对顶部输入框做实时筛选
$('kwSearchBtn').onclick=()=>runKeywordSearch();
$('kwInput').onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); runKeywordSearch(); } };
$('btnFav').onclick=e=>{
  e.stopPropagation();
  const m=$('favMenu'); m.hidden=!m.hidden;
  $('btnFav').classList.toggle('on',!m.hidden);
  if(!m.hidden) renderFavMenu();
};
$('favMenu').onclick=e=>e.stopPropagation();
document.addEventListener('click',()=>{
  $('favMenu').hidden=true; $('btnFav').classList.remove('on');
});
$('favClear').onclick=()=>{
  if(!favorites.length){ toast('收藏夹已是空的'); return; }
  favorites=[]; saveFavorites(); renderFavMenu(); renderSel(); markChange(); toast('已清空收藏');
};
// 难度变为非「全选」时，任何模式下都先把科目级难度包补齐，再重筛当前左栏
function rebuildWithDifficulty(){
  if(mode==='keyword'){
    // 关键词结果已在本地缓存：直接重渲染，不重新请求题库
    if(diffFilter!=='all') ensureAllDifficulty().then(()=>renderKeywordHits(kwDone));
    else renderKeywordHits(kwDone);
    return;
  }
  if(diffFilter!=='all') ensureAllDifficulty().then(buildTree); else buildTree();
}
$('diffSel').onchange=e=>{
  diffFilter=e.target.value; selected=[]; activeIdx=-1; current=null; renderSel(); updateStatus(); markChange();
  $('previewLabel').textContent='—';
  $('paperView').innerHTML='<div class="empty-preview">点击左侧题号<br>即可在此预览截图</div>';
  rebuildWithDifficulty();
};
$('btnClear').onclick=()=>{ selected=[]; activeIdx=-1; renderSel(); syncChecks(); updateStatus(); markChange(); };
$('btnCollapseAll').onclick=()=>{
  document.querySelectorAll('#tree .children').forEach(c=>{
    c.classList.add('hidden');
    const row=c.previousElementSibling, tw=row&&row.querySelector('.twist');
    if(tw){ tw.textContent='▸'; row.classList.add('collapsed'); }
  });
};
$('zoomOut').onclick=()=>{ zoom=zoom<=0?90:Math.max(40,zoom-10); applyZoom(); };
$('zoomIn').onclick=()=>{ zoom=zoom<=0?110:Math.min(200,zoom+10); applyZoom(); };
$('actOverall').onclick=overallPreview;
$('actWord').onclick=()=>doExport('docx');
$('actPdf').onclick=()=>doExport('pdf');
$('actAnswer').onclick=()=>toast('「输出答案」功能待接入（题库快照暂无答案数据）');
$('actPublish').onclick=gotoPublish;
$('btnUndo').onclick=()=>histGo(-1);
$('btnRedo').onclick=()=>histGo(1);
$('pubBack').onclick=backToSelect;
$('pubBack2').onclick=backToSelect;
$('pubConfirm').onclick=()=>toast('已生成作业预览（正式发布接口待接入）');

favorites=loadFavorites();
renderFavMenu();
loadCatalog();
