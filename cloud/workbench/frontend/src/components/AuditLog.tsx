import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import type { AuditRecord } from '../api'
import { authApi } from '../api'

/**
 * 操作日志（管理员专享）。
 *
 * 数据来自 `GET /api/auth/audit`，后端读 `data/_audit/<日期>.jsonl` 最近 7 天的文件、
 * 新→旧取前 N 条。这里**不做任何写操作**，纯查看。
 *
 * 为什么要有这一页：确认记录在 state.db 里只有「最后是谁、什么时候」，
 * 而「标签被谁从 A 改成 B」只落在审计里。以前只能 SSH 上去 tail jsonl / sqlite3，
 * 现在管理员在网页上就能查——尤其是 AI 与人工混着标的时候，一眼看得出是谁改的。
 */

const ACTIONS: Record<string, string> = {
  login: '登录',
  logout: '登出',
  'password.change': '修改密码',
  'taxonomy.save': '改词表',
  'annotation.save': '保存标注',
  confirm: '确认',
  unconfirm: '取消确认',
  'flag.set': '标有问题',
  'flag.clear': '取消标记',
}

const ACTION_CLS: Record<string, string> = {
  'annotation.save': 'bg-blue-50 text-blue-700 border-blue-200',
  confirm: 'bg-green-50 text-green-700 border-green-200',
  unconfirm: 'bg-amber-50 text-amber-700 border-amber-200',
  'flag.set': 'bg-red-50 text-red-700 border-red-200',
  'flag.clear': 'bg-gray-50 text-gray-600 border-gray-200',
  'taxonomy.save': 'bg-purple-50 text-purple-700 border-purple-200',
  login: 'bg-gray-50 text-gray-500 border-gray-200',
  logout: 'bg-gray-50 text-gray-500 border-gray-200',
  'password.change': 'bg-gray-50 text-gray-500 border-gray-200',
}

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  lead: '学科负责人',
  tagger: '打标员',
  viewer: '只读',
  service: 'AI',
}

const fmtTime = (ts: string) => (ts || '').replace('T', ' ').replace(/([+-]\d{2}:\d{2})$/, '')

export default function AuditLog({ onExit }: { onExit: () => void }) {
  const [rows, setRows] = useState<AuditRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [limit, setLimit] = useState(300)
  const [q, setQ] = useState('')
  const [via, setVia] = useState<'all' | 'cookie' | 'key'>('all')
  const [open, setOpen] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      setRows(await authApi.audit(limit))
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [limit])

  useEffect(() => {
    void load()
  }, [load])

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase()
    return rows.filter((r) => {
      if (via !== 'all' && (r.via ?? '') !== via) return false
      if (!kw) return true
      return [r.user, r.name, r.target, r.action, r.ip]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(kw))
    })
  }, [rows, q, via])

  const counts = useMemo(() => {
    const c = { cookie: 0, key: 0 }
    for (const r of rows) {
      if (r.via === 'key') c.key += 1
      else c.cookie += 1
    }
    return c
  }, [rows])

  return (
    <div data-audit="1" className="flex h-screen flex-col bg-white text-gray-900">
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
            操作日志
            <span className="ml-2 text-xs font-normal text-gray-400">
              最近 7 天 · 新→旧 · 共 {filtered.length} 条
            </span>
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-gray-300 bg-gray-100 p-0.5 text-xs">
            {(
              [
                ['all', `全部 ${rows.length}`],
                ['cookie', `人工 ${counts.cookie}`],
                ['key', `AI ${counts.key}`],
              ] as const
            ).map(([v, label]) => (
              <button
                key={v}
                type="button"
                onClick={() => setVia(v)}
                className={`rounded-md px-2.5 py-1 font-medium transition-colors ${
                  via === v ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-8 rounded-md border border-gray-300 bg-white px-1.5 text-xs text-gray-700"
          >
            <option value={200}>200 条</option>
            <option value={300}>300 条</option>
            <option value={1000}>1000 条</option>
            <option value={2000}>2000 条</option>
          </select>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="h-8 rounded-lg border border-gray-300 bg-white px-3 text-xs font-medium text-gray-700 transition-colors enabled:hover:bg-gray-50 disabled:opacity-50"
          >
            {loading ? '读取中…' : '刷新'}
          </button>
        </div>
      </header>

      <div className="flex shrink-0 items-center gap-2 border-b border-gray-200 bg-gray-50 px-4 py-2">
        <div className="flex h-[30px] w-72 items-center gap-1.5 rounded-md border border-gray-300 bg-white px-2">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            className="h-3.5 w-3.5 shrink-0 text-gray-400"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="按人 / 对象 / IP 过滤…"
            className="h-full w-full bg-transparent text-xs text-gray-800 placeholder:text-gray-400 focus:outline-none"
          />
        </div>
        <span className="text-[11px] text-gray-400">
          点一行看改动前后（保存标注的 before / after 都在里面）
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {err ? (
          <div className="p-6 text-sm text-red-700">读取失败：{err}</div>
        ) : loading && rows.length === 0 ? (
          <div className="p-6 text-sm text-gray-400">正在读取…</div>
        ) : filtered.length === 0 ? (
          <div className="p-6 text-sm text-gray-400">没有匹配的记录</div>
        ) : (
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 z-10 bg-white">
              <tr className="border-b border-gray-200 text-[11px] font-medium text-gray-500">
                <th className="w-[136px] px-3 py-2 font-medium">时间</th>
                <th className="w-[150px] px-3 py-2 font-medium">操作人</th>
                <th className="w-[92px] px-3 py-2 font-medium">角色</th>
                <th className="w-[72px] px-3 py-2 font-medium">通道</th>
                <th className="w-[92px] px-3 py-2 font-medium">动作</th>
                <th className="px-3 py-2 font-medium">对象</th>
                <th className="w-[124px] px-3 py-2 font-medium">IP</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r, i) => {
                const detailKeys = Object.keys(r.detail ?? {})
                const expandable = detailKeys.length > 0
                return (
                  <Fragment key={`${r.ts}-${i}`}>
                    <tr
                      onClick={() => setOpen(expandable ? (open === i ? null : i) : null)}
                      className={`border-b border-gray-100 ${
                        expandable ? 'cursor-pointer hover:bg-gray-50' : ''
                      } ${open === i ? 'bg-blue-50/40' : ''}`}
                    >
                      <td className="px-3 py-1.5 tabular-nums text-gray-500">{fmtTime(r.ts)}</td>
                      <td className="px-3 py-1.5">
                        <span className="font-medium text-gray-800">{r.name ?? r.user ?? '—'}</span>
                        {r.user && r.name && r.name !== r.user && (
                          <span className="ml-1 font-mono text-[10px] text-gray-400">{r.user}</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5 text-gray-600">
                        {ROLE_LABELS[r.role ?? ''] ?? r.role ?? '—'}
                      </td>
                      <td className="px-3 py-1.5">
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                            r.via === 'key'
                              ? 'bg-purple-50 text-purple-700'
                              : 'bg-gray-100 text-gray-600'
                          }`}
                        >
                          {r.via === 'key' ? 'AI' : r.via === 'cookie' ? '人工' : '—'}
                        </span>
                      </td>
                      <td className="px-3 py-1.5">
                        <span
                          className={`inline-block rounded border px-1.5 py-0.5 text-[10px] font-medium ${
                            ACTION_CLS[r.action] ?? 'border-gray-200 bg-gray-50 text-gray-600'
                          }`}
                        >
                          {ACTIONS[r.action] ?? r.action}
                        </span>
                      </td>
                      <td className="max-w-0 truncate px-3 py-1.5 font-mono text-[11px] text-gray-700">
                        {r.target}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-[11px] text-gray-400">{r.ip ?? '—'}</td>
                    </tr>
                    {open === i && (
                      <tr className="border-b border-gray-200 bg-gray-50">
                        <td colSpan={7} className="px-3 py-2">
                          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded border border-gray-200 bg-white p-2 font-mono text-[11px] leading-5 text-gray-700">
                            {JSON.stringify(r.detail, null, 1)}
                          </pre>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
