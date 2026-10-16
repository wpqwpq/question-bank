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
  /** 勾选/取消确认当前单元（切分题=当前小问；非切分题=整题）。true=确认 */
  onConfirm: (confirmed: boolean) => void
  /** 确认请求进行中（禁用勾选框，防连点） */
  confirming?: boolean
  /** 本次在预览里补选／改选的难度（尚未落库，docs/13） */
  pick: string | null
  onPickDifficulty: (d: string) => void
}

/**
 * 预览模式右侧面板：展示题目标记；切分题用平铺按钮切换小问（联动中间题图）。
 * 难度可**就地补选／修改**（docs/13）：没有难度的单元不能勾确认框；勾选即把难度写回题库。
 */
export default function PreviewPanel({
  detail,
  subIdx,
  onSubIdx,
  width,
  onConfirm,
  confirming,
  pick,
  onPickDifficulty,
}: Props) {
  const split = detail.split_active && detail.subs.length > 0
  const safeIdx = Math.min(subIdx, detail.subs.length - 1)
  const sub = split ? detail.subs[safeIdx] : null
  const tags = sub ? sub.tags : detail.tags
  const savedDifficulty = sub ? sub.difficulty : detail.difficulty
  const difficulty = pick ?? savedDifficulty
  const hasDifficulty = !!difficulty
  const tagById = new Map(detail.available_tags.map((t) => [t.id, t]))

  // 当前确认单元的状态：切分题看当前小问，非切分题看整题
  const unitVerified = split ? !!sub?.verified : !!detail.verified
  const totalUnits = detail.total_units ?? (split ? detail.subs.length : 1)
  const doneUnits =
    detail.confirmed_units ?? (split ? detail.subs.filter((s) => s.verified).length : detail.verified ? 1 : 0)

  const badgeCls =
    detail.verify_state === 'all'
      ? 'bg-green-200 text-green-900'
      : detail.verify_state === 'partial'
        ? 'bg-amber-100 text-amber-800'
        : 'bg-gray-100 text-gray-500'

  return (
    <aside
      data-preview-panel="1"
      data-qid={detail.qid}
      style={{ width }}
      className="flex shrink-0 flex-col gap-3 overflow-y-auto border-l border-gray-200 bg-white p-3"
    >
      <div className="shrink-0 rounded-lg border border-gray-200 bg-gray-50 px-2.5 py-2 text-xs text-gray-500">
        标签请在打标模式修改；<span className="text-gray-700">难度可在此直接补选／修改</span>，确认即写回题库
      </div>

      {/* 小问平铺按钮（仅切分题）：只显示标签字母，≤6 问也能一行铺开；✓ = 该小问已确认 */}
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
                title={`${s.label} 问 · ${DIFF[s.difficulty ?? '']?.label ?? '未标难度'} · ${s.tags.length} 个标签 · ${
                  s.verified ? '已确认' : '未确认'
                }`}
                onClick={() => onSubIdx(i)}
                className={`flex-1 rounded-md border px-1.5 py-1 text-xs font-medium transition-colors ${
                  i === safeIdx
                    ? 'border-blue-500 bg-blue-50 text-blue-700 shadow-sm'
                    : s.verified
                      ? 'border-green-400 bg-green-50 text-green-800 hover:border-green-500'
                      : 'border-gray-300 bg-white text-gray-600 hover:border-blue-400 hover:text-blue-600'
                }`}
              >
                {s.label}
                {s.verified ? (
                  <span className="ml-0.5 align-super text-[9px] leading-none">✓</span>
                ) : !s.difficulty ? (
                  <span className="ml-0.5 align-super text-[9px] leading-none text-amber-600" title="缺难度">
                    !
                  </span>
                ) : null}
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
          <span className="text-xs font-normal text-gray-400">
            {pick ? '已改选 · 确认后写回' : hasDifficulty ? '单选 · 可改' : '单选 · 缺则需先补'}
            {' · '}快捷键 1–5
          </span>
        </div>
        <div className="flex gap-1.5">
          {DIFFICULTY_LABELS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              disabled={confirming}
              onClick={() => onPickDifficulty(value)}
              className={`flex-1 rounded-md border px-1.5 py-1 text-center text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                difficulty === value
                  ? DIFF[value].cls
                  : 'border-gray-200 bg-gray-50 text-gray-400 hover:border-blue-400 hover:text-blue-600'
              }`}
            >
              {label}
            </button>
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

      {/* 审核确认（docs/11 红框处 + docs/13）：勾选 = 确认当前单元无误。
          没有难度的单元先选难度才能勾（勾选时把难度一起写回题库）。 */}
      <div
        data-confirm-box="1"
        className={`shrink-0 rounded-lg border p-2.5 ${
          unitVerified
            ? 'border-green-300 bg-green-50'
            : hasDifficulty
              ? 'border-gray-200 bg-gray-50'
              : 'border-amber-300 bg-amber-50'
        }`}
      >
        <label
          className={`flex select-none items-start justify-between gap-2 ${
            hasDifficulty ? 'cursor-pointer' : 'cursor-not-allowed'
          }`}
        >
          <span className="flex items-start gap-2">
            <input
              type="checkbox"
              data-confirm-checkbox="1"
              checked={unitVerified}
              disabled={confirming || !hasDifficulty}
              onChange={(e) => onConfirm(e.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-green-600 disabled:cursor-not-allowed disabled:opacity-50"
            />
            <span className="text-sm font-medium text-gray-800">
              {split ? '我已核对本小问标记' : '我已核对本题标记'}
            </span>
          </span>
          <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium ${badgeCls}`}>
            {split ? `${doneUnits}/${totalUnits} 小问` : unitVerified ? '已确认' : '未确认'}
          </span>
        </label>
        <p
          className={`mt-1 pl-6 text-[11px] leading-4 ${
            !hasDifficulty && !unitVerified ? 'text-amber-700' : 'text-gray-400'
          }`}
        >
          {confirming
            ? '提交中…'
            : !hasDifficulty
              ? '先选中难度，才能勾选确认'
              : unitVerified
                ? '已确认 · 取消勾选可撤销（不会跳转）'
                : pick
                  ? split
                    ? '确认后写回该小问难度，并自动切到下一个未确认的小问'
                    : '确认后写回本题难度，并自动跳到下一题'
                  : split
                    ? '勾选后自动切到下一个未确认的小问'
                    : '勾选后自动跳到下一题'}
        </p>
      </div>
    </aside>
  )
}
