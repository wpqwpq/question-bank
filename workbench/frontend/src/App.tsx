import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Paper, QuestionDetail, QuestionSummary, Status, SubDraft, VerifyState } from './api'
import { api, DIFFICULTY_LABELS, primaryOk, resolvePrimary } from './api'
import Sidebar from './components/Sidebar'
import TagAdmin from './components/TagAdmin'
import TagPanel from './components/TagPanel'
import QuestionViewer from './components/QuestionViewer'
import PreviewPanel from './components/PreviewPanel'
import ResizeHandle from './components/ResizeHandle'

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
  const [papers, setPapers] = useState<Paper[]>([])
  const [board, setBoard] = useState('edexcel')
  const [qualification, setQualification] = useState('a-level')
  const [subject, setSubject] = useState('physics')
  const [paperId, setPaperId] = useState('')
  const [questions, setQuestions] = useState<QuestionSummary[]>([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [detail, setDetail] = useState<QuestionDetail | null>(null)
  const [draftTags, setDraftTags] = useState<string[]>([])
  const [draftDifficulty, setDraftDifficulty] = useState<string | null>(null)
  /** 整题模式的主标签草稿（切分题逐问存在 draftSubs[].primaryTag 上） */
  const [draftPrimary, setDraftPrimary] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<Toast>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  // ── 页面：打标台 / 标签管理（右上入口跳转，整页切换）──
  const [view, setView] = useState<'workbench' | 'tagadmin'>('workbench')
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

  // 预览列表（docs/13 + plans/主标签实施方案 §2）：齐备的题 + 「有标签但缺难度或缺主标签」的题
  // （灰色，前两者可回到打标模式补齐）。「只有难度没有标签」的题不在列表中（tags_ready=false）。
  const previewQuestions = useMemo(
    () =>
      questions.filter(
        (q) =>
          q.status === 'done' ||
          (q.tags_ready === true && (q.difficulty_ready === false || q.primary_ready === false)),
      ),
    [questions],
  )

  // 初始加载卷列表
  useEffect(() => {
    api
      .papers()
      .then(setPapers)
      .catch((e: Error) => setToast({ kind: 'err', text: `加载卷列表失败：${e.message}` }))
  }, [])

  const showToast = useCallback((t: Toast) => {
    setToast(t)
    window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(null), 3000)
  }, [])

  // 切换考试局/资格/学科 → 当前卷必不属于新维度，立即关闭
  // （清 paperId，由下方切卷 effect 级联清空题目/详情/预览状态）
  useEffect(() => {
    setPaperId('')
  }, [board, qualification, subject])

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
    api
      .questions(paperId)
      .then(setQuestions)
      .catch((e: Error) => showToast({ kind: 'err', text: `加载题目失败：${e.message}` }))
  }, [paperId, showToast])

  // 拉当前题详情，重置草稿（切分状态按 开关意图 × 本题资格 推导）
  // 依赖刻意只取「当前题的 qid」而不是整个 questions 数组：增量保存（savePartial）会更新
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
          const ex = d.subs.find((s) => s.label === l)
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

  // 切分题：每个小问至少 1 个标签即可保存；难度可留空（docs/13）
  const subsTagged = draftSubs.length > 0 && draftSubs.every((s) => s.tags.length > 0)
  const subDoneCount = draftSubs.filter((s) => subState(s) === 'done').length
  // ≥2 个标签必须有主标签（plans/主标签实施方案 §4.1）——整题与切分题各自校验
  const primaryAllOk = splitMode
    ? draftSubs.length > 0 && draftSubs.every((s) => primaryOk(s.tags, s.primaryTag))
    : primaryOk(draftTags, draftPrimary)
  const canSave =
    !saving &&
    !!detail &&
    (splitMode ? subsTagged && primaryAllOk : draftTags.length > 0 && primaryAllOk)
  /** 「保存，下一问」只放行「焦点小问没问题」的情况，避免落到后端 422 */
  const focusSubOk =
    !splitMode || !focusSub || focusSub.tags.length === 0 || primaryOk(focusSub.tags, focusSub.primaryTag)
  /** 底部提示用：确实存在「≥2 标签却没指定主标签」的单元 */
  const missingPrimary = splitMode
    ? draftSubs.some((s) => s.tags.length >= 2 && !primaryOk(s.tags, s.primaryTag))
    : draftTags.length >= 2 && !primaryOk(draftTags, draftPrimary)

  // split 模式：焦点在最后一问时按钮才是「保存」，否则是「下一问」导航
  const onLastSub = !splitMode || focusIdx >= draftSubs.length - 1
  /** 「保存，下一问」是否可点：焦点小问要么还没打标、要么主标签齐备 */
  const canAdvance = splitMode && !onLastSub && !saving && focusSubOk
  const saveEnabled = splitMode && !onLastSub ? canAdvance : canSave

  const save = useCallback(async () => {
    if (!detail || saving) return
    if (splitMode) {
      if (!subsTagged) return
    } else if (draftTags.length === 0) return
    setSaving(true)
    try {
      const body = splitMode
        ? {
            subs: draftSubs.map((s) => ({
              label: s.label,
              tags: s.tags,
              difficulty: s.difficulty, // 可 null（docs/13）
              primary_tag: s.primaryTag, // 单标签可传 null，后端补全（§2.1）
            })),
          }
        : { tags: draftTags, difficulty: draftDifficulty, primary_tag: draftPrimary }
      const result = await api.saveAnnotation(detail.qid, body)
      // 「单元是否齐备」也一并更新，否则切到预览模式时新标好的题可能不进列表（docs/13）
      const tagsReady = splitMode ? draftSubs.every((s) => s.tags.length > 0) : draftTags.length > 0
      const diffReady = splitMode
        ? draftSubs.every((s) => s.difficulty !== null)
        : draftDifficulty !== null
      const priReady = splitMode
        ? draftSubs.every((s) => primaryOk(s.tags, s.primaryTag))
        : primaryOk(draftTags, draftPrimary)
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
      setPapers((prev) =>
        prev.map((p) => {
          if (p.id !== paperId) return p
          const before = questions[currentIndex]
          const wasDone = before?.status === 'done'
          return {
            ...p,
            annotated_count:
              p.annotated_count +
              (result.status === 'done' && !wasDone ? 1 : result.status !== 'done' && wasDone ? -1 : 0),
          }
        }),
      )
      const warn = result.warnings?.length ? `（注意：${result.warnings[0]}）` : ''
      const cleared = result.confirm_cleared?.length ? '（改动已让原确认失效）' : ''
      showToast({ kind: 'ok', text: `第 ${String(detail.meta.question_number)} 题已保存${cleared}${warn}` })
      if (currentIndex < questions.length - 1) setCurrentIndex(currentIndex + 1)
    } catch (e) {
      showToast({ kind: 'err', text: `保存失败：${(e as Error).message}` })
    } finally {
      setSaving(false)
    }
  }, [detail, splitMode, subsTagged, primaryAllOk, draftSubs, draftDifficulty, draftPrimary, draftTags, saving, paperId, questions, currentIndex, showToast])

  /**
   * 「下一问」时的增量保存：把**已有标签**的小问先落库，避免中途切题/中断丢失。
   * 未打标的小问不提交——后端 `_save_split` 已放开为允许「本题小问的子集」。
   * 刻意不复用 save()：那个会弹 toast 并跳到下一题。
   */
  const savePartial = useCallback(async () => {
    if (!detail) return
    // 只提交「有标签且主标签已定」的小问：多标签却没选主的本小问会 422，
    // 静默跳过（与没打标的同级），等用户点右侧圆点定好主标签再随下一次落库。
    const filled = draftSubs.filter((s) => s.tags.length > 0 && primaryOk(s.tags, s.primaryTag))
    if (filled.length === 0) return
    try {
      const res = await api.saveAnnotation(detail.qid, {
        subs: filled.map((s) => ({
          label: s.label,
          tags: s.tags,
          difficulty: s.difficulty,
          primary_tag: s.primaryTag,
        })),
      })
      // 只刷新列表状态；detail 的 effect 现在只依赖 qid，不会因此重拉详情、重置草稿
      const tagsReady = draftSubs.every((s) => s.tags.length > 0)
      const diffReady = draftSubs.every((s) => s.difficulty !== null)
      const priReady = draftSubs.every((s) => primaryOk(s.tags, s.primaryTag))
      setQuestions((prev) =>
        prev.map((q) =>
          q.qid === res.qid
            ? {
                ...q,
                status: res.status,
                primary_tag: res.primary_tag ?? null,
                split: res.split ?? q.split ?? false,
                sub_labels: res.sub_labels ?? q.sub_labels,
                tags_ready: tagsReady,
                difficulty_ready: diffReady,
                primary_ready: priReady,
                // 增量保存若改动了某个小问，它的确认也随之失效
                ...(res.confirm_cleared?.length
                  ? { verify_state: 'none' as VerifyState, confirmed_units: 0 }
                  : {}),
              }
            : q,
        ),
      )
      if (res.confirm_cleared?.length) {
        showToast({ kind: 'ok', text: '已保存（改动让原确认失效）' })
      }
    } catch (e) {
      showToast({ kind: 'err', text: `保存失败：${(e as Error).message}` })
    }
  }, [detail, draftSubs, showToast])

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

  // 快捷键：←/→ 翻题（预览模式翻预览列表），1/2/3 难度（split 模式作用于焦点组），Enter 保存
  // （预览模式只读，难度/保存快捷键禁用）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (view !== 'workbench') return // 标签管理页里不吃打标台快捷键
      const el = e.target as HTMLElement | null
      const typing = el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)
      if (mode === 'preview') {
        if (e.key === 'ArrowLeft' && previewIndex > 0) setPreviewIndex(previewIndex - 1)
        else if (e.key === 'ArrowRight' && previewIndex < previewQuestions.length - 1)
          setPreviewIndex(previewIndex + 1)
        else if (!typing && e.key >= '1' && e.key <= '5') {
          // 1–5 就地选难度（与打标模式一致）；只暂存，勾「确认」时才写回题库
          void previewPickDifficulty(DIFFICULTY_LABELS[Number(e.key) - 1][0])
        }
        return
      }
      if (e.key === 'ArrowLeft') gotoPrev()
      else if (e.key === 'ArrowRight') gotoNext()
      else if (typing) return
      else if (e.key === 'Enter' && canSave) void save()
      else if (e.key === '1') pickDifficulty('very-easy')
      else if (e.key === '2') pickDifficulty('easy')
      else if (e.key === '3') pickDifficulty('medium')
      else if (e.key === '4') pickDifficulty('hard')
      else if (e.key === '5') pickDifficulty('very-hard')
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [mode, previewIndex, previewQuestions.length, gotoPrev, gotoNext, pickDifficulty, previewPickDifficulty, save, canSave, view])

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
      if (!d || confirming) return
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
    ],
  )

  // 预览模式：切换小问（补难度只对当前小问有效，切换即丢）
  const pickPreviewSub = useCallback((i: number) => {
    setPreviewSubIdx(i)
    setPreviewPick(null)
  }, [])

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
          {currentPaper && (
            <span className="text-sm text-gray-500">
              卷进度：已打标{' '}
              <span className="font-medium text-gray-900">
                {currentPaper.annotated_count}/{currentPaper.question_count}
              </span>
            </span>
          )}
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
              style={{ width: rightW }}
              className="flex shrink-0 flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white p-3"
            >
              {/* 小问切分开关 */}
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
                        disabled={!detail.split_eligible}
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
                  <div className="flex gap-1.5">
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
                          className={`flex-1 rounded-md border px-1.5 py-1 text-xs font-medium transition-colors ${cls}`}
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
                      onClick={() => pickDifficulty(value)}
                      className={`flex-1 rounded-md border px-1.5 py-1 text-xs transition-colors ${
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
                  onClick={() => {
                    if (splitMode && !onLastSub) {
                      void savePartial() // 先把已选标签落库，再推进焦点
                      setFocusIdx(focusIdx + 1)
                    } else void save()
                  }}
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
