import { useCallback, useEffect, useMemo, useState } from 'react'
import type { TagDeleteMode, TagDeleteResp, TagUsageResp } from '../api'
import { api, ConflictError, SESSION_LABELS } from '../api'

/**
 * 删标签 / 删分组的确认弹窗（admin + lead）。
 *
 * 打开即拉影响面（GET /usage），把两种方式各自会清多少、摘多少摆在一起让人选；
 * 点确认后一步到底（清标注 + 改词表），**不可回滚**——词表旧版会落到 _prev/。
 *
 * 为什么不做成「改草稿 → 点保存」：草稿里删标签会让题目引用着词表里不存在的 id，
 * 中间态在任何写标注路径上都会被 _validate_tags 拒掉（422）。
 */

type Mode = TagDeleteMode

export interface DeleteTarget {
  kind: 'tag' | 'group'
  /** 标签 id 或分组 id */
  id: string
  zh: string
  en?: string
  /** 该标签所属分组的中文名（删标签时用） */
  groupZh?: string
}

interface Props {
  board: string
  qual: string
  subject: string
  unit: string
  target: DeleteTarget
  /** tag id → 中文名（明细表里把其它标签也显示成人话） */
  labelOf: (id: string) => string
  onClose: () => void
  onDone: (r: TagDeleteResp) => void
}

const MODE_ORDER: Mode[] = ['smart', 'clear_all']

export default function TagDeleteDialog({
  board,
  qual,
  subject,
  unit,
  target,
  labelOf,
  onClose,
  onDone,
}: Props) {
  const [usage, setUsage] = useState<TagUsageResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [mode, setMode] = useState<Mode>('smart')
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(false)

  const isTag = target.kind === 'tag'

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .taxonomyUsage(board, qual, subject, unit, {
        tagId: isTag ? target.id : undefined,
        groupId: isTag ? undefined : target.id,
      })
      .then((r) => {
        if (!cancelled) {
          setUsage(r)
          setErr('')
        }
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [board, qual, subject, unit, target.id, isTag])

  // ESC 关闭（执行中不响应，避免以为取消了其实已经写了一半）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  const doomed = useMemo(
    () => new Set((usage?.tags ?? []).map((t) => t.id)),
    [usage],
  )

  const submit = useCallback(async () => {
    if (!usage || busy) return
    setBusy(true)
    setErr('')
    try {
      const r = await api.deleteTaxonomy(board, qual, subject, {
        unit,
        mode,
        tag_id: isTag ? target.id : undefined,
        group_id: isTag ? undefined : target.id,
        base_sig: usage.sig,
      })
      onDone(r)
    } catch (e) {
      if (e instanceof ConflictError) {
        setErr('这份词表刚被其他账号保存过，请重新加载后再删。标注没有被改动。')
      } else {
        setErr((e as Error).message)
      }
    } finally {
      setBusy(false)
    }
  }, [usage, busy, board, qual, subject, unit, mode, isTag, target.id, onDone])

  const rows = usage?.rows ?? []
  const visible = expanded ? rows : rows.slice(0, 8)
  const cur = usage?.by_mode[mode]

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-40 grid place-items-center bg-gray-900/40 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose()
      }}
    >
      <div className="flex max-h-[86vh] w-[760px] max-w-full flex-col overflow-hidden rounded-xl bg-white shadow-2xl">
        {/* 标题 */}
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-gray-200 px-4 py-3">
          <div className="min-w-0">
            <h3 className="text-[14px] font-semibold text-gray-900">
              {isTag ? '删除标签' : '删除分组'}
              <span className="ml-2 font-normal text-gray-700">{target.zh}</span>
              {target.en && (
                <span className="ml-1.5 text-[12px] font-normal text-gray-400">{target.en}</span>
              )}
            </h3>
            <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[11.5px] text-gray-400">
              <span>{target.id}</span>
              {isTag && target.groupZh && <span>· 分组 {target.groupZh}</span>}
              <span>· {unit}</span>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-40"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" className="h-3.5 w-3.5">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* 影响面概况 */}
        <div className="shrink-0 border-b border-gray-200 bg-gray-50 px-4 py-2 text-[12.5px] text-gray-700">
          {loading ? (
            <span className="text-gray-400">正在统计影响面…</span>
          ) : usage ? (
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>
                共 <b className="tabular-nums text-gray-900">{usage.total_units}</b> 处正在使用
              </span>
              <span className="text-gray-300">|</span>
              <span className="tabular-nums">{usage.paper_count} 卷</span>
              {usage.flagged_units > 0 && (
                <>
                  <span className="text-gray-300">|</span>
                  <span className="rounded border border-amber-200 bg-amber-50 px-1.5 text-[11.5px] text-amber-800">
                    {usage.flagged_units} 处被标「有误」，一并处理（标记保留）
                  </span>
                </>
              )}
              {usage.removed_groups.length > 0 && (
                <>
                  <span className="text-gray-300">|</span>
                  <span className="text-[11.5px] text-gray-500">
                    会连带删除空分组：{usage.removed_groups.map((g) => g.zh).join('、')}
                  </span>
                </>
              )}
              {usage.unit_tags_after === 0 && (
                <>
                  <span className="text-gray-300">|</span>
                  <span className="text-[11.5px] text-amber-800">
                    删完 {usage.unit} 就没有任何标签了
                  </span>
                </>
              )}
            </span>
          ) : (
            <span className="text-red-700">{err || '统计失败'}</span>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {/* 方式选择 */}
          <div className="flex flex-col gap-2">
            {MODE_ORDER.map((m) => {
              const on = mode === m
              const c = usage?.by_mode[m]
              return (
                <button
                  key={m}
                  type="button"
                  data-ta-del-mode={m}
                  disabled={!usage || busy}
                  onClick={() => setMode(m)}
                  className={`flex items-start gap-3 rounded-[10px] border px-3 py-2.5 text-left transition-colors disabled:opacity-60 ${
                    on
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-300 bg-white hover:border-gray-400 hover:bg-gray-50'
                  }`}
                >
                  <span
                    className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full border ${
                      on ? 'border-blue-600 bg-blue-600' : 'border-gray-300 bg-white'
                    }`}
                  >
                    {on && <span className="h-1.5 w-1.5 rounded-full bg-white" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-baseline justify-between gap-3">
                      <span className="text-[13px] font-medium text-gray-900">
                        {m === 'clear_all' ? '方式一 · 全部清空' : '方式二 · 只删这个标签'}
                      </span>
                      {c && (
                        <span className="shrink-0 text-[12px] tabular-nums text-gray-600">
                          {m === 'clear_all' ? (
                            <>清空 {c.clear} 处</>
                          ) : (
                            <>
                              整清 {c.clear} 处 · 只摘 {c.strip} 处
                            </>
                          )}
                        </span>
                      )}
                    </span>
                    <span className="mt-0.5 block text-[12px] leading-relaxed text-gray-600">
                      {m === 'clear_all'
                        ? '这些题/小问的标签全部清空（连其它标签一起），难度保留。'
                        : '主标签被删到的才整份清空；其余只摘掉这个标签，其它标签与主标签不动。'}
                    </span>
                  </span>
                </button>
              )
            })}
          </div>

          {/* 明细 */}
          <div className="mt-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-[11.5px] font-medium text-gray-500">
                明细{usage && usage.total_units > 0 ? `（前 ${visible.length} / 共 ${usage.total_units}）` : ''}
              </span>
              {usage && usage.truncated && (
                <span className="text-[11px] text-gray-400">后端只回前 300 条</span>
              )}
            </div>
            <div className="overflow-hidden rounded-[10px] border border-gray-200">
              <table className="w-full table-fixed border-collapse text-[12px]">
                <thead>
                  <tr className="bg-gray-50 text-left text-[11.5px] text-gray-500">
                    <th className="w-[168px] px-2 py-1.5 font-medium">卷</th>
                    <th className="w-[64px] px-2 py-1.5 font-medium">题</th>
                    <th className="px-2 py-1.5 font-medium">现有标签</th>
                    <th className="w-[92px] px-2 py-1.5 font-medium">随之变成</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r) => {
                    const after = mode === 'clear_all' ? r.clear_all : r.smart
                    const cleared = after.length === 0
                    return (
                      <tr key={r.qid} className="border-t border-gray-100 align-top">
                        <td className="truncate px-2 py-1.5 text-gray-600" title={r.qid}>
                          {r.paper_code ?? '—'}
                          {r.year ? ` · ${r.year}` : ''}
                          {r.session ? ` ${SESSION_LABELS[r.session] ?? r.session}` : ''}
                        </td>
                        <td className="px-2 py-1.5 font-mono text-gray-700">
                          {r.question_number ?? '—'}
                          {r.sub_label && <span className="text-gray-400">({r.sub_label})</span>}
                        </td>
                        <td className="px-2 py-1.5">
                          <span className="flex flex-wrap gap-1">
                            {r.tags.map((t) => {
                              const pre = doomed.has(t)
                              const isPrimary = r.primary_tag === t
                              return (
                                <span
                                  key={t}
                                  className={`rounded border px-1 py-px text-[11px] ${
                                    pre
                                      ? 'border-red-200 bg-red-50 text-red-700'
                                      : 'border-gray-200 bg-gray-50 text-gray-600'
                                  }`}
                                >
                                  {labelOf(t)}
                                  {isPrimary && <span className="ml-1 text-[10px] text-blue-700">主</span>}
                                </span>
                              )
                            })}
                          </span>
                        </td>
                        <td className="px-2 py-1.5">
                          {cleared ? (
                            <span className="text-gray-500">无标签</span>
                          ) : (
                            <span className="tabular-nums text-gray-700">剩 {after.length} 个</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                  {!loading && rows.length === 0 && (
                    <tr>
                      <td colSpan={4} className="px-2 py-6 text-center text-gray-400">
                        没有题目在用这个标签
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {rows.length > 8 && (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="mt-1.5 rounded-md px-1.5 py-0.5 text-[11.5px] text-blue-600 hover:bg-blue-50"
              >
                {expanded ? '收起' : `展开全部 ${rows.length} 行`}
              </button>
            )}
          </div>
        </div>

        {/* 底部 */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-gray-200 px-4 py-2.5">
          <p className="min-w-0 text-[11.5px] leading-snug text-gray-500">
            {err ? (
              <span className="text-red-700">{err}</span>
            ) : (
              <>
                不可撤销（题目上的标注只留操作日志）。词表旧版会自动备份到{' '}
                <span className="font-mono">data/taxonomy/_prev/</span>。
              </>
            )}
          </p>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="h-8 rounded-lg px-3 text-[12.5px] text-gray-600 hover:bg-gray-100 disabled:opacity-40"
            >
              取消
            </button>
            <button
              type="button"
              data-ta-del-confirm="1"
              disabled={!usage || busy || !!err}
              onClick={() => void submit()}
              title={cur ? `将处理 ${cur.clear + cur.strip} 处` : undefined}
              className={`h-8 rounded-lg border px-3.5 text-[12.5px] font-medium transition-colors ${
                usage && !busy && !err
                  ? 'border-red-300 bg-red-50 text-red-700 hover:bg-red-100'
                  : 'cursor-not-allowed border-gray-200 bg-gray-50 text-gray-400'
              }`}
            >
              {busy ? '删除中…' : '确认删除'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
