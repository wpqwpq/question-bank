import { useCallback, useEffect, useMemo, useState } from 'react'
import type { TaxFile, TaxGroup, TaxTag, TaxTree, TaxUnit } from '../api'
import { api, asUnit, slug, unitKeys } from '../api'

/**
 * 标签管理页：直接维护 data/taxonomy/<board>/<qual>/<subject>.json。
 *
 * 本期只做「改」+「增」（2026-10-16 定）：不做删除、不做改 id。
 * 编辑模型：整页草稿 + 显式保存；词表只存 id，改中英文名不影响任何已打标题目。
 */

interface Props {
  initialBoard: string
  initialQual: string
  initialSubject: string
  /** 打开时直接定位到的单元（如打标台当前卷的 U1 / P1P2）；不在该词表里则回落到第一个单元 */
  initialUnit?: string
  onExit: () => void
}

interface Sel {
  board: string
  qual: string
  subject: string
}

/** 待确认的新增标签（确认后才写进草稿） */
interface PendingTag {
  gid: string
  zh: string
  en: string
}
/** 待确认的新增分组 */
interface PendingGroup {
  zh: string
  en: string
}
type Ask = null | { kind: 'exit' } | { kind: 'switch'; next: Sel }

const GRID = 'grid grid-cols-[26px_minmax(0,1fr)_minmax(0,1fr)_236px_132px] items-center gap-2 px-2'
const FIELD =
  'w-full min-w-0 rounded-md border border-transparent bg-transparent px-1.5 py-1 text-[13px] text-gray-900 outline-none transition-colors hover:border-gray-200 hover:bg-white focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-100'
const GRID_ADD =
  'mt-0.5 w-full rounded-[10px] border border-dashed border-gray-300 px-3 py-2.5 text-[12.5px] text-gray-500 transition-colors hover:border-blue-400 hover:bg-blue-50 hover:text-blue-600'

const sUp = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m6 15 6-6 6 6" />
  </svg>
)
const sDown = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m6 9 6 6 6-6" />
  </svg>
)
const sLock = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="4" y="11" width="16" height="10" rx="2" />
    <path d="M8 11V7a4 4 0 0 1 8 0v4" />
  </svg>
)

const UNIT_RE = /^(?:U[1-9]|P[1-9](?:P[1-9])?)$/

/** 词表维度下拉（考试局 / 资格 / 学科） */
function Picker({
  label,
  opts,
  value,
  onChange,
}: {
  label: string
  opts: string[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <label className="relative block pt-1.5">
      <span className="absolute top-0 left-2 bg-gray-50 px-0.5 text-[10px] text-gray-400">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-[30px] rounded-md border border-gray-300 bg-white px-2 text-[12.5px] text-gray-800 focus:border-blue-500 focus:outline-none"
      >
        {opts.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  )
}

/** 与后端 taxonomy.validate_taxonomy 同规则的本地校验：先在前端把错误指到行 */
function clientValidate(file: TaxFile): string[] {
  const errs: string[] = []
  const units = unitKeys(file)
  if (units.length === 0) return ['词表至少要有一个单元']
  const tagOwner = new Map<string, string>()
  const groupOwner = new Map<string, string>()

  for (const u of units) {
    if (!UNIT_RE.test(u)) errs.push(`单元键不合法：${u}（应形如 U1 或 P1P2）`)
    const entry = asUnit(file, u)
    if (!entry || !Array.isArray(entry.groups) || !Array.isArray(entry.tags)) {
      errs.push(`${u}：结构异常（groups / tags 必须是数组）`)
      continue
    }

    const local = new Set<string>()
    entry.groups.forEach((g, i) => {
      const gid = (g.id ?? '').trim()
      if (!gid) {
        errs.push(`${u} 第 ${i + 1} 个分组：id 不能为空`)
        return
      }
      if (groupOwner.has(gid)) errs.push(`${u} 分组 ${gid}：id 与 ${groupOwner.get(gid)} 里的分组重复`)
      groupOwner.set(gid, u)
      local.add(gid)
      if (!(g.zh ?? '').trim()) errs.push(`${u}/${gid}：分组中文名不能为空`)
      if (!(g.en ?? '').trim()) errs.push(`${u}/${gid}：分组英文名不能为空（用于生成 id）`)
    })

    const used = new Map<string, number>()
    local.forEach((g) => used.set(g, 0))

    entry.tags.forEach((t, i) => {
      const tid = (t.id ?? '').trim()
      const where = tid || `${u} 第 ${i + 1} 个标签`
      if (!tid) errs.push(`${where}：id 不能为空`)
      else if (tagOwner.has(tid)) errs.push(`${where}：id 与 ${tagOwner.get(tid)} 里的标签重复`)
      else tagOwner.set(tid, u)
      if (!(t.zh ?? '').trim()) errs.push(`${where}：中文名不能为空`)
      if (!(t.en ?? '').trim()) errs.push(`${where}：英文名不能为空（用于生成 id）`)
      const gid = (t.group ?? '').trim()
      if (!gid) errs.push(`${where}：必须指定所属分组`)
      else if (!local.has(gid)) errs.push(`${where}：所属分组 ${gid} 在本单元里不存在`)
      else used.set(gid, (used.get(gid) ?? 0) + 1)
    })

    local.forEach((g) => {
      if (!used.get(g)) errs.push(`${u}/${g}：分组下没有任何标签（不留空组头）`)
    })
  }
  return errs
}

export default function TagAdmin({ initialBoard, initialQual, initialSubject, initialUnit, onExit }: Props) {
  const [tree, setTree] = useState<TaxTree | null>(null)
  const [sel, setSel] = useState<Sel>({ board: initialBoard, qual: initialQual, subject: initialSubject })
  const [file, setFile] = useState<TaxFile | null>(null)
  const [filePath, setFilePath] = useState('')
  const [baseline, setBaseline] = useState('')
  const [unit, setUnit] = useState(initialUnit ?? '')
  const [folded, setFolded] = useState<Set<string>>(new Set())
  const [query, setQuery] = useState('')
  const [pendingTag, setPendingTag] = useState<PendingTag | null>(null)
  const [pendingGroup, setPendingGroup] = useState<PendingGroup | null>(null)
  const [pendingErr, setPendingErr] = useState('')
  const [errors, setErrors] = useState<string[]>([])
  const [showNotice, setShowNotice] = useState(true)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [ask, setAsk] = useState<Ask>(null)

  // 待确认的新增行只要填了字，也算「未保存」——否则点返回会静默丢掉刚敲的内容
  const pendingHasText =
    (!!pendingTag && (pendingTag.zh.trim() !== '' || pendingTag.en.trim() !== '')) ||
    (!!pendingGroup && (pendingGroup.zh.trim() !== '' || pendingGroup.en.trim() !== ''))
  const dirty = !!file && (JSON.stringify(file) !== baseline || pendingHasText)

  const showToast = useCallback((kind: 'ok' | 'err', text: string) => {
    setToast({ kind, text })
    window.setTimeout(() => setToast((t) => (t && t.text === text ? null : t)), 3000)
  }, [])

  // ── 词表目录树 ──
  useEffect(() => {
    api
      .taxonomyTree()
      .then(setTree)
      .catch((e: Error) => showToast('err', `加载词表清单失败：${e.message}`))
  }, [showToast])

  // 初始维度若不在目录树里（例如资格选了 IGCSE），收敛到第一个可用组合
  useEffect(() => {
    if (!tree) return
    const boards = Object.keys(tree)
    if (boards.length === 0) return
    const b = tree[sel.board] ? sel.board : boards[0]
    const quals = Object.keys(tree[b] ?? {})
    const q = quals.includes(sel.qual) ? sel.qual : (quals[0] ?? '')
    const subs = tree[b]?.[q] ?? []
    const s = subs.includes(sel.subject) ? sel.subject : (subs[0] ?? '')
    if (b !== sel.board || q !== sel.qual || s !== sel.subject) setSel({ board: b, qual: q, subject: s })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tree])

  const load = useCallback(
    async (s: Sel) => {
      setLoading(true)
      try {
        const r = await api.taxonomyFile(s.board, s.qual, s.subject)
        setFile(r.data)
        setFilePath(r.path)
        setBaseline(JSON.stringify(r.data))
        const us = unitKeys(r.data)
        setUnit((prev) => (us.includes(prev) ? prev : (us[0] ?? '')))
        setFolded(new Set())
        setPendingTag(null)
        setPendingGroup(null)
        setPendingErr('')
        setErrors([])
      } catch (e) {
        setFile(null)
        showToast('err', `加载词表失败：${(e as Error).message}`)
      } finally {
        setLoading(false)
      }
    },
    [showToast],
  )

  useEffect(() => {
    if (!tree) return
    if (!sel.board || !sel.qual || !sel.subject) return
    void load(sel)
  }, [tree, sel, load])

  // ── 草稿编辑 ──
  const patchUnit = useCallback(
    (fn: (u: TaxUnit) => TaxUnit) => {
      setFile((prev) => (prev ? { ...prev, [unit]: fn(asUnit(prev, unit)) } : prev))
    },
    [unit],
  )

  const setGroupField = useCallback(
    (gi: number, key: keyof TaxGroup, value: string) =>
      patchUnit((u) => ({ ...u, groups: u.groups.map((g, i) => (i === gi ? { ...g, [key]: value } : g)) })),
    [patchUnit],
  )
  const setTagField = useCallback(
    (ti: number, key: keyof TaxTag, value: string) =>
      patchUnit((u) => ({ ...u, tags: u.tags.map((t, i) => (i === ti ? { ...t, [key]: value } : t)) })),
    [patchUnit],
  )

  const moveTag = useCallback(
    (ti: number, dir: -1 | 1) =>
      patchUnit((u) => {
        const tags = [...u.tags]
        const gid = tags[ti].group
        let j = ti + dir
        while (j >= 0 && j < tags.length && tags[j].group !== gid) j += dir
        if (j < 0 || j >= tags.length) return u
        ;[tags[ti], tags[j]] = [tags[j], tags[ti]]
        return { ...u, tags }
      }),
    [patchUnit],
  )

  const moveGroup = useCallback(
    (gi: number, dir: -1 | 1) =>
      patchUnit((u) => {
        const gs = [...u.groups]
        const j = gi + dir
        if (j < 0 || j >= gs.length) return u
        ;[gs[gi], gs[j]] = [gs[j], gs[gi]]
        return { ...u, groups: gs }
      }),
    [patchUnit],
  )

  // ── id 占用表（新增时查重）──
  const taken = useMemo(() => {
    const tags = new Set<string>()
    const groups = new Set<string>()
    if (!file) return { tags, groups }
    for (const u of unitKeys(file)) {
      const entry = asUnit(file, u)
      entry.groups.forEach((g) => g.id && groups.add(g.id))
      entry.tags.forEach((t) => t.id && tags.add(t.id))
    }
    return { tags, groups }
  }, [file])

  const unitLower = unit.toLowerCase()
  const pendingTagId = pendingTag && slug(pendingTag.en) ? `${unitLower}-${slug(pendingTag.en)}` : ''
  const pendingGroupId = pendingGroup && slug(pendingGroup.en) ? `${unitLower}g-${slug(pendingGroup.en)}` : ''

  function commitPendingTag() {
    if (!pendingTag) return
    const zh = pendingTag.zh.trim()
    const en = pendingTag.en.trim()
    if (!zh) return setPendingErr('中文名不能为空')
    if (!en) return setPendingErr('英文名不能为空 —— id 由英文名生成')
    const id = `${unitLower}-${slug(en)}`
    if (!id || id === unitLower + '-') return setPendingErr('英文名里至少要有一个字母或数字')
    if (taken.tags.has(id)) return setPendingErr(`算出的 id ${id} 已存在，换一个英文名`)
    const gid = pendingTag.gid
    patchUnit((u) => ({ ...u, tags: [...u.tags, { id, zh, en, group: gid }] }))
    setPendingTag(null)
    setPendingErr('')
  }

  function commitPendingGroup() {
    if (!pendingGroup) return
    const zh = pendingGroup.zh.trim()
    const en = pendingGroup.en.trim()
    if (!zh) return setPendingErr('分组中文名不能为空')
    if (!en) return setPendingErr('分组英文名不能为空 —— id 由英文名生成')
    const id = `${unitLower}g-${slug(en)}`
    if (!slug(en)) return setPendingErr('英文名里至少要有一个字母或数字')
    if (taken.groups.has(id)) return setPendingErr(`算出的 id ${id} 已存在，换一个英文名`)
    patchUnit((u) => ({ ...u, groups: [...u.groups, { id, zh, en }] }))
    setPendingGroup(null)
    setPendingErr('')
  }

  // ── 保存 ──
  const save = useCallback(async (): Promise<boolean> => {
    if (!file || saving) return false
    if (pendingHasText) {
      showToast('err', '有一行新增还没确认（点行尾的 ✓），先确认或取消再保存')
      return false
    }
    const errs = clientValidate(file)
    if (errs.length) {
      setErrors(errs)
      showToast('err', `保存被拒绝，共 ${errs.length} 处需要修正`)
      return false
    }
    setSaving(true)
    try {
      const r = await api.saveTaxonomy(sel.board, sel.qual, sel.subject, file)
      setBaseline(JSON.stringify(file))
      setErrors([])
      showToast('ok', `已保存 ${r.tags} 个标签 · 打标台右栏已刷新`)
      return true
    } catch (e) {
      const msg = (e as Error).message
      setErrors(msg.split('\n').filter(Boolean))
      showToast('err', '保存失败')
      return false
    } finally {
      setSaving(false)
    }
  }, [file, saving, sel, showToast, pendingHasText])

  // ── 下拉 ──
  const boards = tree ? Object.keys(tree) : []
  const quals = tree && tree[sel.board] ? Object.keys(tree[sel.board]) : []
  const subjects = tree && tree[sel.board] ? (tree[sel.board][sel.qual] ?? []) : []

  function requestSel(next: Sel) {
    if (next.board === sel.board && next.qual === sel.qual && next.subject === sel.subject) return
    if (dirty) setAsk({ kind: 'switch', next })
    else setSel(next)
  }
  function onBoard(b: string) {
    const q = Object.keys(tree?.[b] ?? {})[0] ?? ''
    const s = tree?.[b]?.[q]?.[0] ?? ''
    requestSel({ board: b, qual: q, subject: s })
  }
  function onQual(q: string) {
    const s = tree?.[sel.board]?.[q]?.[0] ?? ''
    requestSel({ board: sel.board, qual: q, subject: s })
  }
  function onSubject(s: string) {
    requestSel({ board: sel.board, qual: sel.qual, subject: s })
  }

  function requestExit() {
    if (dirty) setAsk({ kind: 'exit' })
    else onExit()
  }

  // ── 渲染数据 ──
  const units = file ? unitKeys(file) : []
  const cur = file && unit ? asUnit(file, unit) : null
  const q = query.trim().toLowerCase()
  const md = (s: string) => s.toLowerCase().includes(q)
  const groupHit = (g: TaxGroup) => !!q && (md(g.zh) || md(g.en) || md(g.id))
  const tagHit = (t: TaxTag) => !q || md(t.zh) || md(t.en) || md(t.id)

  const sections = useMemo(() => {
    if (!cur) return []
    return cur.groups.map((g, gi) => {
      const hit = groupHit(g)
      const items = cur.tags
        .map((t, ti) => ({ t, ti }))
        .filter((x) => x.t.group === g.id && (hit || tagHit(x.t)))
      return { g, gi, items, show: !q || hit || items.length > 0 }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur, q])

  const errHead = errors.slice(0, 5)

  return (
    <div data-tagadmin="1" className="flex h-screen flex-col bg-white text-gray-900">
      {/* 顶栏 */}
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-gray-200 px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={requestExit}
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[13px] text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-3.5 w-3.5">
              <path d="m15 18-6-6 6-6" />
            </svg>
            返回打标台
          </button>
          <span className="h-5 w-px bg-gray-200" />
          <h1 className="text-[15px] font-medium whitespace-nowrap">
            标签管理
            <span className="ml-2 font-mono text-xs font-normal text-gray-400">{filePath}</span>
          </h1>
        </div>
        <div className="flex items-center gap-2.5">
          {dirty ? (
            <span
              data-ta-dirty="1"
              className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 text-xs text-amber-800"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-amber-600" />
              有未保存的修改
            </span>
          ) : (
            <span className="text-xs text-gray-400">已保存</span>
          )}
          <button
            type="button"
            data-ta-save="1"
            disabled={!dirty || saving}
            onClick={() => void save()}
            className={`h-8 rounded-lg px-4 text-[13px] font-medium transition-colors ${
              dirty && !saving ? 'bg-blue-600 text-white hover:bg-blue-700' : 'cursor-not-allowed bg-gray-300 text-white'
            }`}
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </div>
      </header>

      {/* 工具条 */}
      <div className="flex shrink-0 items-center justify-between gap-4 border-b border-gray-200 bg-gray-50 px-4 py-2">
        <div className="flex items-center gap-2">
          <Picker label="考试局" opts={boards} value={sel.board} onChange={onBoard} />
          <Picker label="资格" opts={quals} value={sel.qual} onChange={onQual} />
          <Picker label="学科" opts={subjects} value={sel.subject} onChange={onSubject} />
        </div>
        <div className="flex h-[30px] w-60 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-3.5 w-3.5 shrink-0 text-gray-400">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索中文 / 英文 / id"
            className="w-full text-[12.5px] outline-none"
          />
        </div>
      </div>

      {showNotice && (
        <div className="flex shrink-0 items-center gap-2 border-b border-blue-100 bg-blue-50 px-4 py-2 text-[12.5px] text-blue-800">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-3.5 w-3.5 shrink-0">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8h.01M12 12v4" />
          </svg>
          改中英文名<b>不会动任何已打标题目</b>——题目里只存 id。id 生成后即冻结，只读。
          <button type="button" onClick={() => setShowNotice(false)} className="ml-auto text-blue-300 hover:text-blue-600">
            ✕
          </button>
        </div>
      )}

      {errors.length > 0 && (
        <div className="shrink-0 border-b border-red-100 bg-red-50 px-4 py-2 text-[12.5px] text-red-800">
          保存被拒绝，共 {errors.length} 处需要修正：
          <ul className="mt-1 ml-4 list-disc space-y-0.5">
            {errHead.map((e) => (
              <li key={e}>{e}</li>
            ))}
            {errors.length > errHead.length && <li>…还有 {errors.length - errHead.length} 条</li>}
          </ul>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* 左：单元 */}
        <aside className="flex w-[216px] shrink-0 flex-col overflow-y-auto border-r border-gray-200 bg-gray-50 px-2 py-2.5">
          <p className="px-2 pb-1.5 text-[11px] font-medium tracking-wide text-gray-400">单元</p>
          {units.map((u) => {
            const e = asUnit(file as TaxFile, u)
            const on = u === unit
            return (
              <button
                key={u}
                type="button"
                data-ta-unit={u}
                onClick={() => setUnit(u)}
                className={`flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
                  on ? 'bg-blue-50 text-blue-700 shadow-[inset_2px_0_0_#2563eb]' : 'text-gray-700 hover:bg-white'
                }`}
              >
                <span className={`text-[13px] ${on ? 'font-semibold' : 'font-medium'}`}>{u}</span>
                <span className={`shrink-0 text-[10.5px] ${on ? 'text-[#5b7cc4]' : 'text-gray-400'}`}>
                  {e.groups.length} 组 · {e.tags.length} 标签
                </span>
              </button>
            )
          })}
          <div className="mt-auto border-t border-gray-200 px-2 pt-2.5">
            <p className="font-mono text-[11px] break-all text-gray-500">{filePath}</p>
            <p className="mt-1 text-[11px] text-gray-400">
              {units.length} 个单元 ·{' '}
              {units.reduce((n, u) => n + asUnit(file as TaxFile, u).tags.length, 0)} 个标签
            </p>
          </div>
        </aside>

        {/* 右：主区 */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-[46px] shrink-0 items-center justify-between border-b border-gray-200 px-3.5">
            <span className="text-[13.5px] font-semibold">
              {unit}
              {cur && (
                <em className="ml-2 text-xs font-normal text-gray-400 not-italic">
                  {cur.groups.length} 组 · {cur.tags.length} 个标签
                </em>
              )}
            </span>
            <span className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setFolded(new Set())}
                className="rounded-md px-1.5 py-1 text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-900"
              >
                全部展开
              </button>
              <button
                type="button"
                onClick={() => setFolded(new Set((cur?.groups ?? []).map((g) => g.id)))}
                className="rounded-md px-1.5 py-1 text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-900"
              >
                全部收起
              </button>
            </span>
          </div>

          <div data-ta-list="1" className="min-h-0 flex-1 overflow-y-auto px-3.5 py-3">
            {loading && !file && <p className="py-16 text-center text-[13px] text-gray-400">加载中…</p>}
            {!loading && !file && <p className="py-16 text-center text-[13px] text-gray-400">没有可编辑的词表</p>}

            {cur &&
              sections.map(
                ({ g, gi, items, show }) =>
                  show && (
                    <div
                      key={g.id}
                      data-ta-group={g.id}
                      className="mb-2.5 overflow-hidden rounded-[10px] border border-gray-200 transition-colors hover:border-gray-300"
                    >
                      {/* 组头 */}
                      <div className={`${GRID} group h-[42px] bg-gray-50`}>
                        <button
                          type="button"
                          title="收起 / 展开"
                          onClick={() =>
                            setFolded((prev) => {
                              const next = new Set(prev)
                              if (next.has(g.id)) next.delete(g.id)
                              else next.add(g.id)
                              return next
                            })
                          }
                          className="grid h-5 w-5 place-items-center rounded text-gray-400 hover:bg-gray-200 hover:text-gray-700"
                        >
                          <svg
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2.4"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className={`h-3 w-3 transition-transform ${folded.has(g.id) && !q ? '-rotate-90' : ''}`}
                          >
                            <path d="m6 9 6 6 6-6" />
                          </svg>
                        </button>
                        <input
                          value={g.zh}
                          onChange={(e) => setGroupField(gi, 'zh', e.target.value)}
                          spellCheck={false}
                          className={`${FIELD} font-semibold`}
                        />
                        <input
                          value={g.en}
                          onChange={(e) => setGroupField(gi, 'en', e.target.value)}
                          spellCheck={false}
                          className={`${FIELD} text-[12.5px] font-medium text-gray-500`}
                        />
                        <span
                          title="id 已冻结，不可修改"
                          className="flex min-w-0 items-center gap-1 pl-1 font-mono text-[11px] text-gray-400"
                        >
                          <span className="h-2.5 w-2.5 shrink-0 text-gray-300">{sLock}</span>
                          <span className="truncate">{g.id}</span>
                        </span>
                        <span className="flex items-center justify-end gap-0.5 opacity-45 transition-opacity group-hover:opacity-100">
                          <button
                            type="button"
                            title="分组上移"
                            disabled={gi === 0}
                            onClick={() => moveGroup(gi, -1)}
                            className="grid h-6 w-6 place-items-center rounded-md text-gray-500 hover:border hover:border-gray-200 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-25 disabled:hover:border-transparent disabled:hover:bg-transparent"
                          >
                            <span className="h-3.5 w-3.5">{sUp}</span>
                          </button>
                          <button
                            type="button"
                            title="分组下移"
                            disabled={gi === (cur.groups.length ?? 0) - 1}
                            onClick={() => moveGroup(gi, 1)}
                            className="grid h-6 w-6 place-items-center rounded-md text-gray-500 hover:border hover:border-gray-200 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-25 disabled:hover:border-transparent disabled:hover:bg-transparent"
                          >
                            <span className="h-3.5 w-3.5">{sDown}</span>
                          </button>
                          <button
                            type="button"
                            disabled={!g.id}
                            title={g.id ? '在本组末尾新增一个标签' : '先填分组英文名（id 由它生成）'}
                            onClick={() => {
                              setPendingGroup(null)
                              setPendingErr('')
                              setPendingTag({ gid: g.id, zh: '', en: '' })
                            }}
                            className="rounded-md px-1.5 py-0.5 text-[11.5px] font-medium text-blue-600 hover:bg-blue-50 disabled:cursor-not-allowed disabled:text-gray-300 disabled:hover:bg-transparent"
                          >
                            + 标签
                          </button>
                        </span>
                      </div>

                      {/* 标签行 */}
                      {!(folded.has(g.id) && !q) &&
                        items.map(({ t, ti }) => {
                          const first = items[0]?.ti === ti
                          const last = items[items.length - 1]?.ti === ti
                          return (
                            <div
                              key={t.id || `new-${ti}`}
                              data-ta-tag={t.id}
                              className={`${GRID} group h-9 border-t border-gray-100 hover:bg-[#fcfdff]`}
                            >
                              <span />
                              <input
                                value={t.zh}
                                onChange={(e) => setTagField(ti, 'zh', e.target.value)}
                                spellCheck={false}
                                className={FIELD}
                              />
                              <input
                                value={t.en}
                                onChange={(e) => setTagField(ti, 'en', e.target.value)}
                                spellCheck={false}
                                className={`${FIELD} text-[12.5px] text-gray-500`}
                              />
                              <span
                                title="id 已冻结，不可修改"
                                className="flex min-w-0 items-center gap-1 pl-1 font-mono text-[11px] text-gray-400"
                              >
                                <span className="h-2.5 w-2.5 shrink-0 text-gray-300">{sLock}</span>
                                <span className="truncate">{t.id}</span>
                              </span>
                              <span className="flex items-center justify-end gap-0.5 opacity-45 transition-opacity group-hover:opacity-100">
                                <button
                                  type="button"
                                  title="上移"
                                  disabled={first}
                                  onClick={() => moveTag(ti, -1)}
                                  className="grid h-6 w-6 place-items-center rounded-md text-gray-500 hover:border hover:border-gray-200 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-25 disabled:hover:border-transparent disabled:hover:bg-transparent"
                                >
                                  <span className="h-3.5 w-3.5">{sUp}</span>
                                </button>
                                <button
                                  type="button"
                                  title="下移"
                                  disabled={last}
                                  onClick={() => moveTag(ti, 1)}
                                  className="grid h-6 w-6 place-items-center rounded-md text-gray-500 hover:border hover:border-gray-200 hover:bg-gray-100 hover:text-gray-900 disabled:opacity-25 disabled:hover:border-transparent disabled:hover:bg-transparent"
                                >
                                  <span className="h-3.5 w-3.5">{sDown}</span>
                                </button>
                              </span>
                            </div>
                          )
                        })}

                      {/* 待确认的新增标签 */}
                      {pendingTag && pendingTag.gid === g.id && (
                        <>
                          <div
                            data-ta-newtag="1"
                            className={`${GRID} h-9 border-t border-blue-100 bg-blue-50 shadow-[inset_2px_0_0_#2563eb]`}
                          >
                            <span />
                            <input
                              autoFocus
                              value={pendingTag.zh}
                              placeholder="中文名（必填）"
                              spellCheck={false}
                              onChange={(e) => {
                                setPendingErr('')
                                setPendingTag({ ...pendingTag, zh: e.target.value })
                              }}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') commitPendingTag()
                                else if (e.key === 'Escape') {
                                  setPendingTag(null)
                                  setPendingErr('')
                                }
                              }}
                              className={`${FIELD} bg-white`}
                            />
                            <input
                              value={pendingTag.en}
                              placeholder="英文名（必填，用于生成 id）"
                              spellCheck={false}
                              onChange={(e) => {
                                setPendingErr('')
                                setPendingTag({ ...pendingTag, en: e.target.value })
                              }}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') commitPendingTag()
                                else if (e.key === 'Escape') {
                                  setPendingTag(null)
                                  setPendingErr('')
                                }
                              }}
                              className={`${FIELD} bg-white text-[12.5px] text-gray-500`}
                            />
                            <span className="flex min-w-0 items-center gap-1 pl-1 font-mono text-[11px] text-gray-400">
                              →
                              <span className="truncate font-medium text-gray-600">{pendingTagId || '—'}</span>
                            </span>
                            <span className="flex items-center justify-end gap-0.5">
                              <button
                                type="button"
                                title="确认添加"
                                onClick={commitPendingTag}
                                className="grid h-6 w-6 place-items-center rounded-md text-blue-600 hover:bg-blue-100"
                              >
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="h-3 w-3">
                                  <path d="M20 6 9 17l-5-5" />
                                </svg>
                              </button>
                              <button
                                type="button"
                                title="取消这一行"
                                onClick={() => {
                                  setPendingTag(null)
                                  setPendingErr('')
                                }}
                                className="grid h-6 w-6 place-items-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700"
                              >
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-3 w-3">
                                  <path d="M18 6 6 18M6 6l12 12" />
                                </svg>
                              </button>
                            </span>
                          </div>
                          {pendingErr && (
                            <div className="border-t border-dashed border-red-200 bg-red-50 px-3 py-1.5 text-[11px] text-red-700">
                              {pendingErr}
                            </div>
                          )}
                        </>
                      )}

                      {!(folded.has(g.id) && !q) && (
                        <div className="border-t border-dashed border-gray-200 py-1 pr-2 pl-[26px]">
                          <button
                            type="button"
                            disabled={!g.id}
                            title={g.id ? '在本组末尾新增一个标签' : '先填分组英文名（id 由它生成）'}
                            onClick={() => {
                              setPendingGroup(null)
                              setPendingErr('')
                              setPendingTag({ gid: g.id, zh: '', en: '' })
                            }}
                            className="rounded-md px-1.5 py-0.5 text-xs text-blue-600 hover:bg-blue-50 disabled:cursor-not-allowed disabled:text-gray-300 disabled:hover:bg-transparent"
                          >
                            + 标签
                          </button>
                        </div>
                      )}
                    </div>
                  ),
              )}

            {/* 待确认的新增分组 */}
            {cur && pendingGroup && (
              <div
                data-ta-newgroup="1"
                className="mb-2.5 overflow-hidden rounded-[10px] border border-blue-300 bg-white"
              >
                <div className={`${GRID} h-[42px] bg-blue-50`}>
                  <span />
                  <input
                    autoFocus
                    value={pendingGroup.zh}
                    placeholder="分组中文名（必填）"
                    spellCheck={false}
                    onChange={(e) => {
                      setPendingErr('')
                      setPendingGroup({ ...pendingGroup, zh: e.target.value })
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitPendingGroup()
                      else if (e.key === 'Escape') {
                        setPendingGroup(null)
                        setPendingErr('')
                      }
                    }}
                    className={`${FIELD} bg-white font-semibold`}
                  />
                  <input
                    value={pendingGroup.en}
                    placeholder="英文名（必填，用于生成 id）"
                    spellCheck={false}
                    onChange={(e) => {
                      setPendingErr('')
                      setPendingGroup({ ...pendingGroup, en: e.target.value })
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitPendingGroup()
                      else if (e.key === 'Escape') {
                        setPendingGroup(null)
                        setPendingErr('')
                      }
                    }}
                    className={`${FIELD} bg-white text-[12.5px] font-medium text-gray-500`}
                  />
                  <span className="flex min-w-0 items-center gap-1 pl-1 font-mono text-[11px] text-gray-400">
                    →
                    <span className="truncate font-medium text-gray-600">{pendingGroupId || '—'}</span>
                  </span>
                  <span className="flex items-center justify-end gap-0.5">
                    <button
                      type="button"
                      title="确认新增分组"
                      onClick={commitPendingGroup}
                      className="grid h-6 w-6 place-items-center rounded-md text-blue-600 hover:bg-blue-100"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" className="h-3 w-3">
                        <path d="M20 6 9 17l-5-5" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      title="取消"
                      onClick={() => {
                        setPendingGroup(null)
                        setPendingErr('')
                      }}
                      className="grid h-6 w-6 place-items-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700"
                    >
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-3 w-3">
                        <path d="M18 6 6 18M6 6l12 12" />
                      </svg>
                    </button>
                  </span>
                </div>
                {pendingErr && (
                  <div className="border-t border-dashed border-red-200 bg-red-50 px-3 py-1.5 text-[11px] text-red-700">
                    {pendingErr}
                  </div>
                )}
              </div>
            )}

            {cur && !pendingGroup && (
              <button
                type="button"
                data-ta-addgroup="1"
                className={GRID_ADD}
                onClick={() => {
                  setPendingTag(null)
                  setPendingErr('')
                  setPendingGroup({ zh: '', en: '' })
                }}
              >
                + 新增分组
              </button>
            )}

            {cur && q && sections.every((s) => !s.show) && (
              <p className="py-3 text-center text-xs text-gray-400">无匹配标签</p>
            )}
          </div>
        </div>
      </div>

      {/* toast */}
      {toast && (
        <div
          className={`fixed top-16 right-4 z-30 rounded-lg px-3.5 py-2 text-[12.5px] text-white shadow-lg ${
            toast.kind === 'ok' ? 'bg-green-900' : 'bg-gray-800'
          }`}
        >
          {toast.text}
        </div>
      )}

      {/* 未保存离开确认 */}
      {ask && (
        <div className="fixed inset-0 z-40 grid place-items-center bg-gray-900/40">
          <div className="w-[400px] rounded-xl bg-white p-[18px] shadow-2xl">
            <h4 className="mb-2 text-sm font-semibold">有未保存的修改</h4>
            <p className="mb-4 text-[12.5px] text-gray-600">
              {ask.kind === 'exit' ? '返回打标台' : '切换词表'}会丢掉当前词表上的改动。
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setAsk(null)}
                className="h-8 rounded-lg px-3 text-[12.5px] text-gray-500 hover:bg-gray-100"
              >
                留在此页
              </button>
              <button
                type="button"
                onClick={() => {
                  const a = ask
                  setAsk(null)
                  if (a.kind === 'exit') onExit()
                  else setSel(a.next)
                }}
                className="h-8 rounded-lg border border-gray-300 px-3 text-[12.5px] text-gray-700 hover:bg-gray-50"
              >
                放弃修改并离开
              </button>
              <button
                type="button"
                onClick={async () => {
                  const a = ask
                  const ok = await save()
                  if (!ok) return
                  setAsk(null)
                  if (a.kind === 'exit') onExit()
                  else if (a.kind === 'switch') setSel(a.next)
                }}
                className="h-8 rounded-lg bg-blue-600 px-3 text-[12.5px] font-medium text-white hover:bg-blue-700"
              >
                保存后离开
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
