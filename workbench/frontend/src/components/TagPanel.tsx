import { Fragment, useMemo, useState } from 'react'
import type { GroupDef, TagDef } from '../api'

interface Props {
  tags: TagDef[]
  groups: GroupDef[]
  unit: string
  selected: string[]
  onToggle: (id: string) => void
  /** 当前单元已解析的主标签（plans/主标签实施方案 §2） */
  primaryTag: string | null
  /** 把某个已选标签设为主标签（≥2 个标签时才出现入口） */
  onSetPrimary: (id: string) => void
}

interface Section {
  group: GroupDef | null
  tags: TagDef[]
}

const GRID =
  'grid grid-cols-[repeat(auto-fill,minmax(104px,1fr))] gap-1.5'

// 外框宽度由 grid 列宽决定 → 在 chip **内部**加右侧热区不会改变换行位置与相对位置
// （plans/主标签实施方案 §1.1）。代价是标签文字可用宽度少 16px。
const CHIP_BASE =
  'flex w-full items-center gap-1 rounded-md border px-1.5 py-1 text-xs transition-colors'
const CHIP_TEXT = 'min-w-0 flex-1 truncate text-left'

const CHIP_ON = 'border-blue-300 bg-blue-50 text-blue-700'
const CHIP_PRIMARY = 'border-blue-500 bg-blue-100 text-blue-800 font-medium'
const CHIP_OFF = 'border-gray-200 bg-white text-gray-600 hover:border-blue-400 hover:text-blue-600'

/** chip 右侧的主标签热区：空心圆点 = 可设为主；实心 = 已是主（再点无操作） */
function PrimaryDot({ on }: { on: boolean }) {
  return (
    <span
      className={`h-[7px] w-[7px] shrink-0 rounded-full ${
        on ? 'bg-blue-600' : 'border border-blue-300'
      }`}
    />
  )
}

export default function TagPanel({
  tags,
  groups,
  unit,
  selected,
  onToggle,
  primaryTag,
  onSetPrimary,
}: Props) {
  const [query, setQuery] = useState('')
  const searching = query.trim().length > 0
  /** ≥2 个标签才需要人指定主标签；恰好 1 个时它自动即主（§4.1） */
  const multi = selected.length >= 2

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

  // 跨单元卷（组带 unit 字段，且后端已把本卷单元排在首位）：
  // 在第一个「非本卷单元」的组之前插一条分隔，明示下方为其他单元的知识点。
  const cross = useMemo(() => {
    if (groups.length < 2) return null
    const others: string[] = []
    for (const g of groups) {
      if (g.unit && g.unit !== unit && !others.includes(g.unit)) others.push(g.unit)
    }
    return others.length ? { unit: others[0], label: others.join('、') } : null
  }, [groups, unit])

  const crossAt = cross ? sections.findIndex((s) => s.group?.unit === cross.unit) : -1

  return (
    <div data-tag-panel="1" className="flex min-h-0 flex-1 flex-col">
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
                  data-selected-chip={id}
                  title={
                    id === primaryTag
                      ? `${zhOf(id)}（主标签）· 点击移除`
                      : `移除 ${zhOf(id)}`
                  }
                  onClick={() => onToggle(id)}
                  className={`inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-0.5 text-xs hover:bg-blue-100 ${
                    id === primaryTag
                      ? 'border-blue-400 bg-blue-100 text-blue-800'
                      : 'border-blue-300 bg-blue-50 text-blue-700'
                  }`}
                >
                  {/* 主标签只读指示：名前 5px 实心圆点（§4.2） */}
                  {id === primaryTag && (
                    <span
                      data-primary-marker="1"
                      className="h-[5px] w-[5px] shrink-0 rounded-full bg-blue-600"
                      aria-hidden
                    />
                  )}
                  <span className="truncate">{zhOf(id)}</span>
                  <span aria-hidden>×</span>
                </button>
              ))}
            </div>
          )}

          <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto pr-0.5">
            {sections.map(({ group, tags: bucket }, i) => {
              const selCount = bucket.filter((t) => selected.includes(t.id)).length
              const active = !searching && selCount > 0
              return (
                <Fragment key={group?.id ?? '_all'}>
                  {i === crossAt && cross && (
                    <div className="flex items-center gap-2 border-t border-dashed border-gray-300 pt-2">
                      <span className="text-[11px] text-gray-400">
                        其他单元知识点（{cross.label}）
                      </span>
                    </div>
                  )}
                  <section>
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
                        const isPrimary = on && primaryTag === t.id
                        return (
                          <button
                            key={t.id}
                            type="button"
                            data-chip={t.id}
                            data-primary={isPrimary ? '1' : undefined}
                            title={
                              isPrimary
                                ? `${t.zh}（主标签）`
                                : on && multi
                                  ? `${t.zh} · 点右侧圆点设为主标签`
                                  : t.zh
                            }
                            onClick={() => onToggle(t.id)}
                            className={`${CHIP_BASE} ${on ? (isPrimary ? CHIP_PRIMARY : CHIP_ON) : CHIP_OFF}`}
                          >
                            <span className={CHIP_TEXT}>{t.zh}</span>
                            {on && multi && (
                              <span
                                data-primary-hotzone={t.id}
                                role="button"
                                tabIndex={-1}
                                aria-label={isPrimary ? `${t.zh} 已是主标签` : `把 ${t.zh} 设为主标签`}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  // ≥2 个标签时必须有一个主标签，不允许「取消」（§4.1）
                                  if (!isPrimary) onSetPrimary(t.id)
                                }}
                                className="flex h-4 w-4 shrink-0 items-center justify-center rounded"
                              >
                                <PrimaryDot on={isPrimary} />
                              </span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  </section>
                </Fragment>
              )
            })}
            {filtered.length === 0 && (
              <p className="py-2 text-center text-xs text-gray-400">无匹配标签</p>
            )}
          </div>

          <p data-primary-hint="1" className="mt-1.5 shrink-0 text-[11px] text-gray-400">
            已选 {selected.length} 个
            {selected.length === 0 ? (
              ' · 至少选 1 个'
            ) : primaryTag ? (
              <>
                {' · 主标签：'}
                <span className="text-blue-700">{zhOf(primaryTag)}</span>
              </>
            ) : (
              <span className="font-medium text-amber-600"> · 请指定主标签</span>
            )}
          </p>
        </>
      )}
    </div>
  )
}
