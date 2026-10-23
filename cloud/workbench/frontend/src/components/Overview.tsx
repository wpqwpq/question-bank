import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { OverviewPaper, OverviewResp, OverviewState } from '../api'
import { api, SESSION_LABELS } from '../api'

/**
 * 全局概览（管理员专享）：整个题库的「试卷地图」。
 *
 * 每份卷 = 一个 11px 小方块，按学科分区、区内按考试局分行，密密麻麻排开——
 * 像老迅雷的下载格子墙，一眼能看出哪块还没动。
 *
 * 三态（与后端 api.paper_progress 同源，**不要在这里另写判定**）：
 *   深绿 all  = 非有误题全部核对完成（与左侧卷列表那个 ✓ 完全同义）
 *   淡绿 some = 有 ≥1 个知识点标签，还没核完
 *   灰   none = 一个字都没标
 *
 * 数据一次拿全量（云端约 1200 卷，10 万题时约 7500 卷 / 1.3MB 未压缩、gzip 23KB）。
 * 前端不做虚拟滚动：7500 个格子的构建 + 布局约 20ms，等卷数破 3 万再考虑 canvas。
 */

interface Props {
  onExit: () => void
  /** 点格子里的「打开这卷」→ 回打标台并定位到该卷 */
  onOpen: (p: OverviewPaper) => void
  /**
   * 这卷能不能跳过去打开。默认全允许（管理员）。
   * 学科负责人（lead）能看到全库地图，但跳去别的学科拉题目后端会 404，
   * 所以由 App 传一个「是否在我学科范围内」的判定，越界的卷把按钮置灰。
   */
  canOpen?: (p: OverviewPaper) => boolean
}

type QualFilter = 'all' | 'al' | 'ig'
type Density = 'compact' | 'normal' | 'loose'

const SUBJECT_ORDER = [
  'physics',
  'chemistry',
  'biology',
  'mathematics',
  'economics',
  'accounting',
  'business',
]
const SUBJECT_ZH: Record<string, string> = {
  physics: '物理',
  chemistry: '化学',
  biology: '生物',
  mathematics: '数学',
  'further-mathematics': '进阶数学',
  economics: '经济',
  accounting: '会计',
  business: '商务',
}
/**
 * 分区归并：进阶数学算数学的一部分（2026-10-21）。
 * 只有 CIE 有 further-mathematics（9231，136 卷），合并后仍按考试局分行，
 * 同局同科时再拆成「CIE」＋「CIE 进阶」两行，避免 9709 与 9231 的格子混成一条。
 * 注意：只影响概览的分区与计数，p.subject 原值不动 —— tooltip、选中条照旧显示「进阶数学」。
 */
const CANON_SUBJECT: Record<string, string> = { 'further-mathematics': 'mathematics' }
const canonSubject = (s: string) => CANON_SUBJECT[s] ?? s
const isFurther = (s: string) => canonSubject(s) !== s
const BOARD_ORDER = ['edexcel', 'aqa', 'cie']
const BOARD_LABEL: Record<string, string> = { edexcel: 'Edexcel', aqa: 'AQA', cie: 'CIE' }
const SESSION_ORDER: Record<string, number> = {
  jan: 1,
  mar: 2,
  'may-jun': 3,
  june: 4,
  oct: 5,
  'oct-nov': 6,
}
const STATE_LABEL: Record<OverviewState, string> = {
  all: '审核通过',
  some: '已开工',
  none: '未开工',
}
const STATE_COLOR: Record<OverviewState, string> = {
  all: '#2f9e5c',
  some: '#a9dfba',
  none: '#e3e6ea',
}
const SZ: Record<Density, { cell: number; gap: number }> = {
  compact: { cell: 9, gap: 2 },
  normal: { cell: 11, gap: 2 },
  loose: { cell: 14, gap: 3 },
}

/** 资格归一：'a-level' 与 CIE 的目录名 'alevel' 视为同一档 */
const normQual = (q: string) => (q || '').replace(/-/g, '').toLowerCase()
const matchQual = (q: string, f: QualFilter) => {
  if (f === 'all') return true
  const n = normQual(q)
  if (f === 'al') return n === 'alevel' || n === 'gceadv'
  return n === 'igcse' || n === 'gcse'
}
/** 回打标台时 Sidebar 三个下拉只认这三个值 */
const qualForSelect = (q: string) => {
  const n = normQual(q)
  if (n === 'alevel' || n === 'gceadv') return 'a-level'
  if (n === 'igcse') return 'igcse'
  if (n === 'gcse') return 'gcse'
  return 'a-level'
}

interface BoardGroup {
  board: string
  /** 该行卷的原始学科（进阶数学与数学同局时会各占一行，靠它区分） */
  subject: string
  list: OverviewPaper[]
}
interface SubjectGroup {
  subject: string
  boards: BoardGroup[]
  total: number
  all: number
  some: number
  /** 其中属于进阶数学的卷数（并进来的那部分，用于标题补注） */
  further: number
}

export default function Overview({ onExit, onOpen, canOpen }: Props) {
  const allowOpen = canOpen ?? (() => true)
  const [data, setData] = useState<OverviewResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  const [qual, setQual] = useState<QualFilter>('all')
  const [filter, setFilter] = useState<'all' | OverviewState>('all')
  const [density, setDensity] = useState<Density>('normal')
  const [selected, setSelected] = useState<OverviewPaper | null>(null)

  const tipRef = useRef<HTMLDivElement>(null)
  const tipTitleRef = useRef<HTMLDivElement>(null)
  const tipSubRef = useRef<HTMLDivElement>(null)
  const tipDotRef = useRef<HTMLElement>(null)
  const tipTextRef = useRef<HTMLSpanElement>(null)

  const load = useCallback(async (force = false) => {
    setLoading(true)
    setErr(null)
    try {
      setData(await api.overview(force))
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 当前资格下的卷（全量统计与 KPI 都跟着这个变） */
  const papers = useMemo(
    () => (data?.papers ?? []).filter((p) => matchQual(p.qualification, qual)),
    [data, qual],
  )

  const stats = useMemo(() => {
    const s = { total: papers.length, all: 0, some: 0, none: 0 }
    for (const p of papers) s[p.state] += 1
    return s
  }, [papers])

  const byId = useMemo(() => {
    const m = new Map<string, OverviewPaper>()
    for (const p of papers) m.set(p.id, p)
    return m
  }, [papers])

  /** 学科（归并后）→ 考试局 → 卷（卷按年份、考季、卷号排，老的在前） */
  const groups = useMemo<SubjectGroup[]>(() => {
    // 行键 = 考试局 + 是否进阶：同局同科才拼成一排，进阶数学单独起一行
    const m = new Map<string, Map<string, BoardGroup>>()
    for (const p of papers) {
      const raw = p.subject || 'other'
      const sub = canonSubject(raw)
      let bm = m.get(sub)
      if (!bm) {
        bm = new Map()
        m.set(sub, bm)
      }
      const board = p.board || 'other'
      const key = `${board}|${isFurther(raw) ? 'f' : 'm'}`
      const row = bm.get(key)
      if (row) row.list.push(p)
      else bm.set(key, { board, subject: raw, list: [p] })
    }
    const out: SubjectGroup[] = []
    for (const [subject, bm] of m) {
      const boards: BoardGroup[] = [...bm.values()]
      for (const g of boards) {
        g.list.sort(
          (a, b) =>
            (a.year ?? 0) - (b.year ?? 0) ||
            (SESSION_ORDER[a.session ?? ''] ?? 9) - (SESSION_ORDER[b.session ?? ''] ?? 9) ||
            String(a.paper_code ?? '').localeCompare(String(b.paper_code ?? '')),
        )
      }
      boards.sort((a, b) => {
        const ia = BOARD_ORDER.indexOf(a.board)
        const ib = BOARD_ORDER.indexOf(b.board)
        const da = ia < 0 ? 99 : ia
        const db = ib < 0 ? 99 : ib
        if (da !== db) return da - db
        return (isFurther(a.subject) ? 1 : 0) - (isFurther(b.subject) ? 1 : 0) // 进阶排在同局数学之后
      })
      let all = 0
      let some = 0
      let total = 0
      let further = 0
      for (const g of boards) {
        for (const p of g.list) {
          total += 1
          if (isFurther(g.subject)) further += 1
          if (p.state === 'all') all += 1
          else if (p.state === 'some') some += 1
        }
      }
      out.push({ subject, boards, total, all, some, further })
    }
    out.sort((a, b) => {
      const ia = SUBJECT_ORDER.indexOf(a.subject)
      const ib = SUBJECT_ORDER.indexOf(b.subject)
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
    })
    return out
  }, [papers])

  // ── tooltip：直接改 DOM，不走 state ────────────────────────────
  // 理由：mousemove 每秒几十次，进 state 会让 7500 个格子跟着重渲染。
  const moveTip = (x: number, y: number) => {
    const t = tipRef.current
    if (!t) return
    const cx = Math.min(Math.max(x, 96), window.innerWidth - 96)
    t.style.left = `${cx}px`
    t.style.top = `${y}px`
    t.style.transform =
      y < 96 ? 'translate(-50%, 0) translateY(14px)' : 'translate(-50%, -100%) translateY(-8px)'
  }

  const showTip = (e: React.MouseEvent, p: OverviewPaper) => {
    const t = tipRef.current
    if (!t) return
    if (tipTitleRef.current) {
      tipTitleRef.current.textContent = `${p.paper_code ?? p.id} · ${p.year ?? ''} ${
        SESSION_LABELS[p.session ?? ''] ?? p.session ?? ''
      }`
    }
    if (tipSubRef.current) {
      tipSubRef.current.textContent = `${BOARD_LABEL[p.board] ?? p.board} · ${
        SUBJECT_ZH[p.subject] ?? p.subject
      }`
    }
    if (tipDotRef.current) tipDotRef.current.style.background = STATE_COLOR[p.state]
    if (tipTextRef.current) {
      tipTextRef.current.textContent =
        p.state === 'all'
          ? `${STATE_LABEL[p.state]}（${p.verified_count}/${p.question_count}）`
          : p.state === 'some'
            ? `已开工（已核对 ${p.verified_count}/${p.question_count}）`
            : '未开工'
    }
    t.style.opacity = '1'
    moveTip(e.clientX, e.clientY)
  }

  const hideTip = () => {
    const t = tipRef.current
    if (t) t.style.opacity = '0'
  }

  /** 鼠标在相邻格子之间移动时不要闪一下（离开的是 wall 才算真离开） */
  const onWallOut = (e: React.MouseEvent) => {
    const to = e.relatedTarget as HTMLElement | null
    if (to && to.closest('[data-pid]')) return
    hideTip()
  }

  /** wall 上的事件委托：7500 个格子不各挂一个监听 */
  const onWallOver = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest('[data-pid]') as HTMLElement | null
    if (!el) return
    const p = byId.get(el.dataset.pid || '')
    if (p) showTip(e, p)
  }
  const onWallMove = (e: React.MouseEvent) => {
    if (tipRef.current?.style.opacity === '1') moveTip(e.clientX, e.clientY)
  }
  const onWallClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest('[data-pid]') as HTMLElement | null
    if (!el) return
    const p = byId.get(el.dataset.pid || '')
    if (p) setSelected(p)
  }

  const cellStyle = { width: SZ[density].cell, height: SZ[density].cell }
  const wallStyle = { gap: SZ[density].gap }

  const pct = (n: number) => (stats.total ? Math.round((n / stats.total) * 1000) / 10 : 0)

  return (
    <div data-overview="1" className="flex h-screen flex-col bg-white text-gray-900">
      {/* ── 顶栏 ── */}
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-gray-200 px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={onExit}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[13px] text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-3.5 w-3.5"
            >
              <path d="m15 18-6-6 6-6" />
            </svg>
            返回打标台
          </button>
          <span className="h-5 w-px bg-gray-200" />
          <h1 className="text-[15px] font-medium whitespace-nowrap">
            全局概览
            <span className="ml-2 text-xs font-normal text-gray-400">
              {groups.length} 个学科 · {stats.total} 份试卷
            </span>
          </h1>
        </div>

        <div className="flex items-center gap-2">
          {/* 资格考试级 */}
          <select
            value={qual}
            onChange={(e) => {
              setQual(e.target.value as QualFilter)
              setSelected(null)
            }}
            title="按资格考试级过滤"
            className="h-8 rounded-md border border-gray-300 bg-white px-1.5 text-xs text-gray-700 focus:border-blue-500 focus:outline-none"
          >
            <option value="all">全部资格</option>
            <option value="al">AL · A-Level</option>
            <option value="ig">IG · IGCSE</option>
          </select>
          {/* 密度 */}
          <div className="flex rounded-lg border border-gray-300 bg-gray-100 p-0.5 text-xs">
            {(
              [
                ['compact', '紧凑'],
                ['normal', '标准'],
                ['loose', '宽松'],
              ] as const
            ).map(([v, label]) => (
              <button
                key={v}
                type="button"
                onClick={() => setDensity(v)}
                className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
                  density === v ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => void load(true)}
            disabled={loading}
            className="h-8 rounded-lg border border-gray-300 bg-white px-3 text-xs font-medium text-gray-700 transition-colors enabled:hover:bg-gray-50 disabled:opacity-50"
          >
            {loading ? '读取中…' : '刷新'}
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-auto">
        {err ? (
          <div className="p-6 text-sm text-red-700">
            读取失败：{err}
            <button
              type="button"
              onClick={() => void load(true)}
              className="ml-3 rounded-md border border-gray-300 px-2 py-1 text-xs text-gray-700 hover:bg-gray-50"
            >
              重试
            </button>
          </div>
        ) : loading && !data ? (
          <div className="p-6 text-sm text-gray-400">正在读取…</div>
        ) : (
          <div className="mx-auto max-w-[1240px] px-4 pb-14 pt-4">
            {/* ── KPI ── */}
            <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-4">
              {(
                [
                  ['total', '试卷总数', stats.total, ''],
                  ['all', '审核通过', stats.all, STATE_COLOR.all],
                  ['some', '已开工', stats.some, STATE_COLOR.some],
                  ['none', '未开工', stats.none, STATE_COLOR.none],
                ] as const
              ).map(([k, label, n, color]) => {
                const active = k === 'total' ? filter === 'all' : filter === k
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => {
                      if (k === 'total') setFilter('all')
                      else setFilter(filter === k ? 'all' : (k as OverviewState))
                    }}
                    className={`relative rounded-[11px] border bg-white px-4 py-3 text-left transition-all ${
                      active && k !== 'total'
                        ? 'border-gray-400 shadow-[0_0_0_3px_rgba(148,163,184,0.18)]'
                        : 'border-gray-200 hover:border-gray-300 hover:shadow-sm'
                    }`}
                  >
                    <div className="flex items-center gap-1.5 text-xs text-gray-500">
                      {color && (
                        <i
                          className="inline-block h-2 w-2 rounded-[2px]"
                          style={{ background: color }}
                        />
                      )}
                      {label}
                    </div>
                    <div className="mt-1 text-[23px] font-semibold leading-none tracking-tight">
                      {n}
                      <span className="ml-1 text-xs font-normal text-gray-400">份</span>
                    </div>
                    {k !== 'total' && (
                      <div className="absolute right-3.5 top-3.5 text-[11px] text-gray-400">
                        {pct(n)}%
                      </div>
                    )}
                  </button>
                )
              })}
            </div>

            {/* ── 总进度条 ── */}
            <div className="mt-3 rounded-[11px] border border-gray-200 bg-white px-4 py-3">
              <div className="mb-2 flex justify-between text-xs text-gray-500">
                <span>
                  整体进度 <b className="font-semibold text-gray-900">{pct(stats.all)}%</b> 审核通过
                </span>
                <span>
                  <b className="font-semibold text-gray-900">{stats.all}</b> / {stats.total} 份试卷
                </span>
              </div>
              <div className="flex h-2.5 overflow-hidden rounded-[5px] bg-gray-100">
                <span className="block h-full bg-[#2f9e5c]" style={{ width: `${pct(stats.all)}%` }} />
                <span className="block h-full bg-[#8fd2a5]" style={{ width: `${pct(stats.some)}%` }} />
              </div>
            </div>

            {/* ── 图例 ── */}
            <div className="mt-3.5 flex flex-wrap items-center gap-x-5 gap-y-1 px-0.5 text-xs text-gray-500">
              <span className="inline-flex items-center gap-1.5">
                <i className="inline-block h-[11px] w-[11px] rounded-[2px] bg-[#2f9e5c]" />
                审核通过
              </span>
              <span className="inline-flex items-center gap-1.5">
                <i className="inline-block h-[11px] w-[11px] rounded-[2px] bg-[#a9dfba]" />
                已开工
              </span>
              <span className="inline-flex items-center gap-1.5">
                <i className="inline-block h-[11px] w-[11px] rounded-[2px] bg-[#e3e6ea]" />
                未开工
              </span>
              <span className="ml-auto hidden text-gray-400 sm:inline">
                悬停看卷号 · 点击定位 · 上方卡片可筛选
              </span>
            </div>

            {/* ── 学科分区 ── */}
            {groups.length === 0 ? (
              <div className="mt-8 rounded-xl border border-dashed border-gray-300 p-10 text-center text-sm text-gray-400">
                当前资格下没有试卷
              </div>
            ) : (
              groups.map((g) => (
                <section
                  key={g.subject}
                  className="mt-3 rounded-xl border border-gray-200 bg-white px-[18px] pb-[17px] pt-[15px]"
                >
                  <div className="flex items-baseline gap-2.5 border-b border-gray-100 pb-2.5">
                    <span className="text-sm font-semibold">
                      {SUBJECT_ZH[g.subject] ?? g.subject}
                    </span>
                    <span className="text-[11px] tracking-wide text-gray-400">
                      {g.subject}
                    </span>
                    <span className="text-xs text-gray-500">{g.total} 份试卷</span>
                    {g.further > 0 && (
                      <span className="text-[11px] text-gray-400">含进阶 {g.further}</span>
                    )}
                    <div className="ml-auto flex items-center gap-2.5">
                      <span className="text-xs tabular-nums text-gray-500">
                        {g.all} 通过 · {Math.round((g.all / Math.max(1, g.total)) * 100)}%
                      </span>
                      <span className="flex h-[5px] w-[120px] overflow-hidden rounded-[3px] bg-gray-100">
                        <i
                          className="block h-full bg-[#2f9e5c]"
                          style={{ width: `${(g.all / Math.max(1, g.total)) * 100}%` }}
                        />
                        <i
                          className="block h-full bg-[#a9dfba]"
                          style={{ width: `${(g.some / Math.max(1, g.total)) * 100}%` }}
                        />
                      </span>
                    </div>
                  </div>

                  <div className="flex flex-col gap-2.5 pt-3">
                    {g.boards.map((b) => {
                      const ba = b.list.filter((x) => x.state === 'all').length
                      return (
                        <div key={`${b.board}|${b.subject}`} className="flex items-start gap-3">
                          <div className="w-[72px] shrink-0 pt-px text-right text-[10.5px] leading-[14px] text-gray-400">
                            <b className="block text-[10.5px] font-medium text-gray-500">
                              {BOARD_LABEL[b.board] ?? b.board}
                              {isFurther(b.subject) && (
                                <span className="ml-1 font-normal text-gray-400">进阶</span>
                              )}
                            </b>
                            {ba}/{b.list.length}
                          </div>
                          <div
                            className="flex min-w-0 flex-1 flex-wrap"
                            style={wallStyle}
                            onMouseOver={onWallOver}
                            onMouseMove={onWallMove}
                            onMouseOut={onWallOut}
                            onClick={onWallClick}
                          >
                            {b.list.map((p) => {
                              const dim = filter !== 'all' && p.state !== filter
                              const sel = selected?.id === p.id
                              return (
                                <button
                                  key={p.id}
                                  type="button"
                                  data-pid={p.id}
                                  aria-label={`${p.paper_code ?? p.id} ${p.year ?? ''} ${STATE_LABEL[p.state]}`}
                                  className={`block shrink-0 rounded-[2px] border-0 p-0 transition-[transform,box-shadow,opacity] duration-100 hover:relative hover:z-10 hover:scale-[1.45] hover:shadow-[0_0_0_1.5px_#111827] ${
                                    dim ? 'opacity-10' : ''
                                  } ${sel ? 'relative z-10 scale-[1.3] ring-2 ring-gray-900 ring-offset-1' : ''}`}
                                  style={{ ...cellStyle, background: STATE_COLOR[p.state] }}
                                />
                              )
                            })}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </section>
              ))
            )}

            <div className="mt-4 text-center text-[11.5px] leading-relaxed text-gray-400">
              数据生成于 {data?.generated_at ?? '—'} · 服务端缓存 60 秒 · 点「刷新」强制重算
            </div>
          </div>
        )}
      </div>

      {/* tooltip：位置与内容都由 ref 直接改，避免 7500 个格子跟着重渲染 */}
      <div
        ref={tipRef}
        className="pointer-events-none fixed left-0 top-0 z-50 rounded-lg bg-gray-900 px-2.5 py-2 text-[11.5px] leading-normal text-gray-50 opacity-0 shadow-lg transition-opacity duration-100"
      >
        <div ref={tipTitleRef} className="text-[12px] font-semibold tracking-wide" />
        <div ref={tipSubRef} className="text-[11px] text-gray-400" />
        <div className="mt-1 flex items-center gap-1.5">
          <i ref={tipDotRef} className="inline-block h-[7px] w-[7px] rounded-[1px]" />
          <span ref={tipTextRef} className="text-gray-300" />
        </div>
      </div>

      {/* 选中信息条 */}
      {selected && (
        <div className="fixed bottom-5 left-1/2 z-40 flex -translate-x-1/2 items-center gap-2.5 rounded-[10px] bg-gray-900 px-3.5 py-2.5 text-xs text-white shadow-xl">
          <span>
            <b className="font-semibold">{selected.paper_code ?? selected.id}</b>
            {' · '}
            {selected.year} {SESSION_LABELS[selected.session ?? ''] ?? selected.session}
            <span className="ml-1.5 text-gray-400">
              {SUBJECT_ZH[selected.subject] ?? selected.subject} /{' '}
              {BOARD_LABEL[selected.board] ?? selected.board} · {STATE_LABEL[selected.state]}
            </span>
          </span>
          <button
            type="button"
            onClick={() => {
              if (!allowOpen(selected)) return
              onOpen({ ...selected, qualification: qualForSelect(selected.qualification) })
            }}
            disabled={!allowOpen(selected)}
            title={allowOpen(selected) ? '回打标台并定位到这卷' : '这卷不在你的学科范围内'}
            className={`rounded-md px-2.5 py-1 text-[11.5px] font-medium ${
              allowOpen(selected)
                ? 'bg-white text-gray-900 hover:bg-gray-100'
                : 'cursor-not-allowed bg-white/20 text-gray-400'
            }`}
          >
            打开这卷
          </button>
          <button
            type="button"
            onClick={() => setSelected(null)}
            aria-label="关闭"
            className="px-0.5 text-[15px] leading-none text-gray-400 hover:text-gray-200"
          >
            ×
          </button>
        </div>
      )}
    </div>
  )
}
