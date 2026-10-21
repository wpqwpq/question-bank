import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  AuthUser,
  Paper,
  QuestionDetail,
  QuestionSummary,
  Status,
  SubDraft,
  VerifyState,
} from './api'
import { api, authApi, canEditTaxonomy, DIFFICULTY_LABELS, primaryOk, resolvePrimary } from './api'
import type { SaveBody, SaveResult } from './api'
import Sidebar from './components/Sidebar'
import TagAdmin from './components/TagAdmin'
import AuditLog from './components/AuditLog'
import Overview from './components/Overview'
import TagPanel from './components/TagPanel'
import QuestionViewer from './components/QuestionViewer'
import PreviewPanel from './components/PreviewPanel'
import ResizeHandle from './components/ResizeHandle'
import LoginPage, { ChangePasswordPage } from './components/LoginPage'

/**
 * 「按小问分别打标」三个考试局都支持，语义不同但入口一致：
 *
 *   - **AQA / CIE / accounting 学科**：小问之间的数据是传递的，只打标、**不切图**
 *     （后端 `store.sub_images_enabled`，子题文件不带 image/layout，预览回退整题图）；
 *   - **Edexcel physics / chemistry**：小问彼此独立，每个小问要连题区一起切出来单独成题
 *     （「题干 + a」「题干 + b」），所以保存时会真的裁剪子图。
 *
 * 云端因此装了 Pillow（裁剪只用它，不需要 pymupdf）。早期一版曾把开关整体隐藏，
 * 只放行已经切好的题，那是理解错了需求，已修正。
 */

type Toast = { kind: 'ok' | 'err'; text: string } | null
type Mode = 'annotate' | 'preview'

// ── 右侧面板宽度（可拖动，持久化）──
const RIGHT_DEFAULT = 320
const RIGHT_MIN = 260
const RIGHT_KEY = 'wb.rightW'
/** 左侧卷列表固定 240px，中间题图区至少留 360px */
const RIGHT_MAX = 720
const SIDEBAR_W = 240
const MAIN_MIN = 360

function maxRightOf(vp: number) {
  return Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, vp - SIDEBAR_W - MAIN_MIN))
}
const clampRight = (w: number, vp = window.innerWidth) =>
  Math.min(maxRightOf(vp), Math.max(RIGHT_MIN, Math.round(w)))

function loadRightWidth() {
  const v = Number(localStorage.getItem(RIGHT_KEY))
  return clampRight(Number.isFinite(v) && v > 0 ? v : RIGHT_DEFAULT)
}

/**
 * 资格名归一化后比较。
 * 历史遗留：Edexcel/AQA 的数据目录与 front-matter 用 `a-level`，CIE 用 `alevel`，
 * 而「资格」下拉的选项值统一是 `a-level` —— 直接全等比较会让 CIE 卷全部被过滤掉。
 * 去连字符后 `a-level` 与 `alevel` 等价，igcse/gcse 不受影响。
 */
const normQual = (q: string) => q.replace(/-/g, '').toLowerCase()

// 难度不再是「完成」的必要条件（2026-10-16，与后端 compute_status 一致）：
// 有知识点标签即完成；只有难度没标签算「缺其一」。难度是否齐备在预览模式单独补。
function subState(s: SubDraft): 'done' | 'partial' | 'none' {
  if (s.tags.length > 0) return 'done'
  if (s.difficulty !== null) return 'partial'
  return 'none'
}

/**
 * 主标签降级规则（plans/主标签实施方案 §4.1）——只由「勾掉某个标签」触发：
 *   剩 0 个 → 置空；剩 1 个 → 自动接任；≥2 个且原主被删 → 置空等用户重选。
 * 从 1 个加到 2 个时原主（即那唯一一个标签）自然留任，不必重新点。
 */
function nextPrimary(tags: string[], primary: string | null): string | null {
  if (tags.length === 0) return null
  if (tags.length === 1) return tags[0]
  return primary && tags.includes(primary) ? primary : null
}

/** 勾选/取消一个标签 + 主标签随之降级（纯函数，供切分题各小问复用） */
function withToggledTag(s: SubDraft, id: string): SubDraft {
  const tags = s.tags.includes(id) ? s.tags.filter((t) => t !== id) : [...s.tags, id]
  return { ...s, tags, primaryTag: nextPrimary(tags, s.primaryTag) }
}

export default function App() {
  // ── 身份（云端版）──
  const [me, setMe] = useState<AuthUser | null>(null)
  /** 首次挂载时还不知道登没登录：先显示一屏空白，避免闪一下登录页再跳回来 */
  const [booting, setBooting] = useState(true)
  /** 主动改密（非强制）时的整页切换 */
  const [changingPw, setChangingPw] = useState(false)

  /**
   * 只读账号（viewer）：能翻能看，改不了任何东西。
   * 后端每个写接口都会返回 403 兜底，这里只是别让人白点一遍才发现按钮不能按。
   * role 为 null（还没登录）时按「可写」处理——此时页面被登录页挡住，不会渲染到这里。
   */
  const canWrite = me?.role !== 'viewer'

  const [papers, setPapers] = useState<Paper[]>([])
  const [board, setBoard] = useState('edexcel')
  const [qualification, setQualification] = useState('a-level')
  const [subject, setSubject] = useState('physics')
  const [paperId, setPaperId] = useState('')
  const [questions, setQuestions] = useState<QuestionSummary[]>([])
  /**
   * 最新一次渲染的题目列表（只在异步回调里读）。
   * 保存是串行队列，回调闭包里的 questions 可能落后一轮，
   * 直接用它算「这题原来是不是已打标」会把卷进度重复加减。
   */
  const questionsRef = useRef<QuestionSummary[]>([])
  questionsRef.current = questions
  const [currentIndex, setCurrentIndex] = useState(0)
  const [detail, setDetail] = useState<QuestionDetail | null>(null)
  const [draftTags, setDraftTags] = useState<string[]>([])
  const [draftDifficulty, setDraftDifficulty] = useState<string | null>(null)
  /** 整题模式的主标签草稿（切分题逐问存在 draftSubs[].primaryTag 上） */
  const [draftPrimary, setDraftPrimary] = useState<string | null>(null)
  /**
   * 在飞的保存请求数（>0 = 按钮显示「保存中…」并禁用）。
   * 用计数而不是布尔：保存走串行队列，连点时会排着好几个，布尔会在第一个结束时提前解禁。
   */
  const [savingCount, setSavingCount] = useState(0)
  const saving = savingCount > 0
  /** 保存队列：同一时刻只有一个 PUT 在飞，后到的排队。顺序 = 点击顺序。 */
  const saveChainRef = useRef<Promise<void>>(Promise.resolve())
  /**
   * 「库里的」小问标签快照。用来判断某个小问是不是被用户**勾空**了
   * （勾空要显式提交 tags: []，不提交等于"别动"）。
   * ⚠️ 不能直接读 detail.subs：detail 只在切题时重拉，保存后不刷新，
   *    否则「刚打上标 → 保存 → 又勾空」这条路上会漏判成"从没打过标"而不提交。
   */
  const savedSubTagsRef = useRef<Map<string, string[]>>(new Map())
  const [toast, setToast] = useState<Toast>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  // ── 页面：打标台 / 标签管理 / 操作日志（右上入口跳转，整页切换）──
  const [view, setView] = useState<'workbench' | 'tagadmin' | 'audit' | 'overview'>('workbench')
  /** 从标签管理页回来时 +1，触发当前题详情重拉（词表可能刚被改过） */
  const [refreshTick, setRefreshTick] = useState(0)

  // ── 模式 ──
  const [mode, setMode] = useState<Mode>('annotate')
  const [previewIndex, setPreviewIndex] = useState(0)
  const [previewDetail, setPreviewDetail] = useState<QuestionDetail | null>(null)
  const [previewSubIdx, setPreviewSubIdx] = useState(0)
  const [confirming, setConfirming] = useState(false) // 审核确认请求进行中
  // 预览里就地补/改选的难度（未落库；勾选确认时随请求一起写回，docs/13）
  const [previewPick, setPreviewPick] = useState<string | null>(null)

// ── 小问切分状态 ──
const splitOnRef = useRef(false) // 开关意图（跨题保持）；供题目加载effect读取
/** 预览→打标 切换时，等 detail 加载完要落到的小问序号（见 switchMode） */
const pendingFocusRef = useRef<number | null>(null)
  const [splitMode, setSplitMode] = useState(false) // 本题最终生效
  const [draftSubs, setDraftSubs] = useState<SubDraft[]>([])
  const [focusIdx, setFocusIdx] = useState(0)

  // ── 右侧面板宽度 ──
  const [rightW, setRightW] = useState(loadRightWidth)
  const [vpW, setVpW] = useState(() => window.innerWidth)

  useEffect(() => {
    try {
      localStorage.setItem(RIGHT_KEY, String(rightW))
    } catch {
      /* 隐私模式下忽略 */
    }
  }, [rightW])

  // 窗口变化后重新收敛（避免右侧被撑到把中间挤没）
  useEffect(() => {
    const onResize = () => {
      setVpW(window.innerWidth)
      setRightW((w) => clampRight(w))
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const maxRight = maxRightOf(vpW)

  const visiblePapers = useMemo(
    () =>
      papers.filter(
        (p) =>
          p.board === board &&
          normQual(p.qualification) === normQual(qualification) &&
          p.subject === subject,
      ),
    [papers, board, qualification, subject],
  )

  // 预览列表（docs/13 + plans/主标签实施方案 §2）：
  //   · 齐备的题（status=done）；
  //   · **任意一个小问打了标**的题（2026-10-18）——小问允许跳过保存，
  //     只打了一问也要能在预览里看到，否则「跳过的题」等于被藏起来了。
  //     这类题在预览里显示为「待补齐」灰色（缺难度/缺主标签），可回打标模式补完。
  //   · 标了「此题有误」的题**一律不出现**（2026-10-18 培琪要求）：它已退出核对待办，
  //     哪怕之前打过标、确认过，也先解决「有误」再说。
  //   · 「只有难度、没有任何知识点标签」的题仍然不进列表（any_tagged=false）。
  const previewQuestions = useMemo(
    () => questions.filter((q) => !q.flagged && (q.status === 'done' || q.any_tagged === true)),
    [questions],
  )

  // ── 身份探测（只跑一次）──
  useEffect(() => {
    let cancelled = false
    authApi
      .me()
      .then((u) => {
        if (cancelled) return
        setMe(u)
        // 默认学科落到本人学科上：否则化学生物老师一进来看到的是空的物理列表
        if (!u.subjects.includes('*') && u.subjects.length > 0 && !u.subjects.includes(subject)) {
          setSubject(u.subjects[0])
        }
      })
      .catch(() => {
        /* 401 → me 保持 null，下面渲染登录页 */
      })
      .finally(() => {
        if (!cancelled) setBooting(false)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 会话过期（任意接口返回 401）→ 统一退回登录页，并清掉上一段会话的数据
  useEffect(() => {
    const onUnauth = () => {
      setMe(null)
      setPapers([])
      setPaperId('')
      setQuestions([])
      setDetail(null)
      setPreviewDetail(null)
    }
    window.addEventListener('qb-unauthorized', onUnauth)
    return () => window.removeEventListener('qb-unauthorized', onUnauth)
  }, [])

  // 初始加载卷列表。
  // ⚠️ 依赖必须带 must_change_password —— 只写 me?.username 会踩这个坑：
  //    登录后 me 有值但还停在「强制改密」页，这个 effect 立刻发 /api/papers 拿到 428，
  //    弹一句「加载卷列表失败：首次登录，请先修改初始密码」；改密成功后用户名没变，
  //    effect 不重跑 → 打标台里卷列表一直是空的，只能刷页面才好。
  // 现在两处一起管：强制改密期间根本不发请求；改完 must_change_password 变化会自动重跑。
  useEffect(() => {
    if (!me || me.must_change_password) return
    api
      .papers()
      .then(setPapers)
      .catch((e: Error) => setToast({ kind: 'err', text: `加载卷列表失败：${e.message}` }))
  }, [me?.username, me?.must_change_password]) // eslint-disable-line react-hooks/exhaustive-deps

  const showToast = useCallback((t: Toast) => {
    setToast(t)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 3000)
  }, [])

  // 当前卷必须属于左栏当前三维，否则关闭（清 paperId，由下方切卷 effect 级联清空题目/详情/预览状态）。
  // ⚠️ 不能写成「三维一变就无条件 setPaperId('')」：概览页「打开这卷」（下方 onOpen）会在同一批
  //    setState 里先改三维再设 paperId，React 18 合成一次提交后本 effect 立刻把刚设好的 paperId 清掉
  //    → 左栏下拉落回「— 选择试卷 —」，题号格子一起消失（2026-10-21 修）。
  //    改成按「这卷是否属于新维度」判断后，两条路径都自然正确：
  //      · 概览跳转：三维已被设成该卷的维度 → 属于 → 保留；
  //      · 手动切维度：旧卷不属于新维度 → 清空（原行为）。
  // 依赖刻意不含 papers：卷的考试局/资格/学科不会变，加进来只会在保存/核对刷新列表时白跑。
  useEffect(() => {
    if (!paperId) return
    const cur = papers.find((p) => p.id === paperId)
    const sameDim =
      cur !== undefined &&
      cur.board === board &&
      normQual(cur.qualification) === normQual(qualification) &&
      cur.subject === subject
    if (!sameDim) setPaperId('')
  }, [paperId, board, qualification, subject]) // eslint-disable-line react-hooks/exhaustive-deps

  // 当前卷的「已核对题数」以 questions 为准。
  // 确认、取消确认、存盘、预览改难度都会更新 questions[].verify_state，但 papers[].
  // verified_count 是**接口那一次**的快照。不跟着改，侧栏下拉里的 ✓ 就会停在旧值：
  // 典型场景是老师在一张已整卷核完的卷上改了某题标签 → 后端已撤销该题确认，
  // 但本地 verified_count 没变 → 钩还亮着，等于骗人。
  useEffect(() => {
    if (!paperId || questions.length === 0) return
    const n = questions.reduce((acc, q) => acc + (q.verify_state === 'all' ? 1 : 0), 0)
    setPapers((prev) => {
      const hit = prev.find((p) => p.id === paperId)
      if (!hit || (hit.verified_count ?? -1) === n) return prev // 没变就别造新数组
      return prev.map((p) => (p.id === paperId ? { ...p, verified_count: n } : p))
    })
  }, [paperId, questions])

  // 切卷 → 拉题目列表，回到第 1 题
  useEffect(() => {
    if (!paperId) {
      setQuestions([])
      setDetail(null)
      setCurrentIndex(0)
      setPreviewIndex(0)
      setPreviewDetail(null)
      return
    }
    setQuestions([])
    setDetail(null)
    setCurrentIndex(0)
    setPreviewIndex(0)
    setPreviewDetail(null)
    // 必须能取消：本卷在请求飞行途中被切走（切维度、退出登录）时，
    // 晚到的响应会把「刚清空的题目」又填回来 —— 左栏写着「— 选择试卷 —」，
    // 中间区却显示着上一张卷的第 1 题。
    let cancelled = false
    api
      .questions(paperId)
      .then((qs) => {
        if (!cancelled) setQuestions(qs)
      })
      .catch((e: Error) => {
        if (!cancelled) showToast({ kind: 'err', text: `加载题目失败：${e.message}` })
      })
    return () => {
      cancelled = true
    }
  }, [paperId, showToast])

  // 拉当前题详情，重置草稿（切分状态按 开关意图 × 本题资格 推导）
  // 依赖刻意只取「当前题的 qid」而不是整个 questions 数组：保存（commit）会更新
  // questions 里的 status，若依赖数组就会重拉详情、把草稿与焦点重置，打断正在进行的打标。
  const currentQid = questions[currentIndex]?.qid
  useEffect(() => {
    if (!currentQid) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .question(currentQid)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        applyDetail(d)
      })
      .catch((e: Error) => showToast({ kind: 'err', text: `加载题目失败：${e.message}` }))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentQid, showToast, refreshTick])

  // 预览模式：拉当前预览题详情（只读）
  // 依赖刻意只用「当前题的 qid」而不是整个 previewQuestions 数组：
  // 确认操作会更新 questions 里的 verify_state（数组引用变化），若依赖数组会导致
  // 每次确认都重新拉详情并把 previewSubIdx 重置回 0，冲掉「自动切下一个未确认小问」。
  const previewQid = mode === 'preview' ? previewQuestions[previewIndex]?.qid : undefined
  useEffect(() => {
    if (mode !== 'preview') return
    if (!previewQid) {
      setPreviewDetail(null)
      return
    }
    let cancelled = false
    api
      .question(previewQid)
      .then((d) => {
        if (!cancelled) {
          setPreviewDetail(d)
          setPreviewSubIdx(0) // 切题回第一问
          setPreviewPick(null) // 补难度只对当前题有效，切题即丢（docs/13 定：不勾选不落库）
        }
      })
      .catch((e: Error) => showToast({ kind: 'err', text: `加载题目失败：${e.message}` }))
    return () => {
      cancelled = true
    }
  }, [mode, previewQid, showToast])

  // 切换模式时保持「当前题目 + 当前小问」不变。
  // 两种模式的题号列表不同（预览只含有标签的题），所以用 qid 对齐索引；
  // 当前题不在预览列表里（尚未打标）时，预览回落到第 1 题。
  const switchMode = useCallback(
    (m: Mode) => {
      if (m === mode) return
      if (m === 'preview') {
        const qid = questions[currentIndex]?.qid
        const i = previewQuestions.findIndex((q) => q.qid === qid)
        setPreviewIndex(i >= 0 ? i : 0)
        setPreviewSubIdx(i >= 0 ? focusIdx : 0)
        setPreviewPick(null)
      } else {
        const qid = previewQuestions[previewIndex]?.qid
        const i = questions.findIndex((q) => q.qid === qid)
        if (i >= 0 && i !== currentIndex) {
          pendingFocusRef.current = previewSubIdx // 等 detail 加载完再落焦点
          setCurrentIndex(i)
        } else if (i >= 0) {
          setFocusIdx(previewSubIdx)
        }
      }
      setMode(m)
    },
    [mode, questions, currentIndex, previewQuestions, previewIndex, focusIdx, previewSubIdx],
  )

  function applyDetail(d: QuestionDetail) {
    const mode = d.split_active || (splitOnRef.current && d.split_eligible)
    setSplitMode(mode)
    if (mode) {
      // 小问列表一律以 cut.json 的 top_labels 为准（完整）；subs 只用来回填已有标签。
      // ⚠️ 不能拿 subs 的 label 当列表：增量保存允许「个别小问留空不提交」，
      //    那样未打标的那个小问会在这里被漏掉，界面上只剩已提交的几个小问
      //    （过去只有「开关切两下」走 toggleSplit 才能恢复，因为它用的是 top_labels）。
      const labels =
        d.top_labels.length > 0
          ? d.top_labels
          : d.split_active && d.subs.length > 0
            ? d.subs.map((s) => s.label)
            : []
      setDraftSubs(
        labels.map((l) => {
          // ⚠️ 两侧类型不一定一致（top_labels 来自 cut.json 是 str，子题文件的
          //    sub_label 经 YAML 读回可能是 int），必须按字符串比较，否则标签回填不出来。
          //    后端 api.get_question 已统一输出 str，这里再兜一层防手改/旧数据。
          const ex = d.subs.find((s) => String(s.label) === String(l))
          const tags = ex?.tags ?? []
          return {
            label: l,
            tags,
            difficulty: ex?.difficulty ?? null,
            // 单标签的单元后端已显式落盘；这里再走一次读侧回退，防手改残留
            primaryTag: resolvePrimary(tags, ex?.primary_tag),
          }
        }),
      )
      savedSubTagsRef.current = new Map(
        d.subs.map((s) => [String(s.label), [...(s.tags ?? [])]]),
      )
      // 从预览模式切回来时保留原来的小问选中（switchMode 存的）
      const pf = pendingFocusRef.current
      pendingFocusRef.current = null
      setFocusIdx(pf != null ? Math.min(pf, Math.max(0, labels.length - 1)) : 0)
      setDraftTags([])
      setDraftDifficulty(null)
      setDraftPrimary(null)
    } else {
      setDraftSubs([])
      setDraftTags(d.tags)
      setDraftDifficulty(d.difficulty)
      setDraftPrimary(resolvePrimary(d.tags, d.primary_tag))
    }
  }

  const toggleSplit = useCallback(
    (on: boolean) => {
      splitOnRef.current = on
      if (!detail) return
      if (on && detail.split_eligible) {
        setSplitMode(true)
        setDraftSubs(
          detail.top_labels.map((l) => ({ label: l, tags: [], difficulty: null, primaryTag: null })),
        )
        setFocusIdx(0)
        setDraftTags([])
        setDraftDifficulty(null)
        setDraftPrimary(null)
      } else {
        setSplitMode(false)
        setDraftSubs([])
        setDraftTags(detail.tags)
        setDraftDifficulty(detail.difficulty)
        setDraftPrimary(resolvePrimary(detail.tags, detail.primary_tag))
      }
    },
    [detail],
  )

  // 切题：未保存的修改直接丢弃，不弹确认
  const guardedJump = useCallback((target: number) => {
    setCurrentIndex(target)
  }, [])

  const gotoPrev = useCallback(() => {
    if (currentIndex > 0) guardedJump(currentIndex - 1)
  }, [currentIndex, guardedJump])

  const gotoNext = useCallback(() => {
    if (currentIndex < questions.length - 1) guardedJump(currentIndex + 1)
  }, [currentIndex, questions.length, guardedJump])

  /**
   * 「此题有误」开关。
   *   标记：写审核意见 → 丢弃本题所有草稿（勾选的标签、选的难度都不再保留）→ 跳下一题；
   *   取消：直接解冻，不动草稿。
   * 标记只落 data/_review/flagged.json，不碰 qNN.md；后端还会拒绝该题的一切写入（409），
   * 所以就算有人绕过界面直调接口也改不动。
   */
  const toggleFlag = useCallback(async () => {
    const d = mode === 'preview' ? previewDetail : detail
    if (!d || !canWrite) return
    const on = !d.flagged
    try {
      await api.setFlag(d.qid, on)
    } catch (e) {
      showToast({ kind: 'err', text: `标记失败：${(e as Error).message}` })
      return
    }
    const mark = <T extends { qid: string; flagged?: boolean }>(x: T | null) =>
      x && x.qid === d.qid ? { ...x, flagged: on } : x
    setQuestions((prev) => prev.map((q) => (q.qid === d.qid ? { ...q, flagged: on } : q)))
    setDetail((prev) => mark(prev))
    setPreviewDetail((prev) => mark(prev))

    // 卷级计数同步：标了「有误」的题退出核对待办（既不算待确认也不算已确认），
    // 所以 flagged_count +1 的同时，若它原本已确认，verified_count 要 -1。
    // 反之取消标记时两者回滚。这直接决定「整卷核对完成」的对钩何时亮起。
    const wasAll = (d.verify_state ?? 'none') === 'all'
    setPapers((prev) =>
      prev.map((p) =>
        p.id === paperId
          ? {
              ...p,
              flagged_count: Math.max(0, (p.flagged_count ?? 0) + (on ? 1 : -1)),
              verified_count: Math.max(0, (p.verified_count ?? 0) + (wasAll ? (on ? -1 : 1) : 0)),
            }
          : p,
      ),
    )

    if (on) {
      // 已勾的标签、已选难度全部抛弃：直接按库里现状重置草稿
      applyDetail(d)
      setPreviewPick(null)
      showToast({ kind: 'ok', text: '已标记为「有误」，本题修改已停用' })
      if (mode === 'preview') {
        // 该题会从预览列表里消失：原索引 i 位置上就是下一题，
        // 只有 i 已经是最后一个时才需要往回退一格（列表长度已减 1）。
        setPreviewIndex((i) => Math.min(i, Math.max(0, previewQuestions.length - 2)))
      } else {
        gotoNext()
      }
    } else {
      showToast({ kind: 'ok', text: '已取消「有误」标记，可以继续修改' })
    }
  }, [mode, detail, previewDetail, previewQuestions.length, applyDetail, gotoNext, showToast, paperId, canWrite])

  // 焦点组（split 模式下 1/2/3 与标签面板作用于它）
  const focusSub = splitMode ? draftSubs[focusIdx] : undefined
  const currentTags = splitMode ? (focusSub?.tags ?? []) : draftTags
  const currentDifficulty = splitMode ? (focusSub?.difficulty ?? null) : draftDifficulty
  const currentPrimary = splitMode ? (focusSub?.primaryTag ?? null) : draftPrimary

  const toggleTag = useCallback(
    (id: string) => {
      if (splitMode) {
        setDraftSubs((prev) =>
          prev.map((s, i) => (i === focusIdx ? withToggledTag(s, id) : s)),
        )
      } else {
        const tags = draftTags.includes(id)
          ? draftTags.filter((t) => t !== id)
          : [...draftTags, id]
        setDraftTags(tags)
        setDraftPrimary((p) => nextPrimary(tags, p))
      }
    },
    [splitMode, focusIdx, draftTags],
  )

  /** 设主标签：只接受已选中的标签（≥2 个标签时 chip 右侧才出现入口） */
  const setPrimaryTag = useCallback(
    (id: string) => {
      if (splitMode) {
        setDraftSubs((prev) =>
          prev.map((s, i) => (i === focusIdx && s.tags.includes(id) ? { ...s, primaryTag: id } : s)),
        )
      } else if (draftTags.includes(id)) {
        setDraftPrimary(id)
      }
    },
    [splitMode, focusIdx, draftTags],
  )

  const pickDifficulty = useCallback(
    (d: string) => {
      if (splitMode) {
        setDraftSubs((prev) => prev.map((s, i) => (i === focusIdx ? { ...s, difficulty: d } : s)))
      } else {
        setDraftDifficulty(d)
      }
    },
    [splitMode, focusIdx],
  )

  // 切分题：**允许只标一部分小问**——未打标的小问不提交，后端保留其原有子题文件
  // （只有「本题从没有过该小问文件」时才补一个空壳，status 停在 partial、
  //  空壳没标签确认不了 → 整卷 ✓ 不会假亮）。已打标签的小问仍必须满足主标签规则；
  // 难度一律可留空（docs/13）。
  const filledSubs = draftSubs.filter((s) => s.tags.length > 0)
  const subsSaveable =
    filledSubs.length > 0 && filledSubs.every((s) => primaryOk(s.tags, s.primaryTag))
  const subDoneCount = draftSubs.filter((s) => subState(s) === 'done').length
  // ≥2 个标签必须有主标签（plans/主标签实施方案 §4.1）——整题与切分题各自校验
  const primaryAllOk = splitMode ? subsSaveable : primaryOk(draftTags, draftPrimary)
  const canSave =
    !saving &&
    !!detail &&
    (splitMode ? subsSaveable : draftTags.length > 0 && primaryAllOk)
  /** 「保存，下一问」只放行「焦点小问没问题」的情况，避免落到后端 422 */
  const focusSubOk =
    !splitMode || !focusSub || focusSub.tags.length === 0 || primaryOk(focusSub.tags, focusSub.primaryTag)
  /** 底部提示用：确实存在「≥2 标签却没指定主标签」的单元（只看已打标的小问） */
  const missingPrimary = splitMode
    ? filledSubs.some((s) => s.tags.length >= 2 && !primaryOk(s.tags, s.primaryTag))
    : draftTags.length >= 2 && !primaryOk(draftTags, draftPrimary)

  // split 模式：焦点在最后一问时按钮才是「保存」，否则是「下一问」导航
  const onLastSub = !splitMode || focusIdx >= draftSubs.length - 1
  /** 「保存，下一问」是否可点：焦点小问要么还没打标、要么主标签齐备 */
  const canAdvance = splitMode && !onLastSub && !saving && focusSubOk
  const saveEnabled = canWrite && (splitMode && !onLastSub ? canAdvance : canSave)

  /**
   * 本次要提交的小问：**已打标签的** + **被主动勾空的**（显式 `tags: []`）。
   *
   * 未打标的小问**不提交**——后端 `_save_split` 只给「提交了但没有子文件」的小问补空壳，
   * 已有文件的一律原样保留。这样即使请求重放/乱序，也不会把别的小问抹回空壳。
   * 勾空必须显式提交，否则后端不知道你要删（「没提交」现在等于「别动」）。
   *
   * 多标签却没定主的本小问静默跳过（提交会 422），等用户点右侧圆点定好主标签再随下一次落库。
   */
  const subsToSubmit = useCallback((): SubDraft[] => {
    const savedTags = savedSubTagsRef.current
    return draftSubs.filter((s) =>
      s.tags.length > 0
        ? primaryOk(s.tags, s.primaryTag)
        : (savedTags.get(String(s.label))?.length ?? 0) > 0,
    )
  }, [draftSubs])

  /**
   * 落库——**唯一的写入口**，整题与按小问两种形态都在这里，语义只有一份。
   *   · **串行**：同一时刻只有一个 PUT 在飞，后到的排队，顺序 = 点击顺序。
   *     后端按「提交内容」整体落盘，乱序的旧快照会盖掉新快照，串行是正确性的前提。
   *   · 返回 true/false，由调用方决定要不要前进焦点/跳题——**失败绝不假装成功**。
   */
  const commit = useCallback(
    (subs: SubDraft[] | null): Promise<boolean> => {
      if (!detail || !canWrite) return Promise.resolve(false)
      const qid = detail.qid
      const qn = String(detail.meta.question_number)
      const body: SaveBody = subs
        ? {
            subs: subs.map((s) => ({
              label: s.label,
              tags: s.tags,
              difficulty: s.difficulty, // 可 null（docs/13）
              primary_tag: s.primaryTag, // 单标签可传 null，后端补全（§2.1）
            })),
          }
        : { tags: draftTags, difficulty: draftDifficulty, primary_tag: draftPrimary }
      // 齐备度按「提交那一刻的草稿」算；draftSubs 始终是全部小问，
      // 所以口径仍是「全部小问都有标签」，不受本次只提交子集影响。
      const snap = draftSubs
      const snapTags = draftTags
      const snapDiff = draftDifficulty
      const snapPri = draftPrimary
      const split = splitMode
      setSavingCount((n) => n + 1)
      const run = saveChainRef.current
        .catch(() => undefined) // 上一次失败不该阻断这一次
        .then(() => api.saveAnnotation(qid, body))
        .then(
          (result) => {
            setSavingCount((n) => Math.max(0, n - 1))
            // 「单元是否齐备」与「任意单元有标签」都要一并更新：
            // 前者管预览里的灰/彩与确认门禁，后者管预览列表的可见性（部分打标也要能看到）
            const tagsReady = split ? snap.every((s) => s.tags.length > 0) : snapTags.length > 0
            const anyTagged = split ? snap.some((s) => s.tags.length > 0) : snapTags.length > 0
            const diffReady = split ? snap.every((s) => s.difficulty !== null) : snapDiff !== null
            const priReady = split
              ? snap.every((s) => primaryOk(s.tags, s.primaryTag))
              : primaryOk(snapTags, snapPri)
            setQuestions((prev) =>
              prev.map((q) =>
                q.qid === result.qid
                  ? {
                      ...q,
                      status: result.status,
                      tags: result.tags,
                      primary_tag: result.primary_tag ?? null,
                      difficulty: result.difficulty,
                      tags_ready: tagsReady,
                      difficulty_ready: diffReady,
                      primary_ready: priReady,
                      any_tagged: anyTagged,
                      split: result.split ?? q.split ?? false,
                      sub_labels: result.sub_labels ?? q.sub_labels,
                      // 改动使原确认失效 → 左侧题号立刻退回「未确认」
                      ...(result.confirm_cleared?.length
                        ? { verify_state: 'none' as VerifyState, confirmed_units: 0 }
                        : {}),
                    }
                  : q,
              ),
            )
            // 卷进度跟着「这题到底算不算已打标」走：逐个打标时最后一问落库会让
            // status 从 partial 变 done，annotated_count 必须同步，
            // 否则出现「题号变绿了、进度数字没跟上」
            setPapers((prev) => {
              const prevQ = questionsRef.current.find((q) => q.qid === result.qid)
              if ((prevQ?.status === 'done') === (result.status === 'done')) return prev
              return prev.map((p) =>
                p.id === paperId
                  ? {
                      ...p,
                      annotated_count: p.annotated_count + (result.status === 'done' ? 1 : -1),
                    }
                  : p,
              )
            })
            // 就地刷新「库里的小问标签」快照：detail 不会重拉，
            // 后续判断「这一问是不是被勾空了」全靠它（见 subsToSubmit）。
            const resSubs = (result as SaveResult & { subs?: { label: string; tags: string[] }[] })
              .subs
            if (resSubs) {
              savedSubTagsRef.current = new Map(
                resSubs.map((s) => [String(s.label), [...(s.tags ?? [])]]),
              )
            } else if (subs) {
              for (const s of subs) {
                savedSubTagsRef.current.set(String(s.label), [...s.tags])
              }
            }
            const warn = result.warnings?.length ? `（注意：${result.warnings[0]}）` : ''
            const cleared = result.confirm_cleared?.length ? '（改动已让原确认失效）' : ''
            const what = subs ? ` ${subs.map((s) => s.label).join(' / ')} 问已保存` : '已保存'
            showToast({ kind: 'ok', text: `第 ${qn} 题${what}${cleared}${warn}` })
            return true
          },
          (e: Error) => {
            setSavingCount((n) => Math.max(0, n - 1))
            showToast({ kind: 'err', text: `保存失败：${e.message}` })
            return false
          },
        )
      saveChainRef.current = run.then(
        () => undefined,
        () => undefined,
      )
      return run
    },
    [
      detail, canWrite, draftTags, draftDifficulty, draftPrimary, draftSubs,
      splitMode, questions, paperId, showToast,
    ],
  )

  /** 最后一问的「保存」：落库成功才跳到下一题；失败原地不动。 */
  const save = useCallback(async () => {
    if (!detail || saving || !canWrite) return
    if (splitMode) {
      if (!subsSaveable) return
    } else if (draftTags.length === 0) return
    const ok = await commit(splitMode ? subsToSubmit() : null)
    if (!ok) return
    if (currentIndex < questions.length - 1) setCurrentIndex(currentIndex + 1)
  }, [
    detail, saving, canWrite, splitMode, subsSaveable, draftTags.length,
    commit, subsToSubmit, currentIndex, questions.length,
  ])

  /**
   * 「保存，下一问」：先落库，**成功才把焦点推到下一问**。
   * 失败就原地不动——宁可挡住，也不让界面显示已存而实际没存（这曾是本 bug 的一半）。
   * 与 save() 走同一个 commit()，不再有两套保存语义。
   */
  const saveAndAdvance = useCallback(async () => {
    if (!detail || !canWrite) return
    if (!splitMode || onLastSub) return
    // 这一问啥都没标、也没有要清空的 → 没有可提交的内容，直接翻到下一问
    // （不发空 subs 的请求，后端会以 422「至少要给一个小问打标签」拒掉）
    const subs = subsToSubmit()
    if (subs.length > 0) {
      const ok = await commit(subs)
      if (!ok) return // 没存上就不前进：界面不能显示「已到下一问」而实际没落库
    }
    setFocusIdx((i) => Math.min(i + 1, draftSubs.length - 1))
  }, [detail, canWrite, splitMode, onLastSub, commit, subsToSubmit, draftSubs.length])

  /** 底部那颗按钮的唯一行为：中间小问=保存并下一问，最后一问=保存并跳题。 */
  const primaryAction = useCallback(() => {
    if (splitMode && !onLastSub) void saveAndAdvance()
    else void save()
  }, [splitMode, onLastSub, saveAndAdvance, save])

  /**
   * 预览模式选难度：只暂存（真写库仍在勾「确认」时发生），但**与已存难度不同**时
   * 立刻把该单元的「核对完成」撤掉——与打标模式保存后的行为保持一致：动了难度，
   * 原来的「我已核对」就不该再亮着（docs/12 §③）。
   */
  const previewPickDifficulty = useCallback(
    async (value: string) => {
      setPreviewPick(value)
      const d = previewDetail
      if (!d) return
      const split = d.split_active && d.subs.length > 0
      const idx = split ? Math.min(previewSubIdx, d.subs.length - 1) : 0
      const unit = split ? d.subs[idx]?.qid : d.qid
      const saved = split ? d.subs[idx]?.difficulty : d.difficulty
      const verified = split ? !!d.subs[idx]?.verified : !!d.verified
      if (!unit || !verified || saved === value) return // 本来没确认、或难度没变 → 不动
      try {
        await api.confirmQuestion(unit, false, null)
      } catch (e) {
        showToast({ kind: 'err', text: `撤销确认失败：${(e as Error).message}` })
        return
      }
      const nextSubs = split
        ? d.subs.map((s) => (s.qid === unit ? { ...s, verified: false } : s))
        : d.subs
      const total = split ? nextSubs.length : 1
      const done = nextSubs.filter((s) => s.verified).length
      const state: VerifyState = total && done === total ? 'all' : done ? 'partial' : 'none'
      setPreviewDetail({
        ...d,
        subs: nextSubs,
        verified: split ? state === 'all' : false,
        verify_state: state,
        confirmed_units: done,
        total_units: total,
      })
      setQuestions((prev) =>
        prev.map((q) =>
          q.qid === d.qid ? { ...q, verify_state: state, confirmed_units: done } : q,
        ),
      )
      if (d.verify_state === 'all') {
        setPapers((prev) =>
          prev.map((p) =>
            p.id === d.paper_id
              ? { ...p, verified_count: Math.max(0, (p.verified_count ?? 0) - 1) }
              : p,
          ),
        )
      }
      showToast({ kind: 'ok', text: '难度已改选 · 原确认失效' })
    },
    [previewDetail, previewSubIdx, showToast],
  )

  const currentPaper = papers.find((p) => p.id === paperId)

  // 预览模式：切分题当前选中问（联动中间题图与右侧标记）
  const previewSub =
    mode === 'preview' && previewDetail?.split_active && previewDetail.subs.length > 0
      ? previewDetail.subs[Math.min(previewSubIdx, previewDetail.subs.length - 1)]
      : null

  // 预览模式：勾选/取消「审核确认」（docs/11 + docs/13）
  //   非切分题 = 确认整题；切分题 = 确认当前小问。
  //   若本次在预览里补/改了难度（previewPick），随确认一起写回题库。
  //   确认成功后：切分题先切到下一个未确认小问，本题全确认后跳下一题；非切分题直接跳下一题。
  //   取消确认不跳转、也不回退难度。
  const handlePreviewConfirm = useCallback(
    async (on: boolean) => {
      const d = previewDetail
      if (!d || confirming || !canWrite) return
      const split = d.split_active && d.subs.length > 0
      const idx = split ? Math.min(previewSubIdx, d.subs.length - 1) : 0
      if (split && !d.subs[idx]) return
      const unit = split ? d.subs[idx].qid : d.qid
      const savedDiff = split ? d.subs[idx].difficulty : d.difficulty
      if (on && !savedDiff && !previewPick) return // 没难度不能确认（勾选框已禁用，这里兜底）
      const unitTags = split ? d.subs[idx].tags : d.tags
      const unitPrimary = split
        ? resolvePrimary(d.subs[idx].tags, d.subs[idx].primary_tag)
        : resolvePrimary(d.tags, d.primary_tag)
      if (on && !primaryOk(unitTags, unitPrimary)) return // 多标签没主标签也不能确认（同上兜底）

      setConfirming(true)
      try {
        const res = await api.confirmQuestion(unit, on, on ? previewPick : null)

        const writtenDiff = res.difficulty ?? null
        const nextSubs = split
          ? d.subs.map((s) =>
              s.qid === unit ? { ...s, verified: on, difficulty: writtenDiff ?? s.difficulty } : s,
            )
          : d.subs
        const difficulty = split ? d.difficulty : (writtenDiff ?? d.difficulty)
        const total = split ? nextSubs.length : 1
        const done = split ? nextSubs.filter((s) => s.verified).length : on ? 1 : 0
        const state: VerifyState = total && done === total ? 'all' : done ? 'partial' : 'none'

        // 「单元是否齐备」——补完难度后题目可能从 partial 变 done（docs/13）
        const tagsReady = split ? nextSubs.every((s) => s.tags.length > 0) : d.tags.length > 0
        const diffReady = split ? nextSubs.every((s) => !!s.difficulty) : !!difficulty
        const priReady = split
          ? nextSubs.every((s) => primaryOk(s.tags, s.primary_tag))
          : primaryOk(d.tags, d.primary_tag)
        const status: Status = tagsReady ? 'done' : diffReady ? 'partial' : 'none'
        const prevStatus = previewQuestions.find((q) => q.qid === d.qid)?.status ?? 'none'

        // 本地乐观更新：右侧详情 + 左侧题号
        setPreviewDetail({
          ...d,
          subs: nextSubs,
          difficulty,
          verified: split ? state === 'all' : on,
          verify_state: state,
          confirmed_units: done,
          total_units: total,
        })
        setQuestions((prev) =>
          prev.map((q) =>
            q.qid === d.qid
              ? {
                  ...q,
                  status,
                  tags_ready: tagsReady,
                  difficulty_ready: diffReady,
                  primary_ready: priReady,
                  difficulty: split ? q.difficulty : (difficulty ?? null),
                  verify_state: state,
                  confirmed_units: done,
                  total_units: total,
                }
              : q,
          ),
        )
        // 卷进度：已打标（补完难度可能 +1）与已确认（跨越 all 边界时 ±1）
        const prevState = d.verify_state ?? 'none'
        if (prevState !== state || prevStatus !== status) {
          setPapers((prev) =>
            prev.map((p) => {
              if (p.id !== d.paper_id) return p
              const dAnn =
                status === 'done' && prevStatus !== 'done'
                  ? 1
                  : status !== 'done' && prevStatus === 'done'
                    ? -1
                    : 0
              const dVer = state === 'all' ? 1 : prevState === 'all' ? -1 : 0
              return {
                ...p,
                annotated_count: p.annotated_count + dAnn,
                verified_count: (p.verified_count ?? 0) + dVer,
              }
            }),
          )
        }
        if (writtenDiff) setPreviewPick(null) // 已写回，撤掉「已改选」标记

        if (!on) return // 取消确认：不跳转
        if (split) {
          const n = nextSubs.length
          let next = -1
          for (let k = 1; k <= n; k++) {
            const j = (idx + k) % n
            if (!nextSubs[j].verified) {
              next = j
              break
            }
          }
          if (next >= 0) {
            setPreviewSubIdx(next)
            setPreviewPick(null)
          } else if (previewIndex < previewQuestions.length - 1) {
            setPreviewIndex(previewIndex + 1)
          } else {
            showToast({ kind: 'ok', text: '已是本卷最后一题' })
          }
        } else if (previewIndex < previewQuestions.length - 1) {
          setPreviewIndex(previewIndex + 1)
        } else {
          showToast({ kind: 'ok', text: '已是本卷最后一题' })
        }
      } catch (e) {
        showToast({ kind: 'err', text: `确认失败：${(e as Error).message}` })
      } finally {
        setConfirming(false)
      }
    },
    [
      previewDetail,
      confirming,
      previewSubIdx,
      previewIndex,
      previewQuestions,
      previewPick,
      showToast,
      canWrite,
    ],
  )

  // 预览模式：切换小问（补难度只对当前小问有效，切换即丢）
  const pickPreviewSub = useCallback((i: number) => {
    setPreviewSubIdx(i)
    setPreviewPick(null)
  }, [])

  // 快捷键：←/→ 翻题（预览模式翻预览列表），1–5 难度（split 模式作用于焦点组），Enter 保存，
  //         预览模式下空格 = 勾选/取消「我已核对」（等价于点右侧确认框）。
  // （预览模式难度/保存快捷键禁用；「有误」的题与只读账号只留翻页）
  // 注意：这段必须排在 handlePreviewConfirm 之后 —— deps 里要用它，提前会撞 TDZ。
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (view !== 'workbench') return // 标签管理页里不吃打标台快捷键
      const frozen = !canWrite || (mode === 'preview' ? previewDetail : detail)?.flagged === true
      const el = e.target as HTMLElement | null
      const typing = el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)

      if (mode === 'preview') {
        if (e.key === 'ArrowLeft' && previewIndex > 0) setPreviewIndex(previewIndex - 1)
        else if (e.key === 'ArrowRight' && previewIndex < previewQuestions.length - 1)
          setPreviewIndex(previewIndex + 1)
        else if (!frozen && !typing && (e.key === ' ' || e.code === 'Space')) {
          // 空格 = 切换当前单元的确认状态。默认行为（滚动页面 / 触发聚焦按钮的 click）必须挡掉，
          // 否则焦点落在难度按钮或确认框上时会双重触发。
          // 长按不确认（防连跳题），但也照样 preventDefault —— 否则页面会一路滚下去。
          if (e.repeat) {
            e.preventDefault()
            return
          }
          const d = previewDetail
          if (!d) return
          e.preventDefault()
          const split = d.split_active && d.subs.length > 0
          const idx = split ? Math.min(previewSubIdx, d.subs.length - 1) : 0
          const cur = split ? !!d.subs[idx]?.verified : !!d.verified
          void handlePreviewConfirm(!cur)
        } else if (!frozen && !typing && e.key >= '1' && e.key <= '5') {
          // 1–5 就地选难度（与打标模式一致）；只暂存，勾「确认」时才写回题库
          void previewPickDifficulty(DIFFICULTY_LABELS[Number(e.key) - 1][0])
        }
        return
      }
      if (e.key === 'ArrowLeft') gotoPrev()
      else if (e.key === 'ArrowRight') gotoNext()
      else if (frozen) return
      else if (typing) return
      else if (e.key === 'Enter' && saveEnabled) primaryAction()
      else if (e.key === '1') pickDifficulty('very-easy')
      else if (e.key === '2') pickDifficulty('easy')
      else if (e.key === '3') pickDifficulty('medium')
      else if (e.key === '4') pickDifficulty('hard')
      else if (e.key === '5') pickDifficulty('very-hard')
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [
    mode,
    previewIndex,
    previewQuestions.length,
    previewSubIdx,
    gotoPrev,
    gotoNext,
    pickDifficulty,
    previewPickDifficulty,
    handlePreviewConfirm,
    primaryAction,
    saveEnabled,
    view,
    detail,
    previewDetail,
    canWrite,
  ])

  // ── 身份闸门：未登录只渲染登录页，首次登录强制改密 ──
  if (booting) {
    return (
      <div className="grid h-screen place-items-center bg-white text-sm text-gray-400">
        正在加载…
      </div>
    )
  }
  if (!me) {
    return <LoginPage onLogin={setMe} />
  }
  if (me.must_change_password) {
    return (
      <ChangePasswordPage
        user={me}
        onDone={(u) => {
          // 清掉强制改密期间可能残留的报错提示（旧逻辑会在这里留下一条 428 的红字）
          setToast(null)
          setMe(u)
        }}
      />
    )
  }
  if (changingPw) {
    return (
      <ChangePasswordPage
        user={me}
        onDone={(u) => {
          setMe(u)
          setChangingPw(false)
          showToast({ kind: 'ok', text: '密码已修改' })
        }}
        onCancel={() => setChangingPw(false)}
      />
    )
  }

  // 标签管理页：整页切换（打标台状态留在本组件里，回来位置不丢）
  if (view === 'tagadmin') {
    return (
      <TagAdmin
        initialBoard={board}
        initialQual={qualification}
        initialSubject={subject}
        initialUnit={currentPaper?.unit}
        onExit={() => {
          setView('workbench')
          setRefreshTick((t) => t + 1)
        }}
      />
    )
  }

  // 操作日志页：整页切换（仅管理员入口；后端 /api/auth/audit 另有 admin 校验兜底）
  if (view === 'audit') {
    return <AuditLog onExit={() => setView('workbench')} />
  }

  // 全局概览页：整页切换（仅管理员；后端 /api/overview 另有 admin 校验兜底）。
  // 点格子里的「打开这卷」→ 三个下拉 + paperId 一起设，否则 Sidebar 按选择过滤会找不到这卷。
  if (view === 'overview') {
    return (
      <Overview
        onExit={() => setView('workbench')}
        onOpen={(p) => {
          setBoard(p.board)
          setQualification(p.qualification)
          setSubject(p.subject)
          setPaperId(p.id)
          setView('workbench')
        }}
      />
    )
  }

  return (
    <div className="flex h-screen flex-col bg-white text-gray-900">
      <header className="flex shrink-0 items-center justify-between border-b border-gray-200 px-4 py-2.5">
        <div className="flex items-center gap-3">
          {/* 模式切换（最左上） */}
          <div className="flex rounded-lg border border-gray-300 bg-gray-100 p-0.5 text-sm">
            <button
              type="button"
              onClick={() => switchMode('annotate')}
              className={`rounded-md px-3 py-1 font-medium transition-colors ${
                mode === 'annotate'
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              打标模式
            </button>
            <button
              type="button"
              onClick={() => switchMode('preview')}
              className={`rounded-md px-3 py-1 font-medium transition-colors ${
                mode === 'preview'
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              预览模式
            </button>
          </div>
          <h1 className="text-base font-medium">题库审核打标台</h1>
        </div>
        <div className="flex items-center gap-4">
          {toast && (
            <span
              className={`rounded-md px-2.5 py-1 text-xs ${
                toast.kind === 'ok' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
              }`}
            >
              {toast.text}
            </span>
          )}
          {me.role === 'admin' && (
            <button
              type="button"
              onClick={() => setView('overview')}
              title="全部试卷的完成度地图（按学科分区，一格一卷）"
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-50 hover:text-gray-900"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                className="h-3.5 w-3.5 text-gray-500"
              >
                <rect x="3" y="3" width="7" height="7" rx="1" />
                <rect x="14" y="3" width="7" height="7" rx="1" />
                <rect x="3" y="14" width="7" height="7" rx="1" />
                <rect x="14" y="14" width="7" height="7" rx="1" />
              </svg>
              全局概览
            </button>
          )}
          {me.role === 'admin' && (
            <button
              type="button"
              onClick={() => setView('audit')}
              title="谁在什么时候改了哪道题（近 7 天，管理员可见）"
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-50 hover:text-gray-900"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-3.5 w-3.5 text-gray-500"
              >
                <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
              </svg>
              操作日志
            </button>
          )}
          {canEditTaxonomy(me.role) && (
            <button
              type="button"
              onClick={() => setView('tagadmin')}
              title="维护标签词表（改中英文名 / 新增一级组与二级标签）"
              className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 transition-colors hover:bg-gray-50 hover:text-gray-900"
            >
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-3.5 w-3.5 text-gray-500"
              >
                <path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82Z" />
                <circle cx="7" cy="7" r="1.2" />
              </svg>
              标签管理
            </button>
          )}
          {currentPaper && (
            <span className="text-sm text-gray-500">
              卷进度：已打标{' '}
              <span className="font-medium text-gray-900">
                {currentPaper.annotated_count}/{currentPaper.question_count}
              </span>
            </span>
          )}
          {/* 当前身份：点一下能改密 / 退出。学科名也显示出来，避免老师登错学科账号还不知道 */}
          <div className="flex items-center gap-2 border-l border-gray-200 pl-3">
            <div className="text-right leading-tight">
              <div className="text-xs font-medium text-gray-800">{me.name}</div>
              <div className="text-[10px] text-gray-400">
                {me.role === 'admin'
                  ? '管理员'
                  : me.role === 'lead'
                    ? '学科负责人'
                    : me.role === 'viewer'
                      ? '只读'
                      : me.subjects.includes('*')
                        ? '全部学科'
                        : me.subjects.join(' / ')}
              </div>
            </div>
            <button
              type="button"
              onClick={() => setChangingPw(true)}
              title="修改密码"
              className="rounded-md border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
            >
              改密
            </button>
            <button
              type="button"
              onClick={async () => {
                try {
                  await authApi.logout()
                } catch {
                  /* 即使请求失败也把本地状态清掉 */
                }
                setMe(null)
                setPapers([])
                setPaperId('')
                setQuestions([])
                setDetail(null)
              }}
              className="rounded-md border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
            >
              退出
            </button>
          </div>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <Sidebar
          papers={papers}
          visiblePapers={visiblePapers}
          board={board}
          qualification={qualification}
          subject={subject}
          paperId={paperId}
          questions={mode === 'preview' ? previewQuestions : questions}
          currentIndex={mode === 'preview' ? previewIndex : currentIndex}
          mode={mode}
          onBoard={setBoard}
          onQualification={setQualification}
          onSubject={setSubject}
          onPaper={(v) => setPaperId(v)}
          onJump={(i) => {
            if (mode === 'preview') {
              setPreviewIndex(i)
              return
            }
            guardedJump(i)
          }}
        />

        {mode === 'preview' ? (
          previewQuestions.length === 0 ? (
            <div className="grid flex-1 place-items-center text-sm text-gray-400">
              {questions.length === 0 ? '在左侧选择一张试卷' : '该卷暂无已打标的题目'}
            </div>
          ) : (
            <>
              <main className="flex min-w-0 flex-1 flex-col">
                <QuestionViewer
                  detail={previewDetail}
                  onPrev={() => setPreviewIndex((i) => Math.max(0, i - 1))}
                  onNext={() =>
                    setPreviewIndex((i) => Math.min(previewQuestions.length - 1, i + 1))
                  }
                  hasPrev={previewIndex > 0}
                  hasNext={previewIndex < previewQuestions.length - 1}
                  imageOverride={previewSub?.image_url ?? null}
                  subLabel={previewSub?.label ?? null}
                  flagged={previewDetail?.flagged === true}
                  onToggleFlag={toggleFlag}
                  canFlag={canWrite}
                />
              </main>
              {previewDetail && (
                <>
                  <ResizeHandle
                    width={rightW}
                    min={RIGHT_MIN}
                    max={maxRight}
                    defaultWidth={RIGHT_DEFAULT}
                    onChange={setRightW}
                  />
                  <PreviewPanel
                    detail={previewDetail}
                    subIdx={previewSubIdx}
                    onSubIdx={pickPreviewSub}
                    width={rightW}
                    onConfirm={handlePreviewConfirm}
                    confirming={confirming}
                    pick={previewPick}
                    onPickDifficulty={previewPickDifficulty}
                    readOnly={!canWrite}
                  />
                </>
              )}
            </>
          )
        ) : questions.length === 0 ? (
          <div className="grid flex-1 place-items-center text-sm text-gray-400">
            {papers.length === 0 ? '加载中…' : '在左侧选择一张试卷开始打标'}
          </div>
        ) : (
          <>
            <main className="flex min-w-0 flex-1 flex-col">
              <QuestionViewer
                detail={detail}
                onPrev={gotoPrev}
                onNext={gotoNext}
                hasPrev={currentIndex > 0}
                hasNext={currentIndex < questions.length - 1}
                flagged={detail?.flagged === true}
                onToggleFlag={toggleFlag}
                canFlag={canWrite}
              />
            </main>

            <ResizeHandle
              width={rightW}
              min={RIGHT_MIN}
              max={maxRight}
              defaultWidth={RIGHT_DEFAULT}
              onChange={setRightW}
            />

            {/* data-annotate-panel：端到端测试锚点（右侧打标面板，与左侧题列表 aside 区分） */}
            <aside
              data-annotate-panel="1"
              data-qid={detail?.qid ?? ''}
              data-flagged={detail?.flagged ? '1' : ''}
              style={{ width: rightW }}
              // 「有误」的题整块冻结：inert 让子树不可聚焦/不可点（含输入框、开关、保存按钮），
              // pointer-events-none 再兜一层（部分浏览器对 inert 的支持有差异）。
              inert={detail?.flagged ? true : undefined}
              className={`flex shrink-0 flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white p-3 ${
                detail?.flagged ? 'pointer-events-none select-none opacity-50' : ''
              }`}
            >
              {/* 只读账号提示：面板不整体禁用（还要能看、能翻），但每个可写控件都禁了 */}
              {!canWrite && (
                <div className="shrink-0 rounded-lg border border-gray-300 bg-gray-50 px-2.5 py-2 text-xs text-gray-600">
                  只读账号（viewer）：可以翻题、看标注，<span className="text-gray-800">不能修改标签/难度、不能确认、不能标有误</span>。
                  需要写权限请联系管理员提权。
                </div>
              )}
              {/* 小问切分开关（三个考试局都有；Edexcel 会额外裁子图，AQA/CIE 只打标不切） */}
              {detail && (
                <div className="shrink-0 rounded-lg border border-gray-200 bg-gray-50 p-2.5">
                  <label
                    className={`flex items-center justify-between gap-2 ${
                      detail.split_eligible ? 'cursor-pointer' : 'cursor-not-allowed'
                    }`}
                  >
                    <span className="text-sm font-medium text-gray-700">按小问分别打标</span>
                    <span className="relative inline-flex shrink-0">
                      <input
                        type="checkbox"
                        checked={splitMode}
                        disabled={!detail.split_eligible || !canWrite}
                        onChange={(e) => toggleSplit(e.target.checked)}
                        className="peer sr-only"
                      />
                      {/* 轨道 */}
                      <span className="block h-5 w-9 rounded-full bg-gray-300 transition-colors peer-checked:bg-blue-600 peer-disabled:bg-gray-200 peer-focus-visible:ring-2 peer-focus-visible:ring-blue-500 peer-focus-visible:ring-offset-1" />
                      {/* 圆点 */}
                      <span className="pointer-events-none absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform peer-checked:translate-x-4" />
                    </span>
                  </label>
                  {!detail.split_eligible && (
                    <p className="mt-1 text-[11px] leading-4 text-gray-400">
                      本题无可分小问，开关暂不可用
                    </p>
                  )}
                </div>
              )}

              {/* 焦点组选择器（split 模式）：平铺按钮，颜色标完成状态（绿=✓完成 · 琥珀=◐进行中 · 灰=○未开始） */}
              {splitMode && draftSubs.length > 0 && (
                <div className="shrink-0">
                  <div className="mb-1.5 flex items-baseline justify-between">
                    <span className="text-sm font-medium text-gray-700">小问分组</span>
                    <span className="text-xs font-normal text-gray-400">
                      {subDoneCount}/{draftSubs.length} 完成
                    </span>
                  </div>
                  {/* auto-fill（不是 auto-fit）：列宽恒定，最后一行不满时按钮保持等大、不被拉伸 */}
                  <div
                    className="grid gap-1.5"
                    style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(2.75rem, 1fr))' }}
                  >
                    {draftSubs.map((s, i) => {
                      const st = subState(s)
                      const active = i === focusIdx
                      const cls = active
                        ? 'border-blue-500 bg-blue-50 text-blue-700 shadow-sm'
                        : st === 'done'
                          ? 'border-green-300 bg-green-50 text-green-700 hover:bg-green-100'
                          : st === 'partial'
                            ? 'border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100'
                            : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
                      return (
                        <button
                          key={s.label}
                          type="button"
                          title={`${s.label} 问 · ${st === 'done' ? '完成' : st === 'partial' ? '进行中' : '未开始'}`}
                          onClick={() => setFocusIdx(i)}
                          className={`w-full whitespace-nowrap rounded-md border px-1 py-1 text-xs font-medium transition-colors ${cls}`}
                        >
                          {s.label}
                        </button>
                      )
                    })}
                  </div>
                  <p className="mt-1 text-[11px] leading-4 text-gray-400">
                    <span className="mr-2 inline-flex h-2 w-2 rounded-sm bg-green-300 align-middle" />
                    完成
                    <span className="mx-2 inline-flex h-2 w-2 rounded-sm bg-amber-300 align-middle" />
                    进行中
                    <span className="mx-2 inline-flex h-2 w-2 rounded-sm bg-gray-200 align-middle" />
                    未开始
                  </p>
                </div>
              )}

              {detail && (
                <TagPanel
                  tags={detail.available_tags}
                  groups={detail.available_groups}
                  unit={detail.unit}
                  selected={currentTags}
                  onToggle={toggleTag}
                  primaryTag={currentPrimary}
                  onSetPrimary={setPrimaryTag}
                  readOnly={!canWrite}
                />
              )}
              <div className="shrink-0 border-t border-gray-100 pt-3">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-700">
                    难度{splitMode && focusSub ? `（${focusSub.label} 问）` : ''}
                  </span>
                  <span className="text-xs font-normal text-gray-400">单选 · 可留空（快捷键 1–5）</span>
                </div>
                <div className="flex gap-1.5">
                  {DIFFICULTY_LABELS.map(([value, label, cls]) => (
                    <button
                      key={value}
                      type="button"
                      disabled={!canWrite}
                      onClick={() => pickDifficulty(value)}
                      className={`flex-1 rounded-md border px-1.5 py-1 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                        currentDifficulty === value
                          ? cls
                          : 'border-gray-300 bg-white text-gray-600 hover:bg-gray-50'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="shrink-0 space-y-2">
                <button
                  type="button"
                  data-save="1"
                  disabled={!saveEnabled}
                  onClick={primaryAction}
                  className={`w-full rounded-md px-4 py-2 text-sm font-medium text-white transition-all duration-100 select-none ${
                    saveEnabled
                      ? 'bg-blue-600 shadow-sm hover:bg-blue-700 active:scale-[0.97] active:bg-blue-800 active:shadow-none'
                      : 'cursor-not-allowed bg-gray-300'
                  }`}
                >
                  {saving
                    ? '保存中…'
                    : splitMode
                      ? onLastSub
                        ? `保存（${subDoneCount}/${draftSubs.length} 问已完成）`
                        : '保存，下一问'
                      : '确定并保存'}
                </button>
                <p className="text-center text-[11px] text-gray-400">
                  {missingPrimary ? (
                    <span className="font-medium text-amber-600">
                      有题选了多个标签，请先点标签右侧圆点指定主标签
                    </span>
                  ) : (
                    '快捷键：←/→ 翻题 · Enter 保存'
                  )}
                </p>
              </div>
            </aside>
          </>
        )}
      </div>
    </div>
  )
}
