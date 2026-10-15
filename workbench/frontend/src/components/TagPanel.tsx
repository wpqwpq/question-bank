import { useMemo, useState } from 'react'
import type { GroupDef, TagDef } from '../api'

interface Props {
  tags: TagDef[]
  groups: GroupDef[]
  unit: string
  selected: string[]
  onToggle: (id: string) => void
}

interface Section {
  group: GroupDef | null
  tags: TagDef[]
}

const GRID =
  'grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-1.5'

const CHIP_BASE =
  'truncate rounded-md border px-1.5 py-1 text-xs transition-colors'

const CHIP_ON = 'border-blue-300 bg-blue-50 text-blue-700'
const CHIP_OFF = 'border-gray-200 bg-white text-gray-600 hover:border-blue-400 hover:text-blue-600'

export default function TagPanel({ tags, groups, unit, selected, onToggle }: Props) {
  const [query, setQuery] = useState('')
  const searching = query.trim().length > 0

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return tags
    return tags.filter((t) => t.zh.toLowerCase().includes(q) || t.id.toLowerCase().includes(q))
  }, [tags, query])

  const sections = useMemo<Section[]>(() => {
    const byGroup = new Map<string, TagDef[]>()
    for (const t of filtered) {
      const key = t.group ?? '_ungrouped'
      const bucket = byGroup.get(key)
      if (bucket) bucket.push(t)
      else byGroup.set(key, [t])
    }
    if (groups.length < 2) {
      return byGroup.size ? [{ group: null, tags: filtered }] : []
    }
    const known = new Map(groups.map((g) => [g.id, g]))
    const out: Section[] = []
    for (const g of groups) {
      const bucket = byGroup.get(g.id)
      if (bucket?.length) out.push({ group: g, tags: bucket })
    }
    // 防御：标签引用了词表外的组 id 时兜底归入「其他」
    for (const [gid, bucket] of byGroup) {
      if (!known.has(gid)) out.push({ group: { id: gid, zh: '其他' }, tags: bucket })
    }
    return out
  }, [filtered, groups])

  const zhOf = (id: string) => tags.find((t) => t.id === id)?.zh ?? id

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-gray-700">
          知识点标签
          <span className="ml-1.5 rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">{unit}</span>
          <span className="ml-1.5 text-xs font-normal text-gray-400">多选</span>
        </span>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索…"
          className="w-24 rounded-md border border-gray-300 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
        />
      </div>

      {tags.length === 0 ? (
        <p className="py-6 text-center text-xs text-gray-400">本学科暂无标签词表</p>
      ) : (
        <>
          {selected.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1 rounded-md border border-gray-200 bg-gray-50 p-1.5">
              {selected.map((id) => (
                <button
                  key={id}
                  type="button"
                  title={`移除 ${zhOf(id)}`}
                  onClick={() => onToggle(id)}
                  className="inline-flex max-w-full items-center gap-1 rounded border border-blue-300 bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700 hover:bg-blue-100"
                >
                  <span className="truncate">{zhOf(id)}</span>
                  <span aria-hidden>×</span>
                </button>
              ))}
            </div>
          )}

          <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto pr-0.5">
            {sections.map(({ group, tags: bucket }) => {
              const selCount = bucket.filter((t) => selected.includes(t.id)).length
              const active = !searching && selCount > 0
              return (
                <section key={group?.id ?? '_all'}>
                  {group && (
                    <div
                      className={`mb-1.5 flex items-center gap-1.5 text-xs font-medium ${
                        active ? 'text-blue-700' : 'text-gray-500'
                      }`}
                    >
                      <span
                        className={`h-3 w-0.5 rounded-full ${active ? 'bg-blue-600' : 'bg-gray-300'}`}
                        aria-hidden
                      />
                      {group.zh}
                      {!searching && selCount > 0 && (
                        <span className="ml-auto text-[10px] font-normal text-blue-700">{selCount}</span>
                      )}
                    </div>
                  )}
                  <div className={GRID}>
                    {bucket.map((t) => {
                      const on = selected.includes(t.id)
                      return (
                        <button
                          key={t.id}
                          type="button"
                          title={t.zh}
                          onClick={() => onToggle(t.id)}
                          className={`${CHIP_BASE} ${on ? CHIP_ON : CHIP_OFF}`}
                        >
                          {t.zh}
                        </button>
                      )
                    })}
                  </div>
                </section>
              )
            })}
            {filtered.length === 0 && (
              <p className="py-2 text-center text-xs text-gray-400">无匹配标签</p>
            )}
          </div>

          <p className="mt-1.5 shrink-0 text-[11px] text-gray-400">
            已选 {selected.length} 个 · 至少选 1 个
          </p>
        </>
      )}
    </div>
  )
}
