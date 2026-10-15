import type { QuestionDetail } from '../api'
import { DIFFICULTY_LABELS } from '../api'

const DIFF: Record<string, { label: string; cls: string }> = Object.fromEntries(
  DIFFICULTY_LABELS.map(([v, label, cls]) => [v, { label, cls }]),
)

interface Props {
  detail: QuestionDetail
  subIdx: number
  onSubIdx: (i: number) => void
  /** 面板宽度（px），与打标模式共用拖拽结果 */
  width: number
}

/** 预览模式右侧面板：只读展示题目标记；切分题用平铺按钮切换小问（联动中间题图）。 */
export default function PreviewPanel({ detail, subIdx, onSubIdx, width }: Props) {
  const split = detail.split_active && detail.subs.length > 0
  const safeIdx = Math.min(subIdx, detail.subs.length - 1)
  const sub = split ? detail.subs[safeIdx] : null
  const tags = sub ? sub.tags : detail.tags
  const difficulty = sub ? sub.difficulty : detail.difficulty
  const tagById = new Map(detail.available_tags.map((t) => [t.id, t]))

  return (
    <aside
      data-preview-panel="1"
      style={{ width }}
      className="flex shrink-0 flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white p-3"
    >
      <div className="shrink-0 rounded-lg border border-gray-200 bg-gray-50 px-2.5 py-2 text-xs text-gray-500">
        只读预览 · 标记请在打标模式下修改
      </div>

      {/* 小问平铺按钮（仅切分题）：只显示标签字母，≤6 问也能一行铺开 */}
      {split && (
        <div className="shrink-0">
          <div className="mb-1.5 text-xs font-medium text-gray-500">
            小问（点击切换右侧标记与中间题图）
          </div>
          <div className="flex gap-1.5">
            {detail.subs.map((s, i) => (
              <button
                key={s.qid}
                type="button"
                title={`${s.label} 问 · ${DIFF[s.difficulty ?? '']?.label ?? '未标难度'} · ${s.tags.length} 个标签`}
                onClick={() => onSubIdx(i)}
                className={`flex-1 rounded-md border px-1.5 py-1 text-xs font-medium transition-colors ${
                  i === safeIdx
                    ? 'border-blue-500 bg-blue-50 text-blue-700 shadow-sm'
                    : 'border-gray-300 bg-white text-gray-600 hover:border-blue-400 hover:text-blue-600'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="shrink-0">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-sm font-medium text-gray-700">
            难度{sub ? `（${sub.label} 问）` : ''}
          </span>
          <span className="text-xs font-normal text-gray-400">单选</span>
        </div>
        <div className="flex gap-1.5">
          {DIFFICULTY_LABELS.map(([value, label]) => (
            <span
              key={value}
              className={`flex-1 rounded-md border px-1.5 py-1 text-center text-xs ${
                difficulty === value ? DIFF[value].cls : 'border-gray-200 bg-gray-50 text-gray-300'
              }`}
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-sm font-medium text-gray-700">
            知识点标签{sub ? `（${sub.label} 问）` : ''}
            <span className="ml-1.5 rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">
              {detail.unit}
            </span>
          </span>
        </div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-0.5">
          {tags.length === 0 && (
            <p className="py-2 text-center text-xs text-gray-400">未选标签</p>
          )}
          {tags.map((id) => {
            const t = tagById.get(id)
            return (
              <div
                key={id}
                title={t ? `${t.zh} · ${t.en}` : id}
                className="flex w-full items-center gap-2 rounded-md border border-blue-300 bg-blue-50 px-2.5 py-1.5 text-left text-sm text-blue-800"
              >
                <span className="w-3 shrink-0 text-center">✓</span>
                <span className="shrink-0">{t?.zh ?? id}</span>
                <span className="truncate text-blue-600">{t?.en ?? ''}</span>
              </div>
            )
          })}
        </div>
        <p className="mt-1.5 shrink-0 text-[11px] text-gray-400">共 {tags.length} 个标签</p>
      </div>
    </aside>
  )
}
