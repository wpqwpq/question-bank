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
  /** 只读账号（viewer）：全部 chip 与主标签热区都禁用，只展示现状 */
  readOnly?: boolean
}

interface Section {
  group: GroupDef | null
  tags: TagDef[]
}

/** 组的来源单元列表：单单元卷 / 旧接口只有 unit，跨单元合并组才有多个 */
const unitsOf = (g: GroupDef): string[] => g.units ?? (g.unit ? [g.unit] : [])

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

/** 标签显示语言：中／英。写 localStorage，切一次全局记住（打标模式专用） */
type Lang = 'zh' | 'en'
const LANG_KEY = 'qb.tag-lang'

function readLang(): Lang {
  try {
    return localStorage.getItem(LANG_KEY) === 'en' ? 'en' : 'zh'
  } catch {
    return 'zh'
  }
}

/** 中英切换方块按钮：「中/En」两段，当前语言那段高亮 */
function LangToggle({ lang, onToggle }: { lang: Lang; onToggle: () => void }) {
  const seg = (active: boolean) =>
    `px-1 py-0.5 leading-none ${active ? 'rounded-sm bg-blue-600 text-white' : 'text-gray-400'}`
  return (
    <button
      type="button"
      data-lang-toggle="1"
      data-lang={lang}
      title={lang === 'zh' ? '当前显示中文 · 点击切到英文' : 'Showing English · click for Chinese'}
      onClick={onToggle}
      className="flex h-6 w-[42px] shrink-0 items-center justify-center rounded-md border border-gray-300 bg-white text-[10px] font-medium transition-colors hover:border-blue-400"
    >
      <span className={seg(lang === 'zh')}>中</span>
      <span className="mx-[1px] text-gray-300">/</span>
      <span className={seg(lang === 'en')}>En</span>
    </button>
  )
}

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
  readOnly = false,
}: Props) {
  const [query, setQuery] = useState('')
  const [lang, setLang] = useState<Lang>(readLang)
  const searching = query.trim().length > 0
  const en = lang === 'en'
  /** ≥2 个标签才需要人指定主标签；恰好 1 个时它自动即主（§4.1） */
  const multi = selected.length >= 2

  const toggleLang = () => {
    const next: Lang = en ? 'zh' : 'en'
    setLang(next)
    try {
      localStorage.setItem(LANG_KEY, next)
    } catch {
      /* 隐私模式下写不进去，不影响本次显示 */
    }
  }

  /** 当前语言下的标签名（英文缺失时回退中文） */
  const nameOf = (t: TagDef) => (en ? t.en || t.zh : t.zh)

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return tags
    return tags.filter(
      (t) =>
        t.zh.toLowerCase().includes(q) ||
        (t.en ?? '').toLowerCase().includes(q) ||
        t.id.toLowerCase().includes(q),
    )
  }, [tags, query])

  /**
   * 跨单元合并（2026-10-23）：同一个一级组可能横跨多个单元——后端已把它合成一条，
   * `gids` 收下各单元的原始组 id。标签上的 `group` 仍是原始 id，所以归桶前先建
   * 「原始组 id → 合并组 id」的映射，否则后一个单元的标签会掉进「其他」。
   */
  const alias = useMemo(() => {
    const m = new Map<string, string>()
    for (const g of groups) for (const gid of g.gids ?? [g.id]) m.set(gid, g.id)
    return m
  }, [groups])

  const sections = useMemo<Section[]>(() => {
    const byGroup = new Map<string, TagDef[]>()
    for (const t of filtered) {
      const raw = t.group ?? '_ungrouped'
      const key = alias.get(raw) ?? raw
      const bucket = byGroup.get(key)
      if (bucket) bucket.push(t)
      else byGroup.set(key, [t])
    }
    if (groups.length < 2) {
      return byGroup.size ? [{ group: null, tags: filtered }] : []
    }
    const known = new Map<string, GroupDef>()
    for (const g of groups) for (const gid of g.gids ?? [g.id]) known.set(gid, g)
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
  }, [filtered, groups, alias])

  const tagOf = (id: string) => tags.find((t) => t.id === id)
  const labelOf = (id: string) => {
    const t = tagOf(id)
    if (!t) return id
    return nameOf(t)
  }
  /** tooltip 用：中英并列，切换语言后仍能对上中文习惯叫法 */
  const bothOf = (id: string) => {
    const t = tagOf(id)
    if (!t) return id
    return t.en && t.en !== t.zh ? `${t.zh} · ${t.en}` : t.zh
  }

  // 跨单元卷（组带 unit / units 字段，且后端已把本卷单元排在首位）：
  // 在第一个「不含本卷单元」的组之前插一条分隔，明示下方为其他单元的知识点。
  // 合并组可能同时含本卷单元与外部单元——它跟着首次出现的位置走，不算外部段的起点。
  // 单元清单按「首来源组出现的顺序」排（合并组里的次要单元先暂存，末尾补入），
  // 否则合并组会把靠后单元（如 U4）提前推到 U2/U3 前面。
  const cross = useMemo(() => {
    if (groups.length < 2) return null
    const own: string[] = []
    const later: string[] = []
    for (const g of groups) {
      unitsOf(g).forEach((u, idx) => {
        if (u === unit) return
        if (idx === 0) {
          if (!own.includes(u)) own.push(u)
        } else if (!own.includes(u) && !later.includes(u)) {
          later.push(u)
        }
      })
    }
    const others = [...own, ...later.filter((u) => !own.includes(u))]
    return others.length ? { label: others.join('、') } : null
  }, [groups, unit])

  const crossAt = cross
    ? sections.findIndex(
        (s) => s.group !== null && unitsOf(s.group).length > 0 && !unitsOf(s.group).includes(unit),
      )
    : -1

  return (
    <div data-tag-panel="1" className="flex min-h-0 flex-1 flex-col">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium text-gray-700">
          知识点标签
          <span className="ml-1.5 rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">{unit}</span>
          <span className="ml-1.5 text-xs font-normal text-gray-400">{en ? 'multi' : '多选'}</span>
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          <LangToggle lang={lang} onToggle={toggleLang} />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={en ? 'Search…' : '搜索…'}
            className="w-20 rounded-md border border-gray-300 px-2 py-1 text-xs focus:border-blue-500 focus:outline-none"
          />
        </div>
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
                      ? `${bothOf(id)}（${en ? 'primary' : '主标签'}）· ${en ? 'click to remove' : '点击移除'}`
                      : `${en ? 'Remove' : '移除'} ${bothOf(id)}`
                  }
                  onClick={() => onToggle(id)}
                  disabled={readOnly}
                  className={`inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-0.5 text-xs hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-70 ${
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
                  <span className="truncate">{labelOf(id)}</span>
                  {!readOnly && <span aria-hidden>×</span>}
                </button>
              ))}
            </div>
          )}

          <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto pr-0.5">
            {sections.map(({ group, tags: bucket }, i) => {
              const selCount = bucket.filter((t) => selected.includes(t.id)).length
              const active = !searching && selCount > 0
              // 合并组（横跨多个单元）：组名不带单元前缀，另挂一枚小灰标标明来源
              const srcUnits = group ? unitsOf(group) : []
              const mergedSrc = srcUnits.length > 1 ? srcUnits.join('·') : null
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
                      // 一级分组标题（B+D+加粗）：左侧 6px 蓝色圆角方块定调，底部 1px 通栏细线切段，
                      // 文字加深+加粗，字号不动。mb-1 + pb-1 让整组只比原来高 2px。
                      <div
                        className={`mb-1 flex items-center gap-1.5 border-b pb-1 text-xs font-semibold ${
                          active ? 'border-blue-300 text-blue-700' : 'border-gray-200 text-gray-700'
                        }`}
                      >
                        <span
                          data-group-marker="1"
                          className={`h-1.5 w-1.5 shrink-0 rounded-[2px] ${
                            active ? 'bg-blue-600' : 'bg-blue-400'
                          }`}
                          aria-hidden
                        />
                        {en ? group.en || group.zh : group.zh}
                        {mergedSrc && (
                          <span
                            data-group-units={mergedSrc}
                            title={`合并自 ${srcUnits.join('、')} 的同名知识点组`}
                            className="shrink-0 rounded bg-gray-100 px-1 py-px text-[10px] font-normal text-gray-500"
                          >
                            {mergedSrc}
                          </span>
                        )}
                        {!searching && selCount > 0 && (
                          <span className="ml-auto text-[10px] font-normal text-blue-700">{selCount}</span>
                        )}
                      </div>
                    )}
                    <div className={GRID}>
                      {bucket.map((t) => {
                        const on = selected.includes(t.id)
                        const isPrimary = on && primaryTag === t.id
                        const both = t.en && t.en !== t.zh ? `${t.zh} · ${t.en}` : t.zh
                        return (
                          <button
                            key={t.id}
                            type="button"
                            data-chip={t.id}
                            data-primary={isPrimary ? '1' : undefined}
                            title={
                              isPrimary
                                ? `${both}${en ? ' (primary)' : '（主标签）'}`
                                : on && multi
                                  ? `${both} · ${en ? 'click the dot to set as primary' : '点右侧圆点设为主标签'}`
                                  : both
                            }
                            onClick={() => onToggle(t.id)}
                            disabled={readOnly}
                            className={`${CHIP_BASE} ${on ? (isPrimary ? CHIP_PRIMARY : CHIP_ON) : CHIP_OFF} disabled:cursor-not-allowed disabled:opacity-70`}
                          >
                            <span className={CHIP_TEXT}>{nameOf(t)}</span>
                            {on && multi && !readOnly && (
                              <span
                                data-primary-hotzone={t.id}
                                role="button"
                                tabIndex={-1}
                                aria-label={
                                  isPrimary
                                    ? `${both} ${en ? 'is already primary' : '已是主标签'}`
                                    : `${en ? 'Set' : '把'} ${both} ${en ? 'as primary' : '设为主标签'}`
                                }
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
            {readOnly ? (
              <span className="text-gray-500">
                {en ? 'Read-only account · view only' : '只读账号 · 只能查看，不能改标签'}
              </span>
            ) : (
              <>
                {en ? `Selected ${selected.length}` : `已选 ${selected.length} 个`}
                {selected.length === 0 ? (
                  en ? ' · pick at least 1' : ' · 至少选 1 个'
                ) : primaryTag ? (
                  <>
                    {en ? ' · primary: ' : ' · 主标签：'}
                    <span className="text-blue-700">{labelOf(primaryTag)}</span>
                  </>
                ) : (
                  <span className="font-medium text-amber-600">
                    {en ? ' · set a primary tag' : ' · 请指定主标签'}
                  </span>
                )}
              </>
            )}
          </p>
        </>
      )}
    </div>
  )
}
