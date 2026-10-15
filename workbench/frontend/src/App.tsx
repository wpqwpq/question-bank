import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Paper, QuestionDetail, QuestionSummary, SubDraft } from './api'
import { api, DIFFICULTY_LABELS } from './api'
import Sidebar from './components/Sidebar'
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

function subState(s: SubDraft): 'done' | 'partial' | 'none' {
  if (s.tags.length > 0 && s.difficulty !== null) return 'done'
  if (s.tags.length > 0 || s.difficulty !== null) return 'partial'
  return 'none'
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
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<Toast>(null)
  const toastTimer = useRef<number | undefined>(undefined)

  // ── 模式 ──
  const [mode, setMode] = useState<Mode>('annotate')
  const [previewIndex, setPreviewIndex] = useState(0)
  const [previewDetail, setPreviewDetail] = useState<QuestionDetail | null>(null)
  const [previewSubIdx, setPreviewSubIdx] = useState(0)

  // ── 小问切分状态 ──
  const splitOnRef = useRef(false) // 开关意图（跨题保持）；供题目加载effect读取
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
          p.board === board && p.qualification === qualification && p.subject === subject,
      ),
    [papers, board, qualification, subject],
  )

  // 预览列表：只显示已打标完成的题目
  const previewQuestions = useMemo(
    () => questions.filter((q) => q.status === 'done'),
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

  // 切卷 → 拉题目列表，回到第 1 题
  useEffect(() => {
    if (!paperId) {
      setQuestions([])
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
  useEffect(() => {
    const q = questions[currentIndex]
    if (!q) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .question(q.qid)
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
  }, [questions, currentIndex, showToast])

  // 预览模式：拉当前预览题详情（只读）
  useEffect(() => {
    if (mode !== 'preview') return
    const q = previewQuestions[previewIndex]
    if (!q) {
      setPreviewDetail(null)
      return
    }
    let cancelled = false
    api
      .question(q.qid)
      .then((d) => {
        if (!cancelled) {
          setPreviewDetail(d)
          setPreviewSubIdx(0) // 切题回第一问
        }
      })
      .catch((e: Error) => showToast({ kind: 'err', text: `加载题目失败：${e.message}` }))
    return () => {
      cancelled = true
    }
  }, [mode, previewQuestions, previewIndex, showToast])

  const switchMode = useCallback(
    (m: Mode) => {
      if (m === mode) return
      setMode(m)
      if (m === 'preview') setPreviewIndex(0)
    },
    [mode],
  )

  function applyDetail(d: QuestionDetail) {
    const mode = d.split_active || (splitOnRef.current && d.split_eligible)
    setSplitMode(mode)
    if (mode) {
      const labels = d.split_active && d.subs.length > 0 ? d.subs.map((s) => s.label) : d.top_labels
      setDraftSubs(
        labels.map((l) => {
          const ex = d.subs.find((s) => s.label === l)
          return { label: l, tags: ex?.tags ?? [], difficulty: ex?.difficulty ?? null }
        }),
      )
      setFocusIdx(0)
      setDraftTags([])
      setDraftDifficulty(null)
    } else {
      setDraftSubs([])
      setDraftTags(d.tags)
      setDraftDifficulty(d.difficulty)
    }
  }

  const toggleSplit = useCallback(
    (on: boolean) => {
      splitOnRef.current = on
      if (!detail) return
      if (on && detail.split_eligible) {
        setSplitMode(true)
        setDraftSubs(detail.top_labels.map((l) => ({ label: l, tags: [], difficulty: null })))
        setFocusIdx(0)
        setDraftTags([])
        setDraftDifficulty(null)
      } else {
        setSplitMode(false)
        setDraftSubs([])
        setDraftTags(detail.tags)
        setDraftDifficulty(detail.difficulty)
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

  const toggleTag = useCallback(
    (id: string) => {
      if (splitMode) {
        setDraftSubs((prev) =>
          prev.map((s, i) =>
            i === focusIdx
              ? {
                  ...s,
                  tags: s.tags.includes(id) ? s.tags.filter((t) => t !== id) : [...s.tags, id],
                }
              : s,
          ),
        )
      } else {
        setDraftTags((prev) => (prev.includes(id) ? prev.filter((t) => t !== id) : [...prev, id]))
      }
    },
    [splitMode, focusIdx],
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

  const subsComplete =
    draftSubs.length > 0 && draftSubs.every((s) => s.tags.length > 0 && s.difficulty !== null)
  const canSave =
    !saving &&
    !!detail &&
    (splitMode ? subsComplete : draftTags.length > 0 && draftDifficulty !== null)

  // split 模式：焦点在最后一问时按钮才是「切分并保存」，否则是「下一问」导航
  const onLastSub = !splitMode || focusIdx >= draftSubs.length - 1

  const save = useCallback(async () => {
    if (!detail || saving) return
    if (splitMode) {
      if (!subsComplete) return
    } else if (!draftDifficulty || draftTags.length === 0) return
    setSaving(true)
    try {
      const body = splitMode
        ? {
            subs: draftSubs.map((s) => ({
              label: s.label,
              tags: s.tags,
              difficulty: s.difficulty as string,
            })),
          }
        : { tags: draftTags, difficulty: draftDifficulty as string }
      const result = await api.saveAnnotation(detail.qid, body)
      setQuestions((prev) =>
        prev.map((q) =>
          q.qid === result.qid
            ? {
                ...q,
                status: result.status,
                tags: result.tags,
                difficulty: result.difficulty,
                split: result.split ?? q.split ?? false,
                sub_labels: result.sub_labels ?? q.sub_labels,
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
      showToast({ kind: 'ok', text: `第 ${String(detail.meta.question_number)} 题已保存${warn}` })
      if (currentIndex < questions.length - 1) setCurrentIndex(currentIndex + 1)
    } catch (e) {
      showToast({ kind: 'err', text: `保存失败：${(e as Error).message}` })
    } finally {
      setSaving(false)
    }
  }, [detail, splitMode, subsComplete, draftSubs, draftDifficulty, draftTags, saving, paperId, questions, currentIndex, showToast])

  // 快捷键：←/→ 翻题（预览模式翻预览列表），1/2/3 难度（split 模式作用于焦点组），Enter 保存
  // （预览模式只读，难度/保存快捷键禁用）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      const typing = el && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)
      if (mode === 'preview') {
        if (e.key === 'ArrowLeft' && previewIndex > 0) setPreviewIndex(previewIndex - 1)
        else if (e.key === 'ArrowRight' && previewIndex < previewQuestions.length - 1)
          setPreviewIndex(previewIndex + 1)
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
  }, [mode, previewIndex, previewQuestions.length, gotoPrev, gotoNext, pickDifficulty, save, canSave])

  const currentPaper = papers.find((p) => p.id === paperId)

  // 预览模式：切分题当前选中问（联动中间题图与右侧标记）
  const previewSub =
    mode === 'preview' && previewDetail?.split_active && previewDetail.subs.length > 0
      ? previewDetail.subs[Math.min(previewSubIdx, previewDetail.subs.length - 1)]
      : null

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
                    onSubIdx={setPreviewSubIdx}
                    width={rightW}
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
                      本题无 a/b/c 级小问，开关暂不可用
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
                      {draftSubs.filter((s) => subState(s) === 'done').length}/{draftSubs.length} 完成
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
                />
              )}
              <div className="shrink-0 border-t border-gray-100 pt-3">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-700">
                    难度{splitMode && focusSub ? `（${focusSub.label} 问）` : ''}
                  </span>
                  <span className="text-xs font-normal text-gray-400">单选 · 必选（快捷键 1–5）</span>
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
                  disabled={splitMode && !onLastSub ? saving : !canSave}
                  onClick={() => {
                    if (splitMode && !onLastSub) setFocusIdx(focusIdx + 1)
                    else void save()
                  }}
                  className={`w-full rounded-md px-4 py-2 text-sm font-medium text-white transition-all duration-100 select-none ${
                    (splitMode && !onLastSub) || canSave
                      ? 'bg-blue-600 shadow-sm hover:bg-blue-700 active:scale-[0.97] active:bg-blue-800 active:shadow-none'
                      : 'cursor-not-allowed bg-gray-300'
                  }`}
                >
                  {saving
                    ? '保存中…'
                    : splitMode
                      ? onLastSub
                        ? `切分并保存（${draftSubs.filter((s) => subState(s) === 'done').length}/${draftSubs.length} 问完成）`
                        : '下一问'
                      : '确定并保存'}
                </button>
                <p className="text-center text-[11px] text-gray-400">
                  快捷键：←/→ 翻题 · Enter 保存
                </p>
              </div>
            </aside>
          </>
        )}
      </div>
    </div>
  )
}
