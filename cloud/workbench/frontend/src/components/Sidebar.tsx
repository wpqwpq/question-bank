import type { Paper, QuestionSummary, Status, VerifyState } from '../api'
import { SESSION_LABELS, statusColor, verifyColor } from '../api'

interface Props {
  papers: Paper[]
  visiblePapers: Paper[]
  board: string
  qualification: string
  subject: string
  paperId: string
  questions: QuestionSummary[]
  currentIndex: number
  mode?: 'annotate' | 'preview'
  onBoard: (v: string) => void
  onQualification: (v: string) => void
  onSubject: (v: string) => void
  onPaper: (v: string) => void
  onJump: (index: number) => void
}

const selectCls =
  'w-full rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-800 focus:border-blue-500 focus:outline-none'

function Label({ text }: { text: string }) {
  return <label className="mb-1 block text-xs font-medium text-gray-500">{text}</label>
}

export default function Sidebar(p: Props) {
  const currentPaper = p.papers.find((x) => x.id === p.paperId)

  return (
    <aside className="flex w-60 shrink-0 flex-col gap-4 overflow-y-auto border-r border-gray-200 bg-gray-50 p-3">
      <div>
        <Label text="考试局" />
        <select className={selectCls} value={p.board} onChange={(e) => p.onBoard(e.target.value)}>
          <option value="edexcel">Edexcel</option>
          <option value="aqa">AQA</option>
          <option value="cie">CIE</option>
        </select>
      </div>
      <div>
        <Label text="资格" />
        <select
          className={selectCls}
          value={p.qualification}
          onChange={(e) => p.onQualification(e.target.value)}
        >
          <option value="a-level">A-Level</option>
          <option value="igcse">IGCSE</option>
          <option value="gcse">GCSE</option>
        </select>
      </div>
      <div>
        <Label text="学科" />
        <select className={selectCls} value={p.subject} onChange={(e) => p.onSubject(e.target.value)}>
          <option value="physics">Physics</option>
          <option value="chemistry">Chemistry</option>
          <option value="biology">Biology</option>
          <option value="mathematics">Mathematics</option>
          <option value="further-mathematics">Further Mathematics</option>
          <option value="accounting">Accounting</option>
          <option value="economics">Economics</option>
        </select>
      </div>
      <div>
        <Label text={`试卷（${p.visiblePapers.length} 卷）`} />
        <select className={selectCls} value={p.paperId} onChange={(e) => p.onPaper(e.target.value)}>
          <option value="">— 选择试卷 —</option>
          {p.visiblePapers.map((paper) => {
            // 整卷「核对完成」的对钩：每题的确认单元（非切分题=整题，切分题=每个小问）
            // 都已确认才算 1；但**标了「有误」的题退出待办**——既不算待确认，也不算已确认。
            // 所以分母是「总数 − 有误题数」。这样无论先标有误还是先核对，都不会卡住对钩。
            // 计数与后端 api.list_papers 的 verified_count / flagged_count 同源，不另算一套。
            const activeTotal = Math.max(0, paper.question_count - (paper.flagged_count ?? 0))
            const allVerified = activeTotal > 0 && (paper.verified_count ?? 0) >= activeTotal
            return (
              <option key={paper.id} value={paper.id}>
                {paper.year} {SESSION_LABELS[paper.session] ?? paper.session} · {paper.paper_code}
                （{paper.annotated_count}/{paper.question_count}）
                {allVerified ? ' ✓' : ''}
              </option>
            )
          })}
        </select>
      </div>

      {currentPaper && (
        <div className="mt-1">
          <div className="mb-2 flex items-baseline justify-between">
            <Label text={p.mode === 'preview' ? '已打标题目（点击查看）' : '题号（点击跳题）'} />
            {/* 预览模式的分母用「预览列表长度」：annotated_count 只数「全部小问都打完」的题，
                而列表里还有「只打了一部分小问」的题（灰色待补齐），用它会跟格子数对不上。 */}
            <span className="text-xs font-medium text-gray-600">
              {p.mode === 'preview'
                ? `${currentPaper.verified_count ?? 0}/${p.questions.length} 已确认`
                : `${currentPaper.annotated_count}/${currentPaper.question_count} 已标`}
            </span>
          </div>
          <div className="grid grid-cols-5 gap-1.5">
            {p.questions.map((q, i) => {
              // 「待补齐」= 有标签但缺难度或缺主标签（plans/主标签实施方案 §2）→ 灰 + 不能确认
              const pending =
                p.mode === 'preview' && (q.difficulty_ready === false || q.primary_ready === false)
              const why = [
                q.difficulty_ready === false ? '待补难度' : null,
                q.primary_ready === false ? '待定主标签' : null,
              ].filter(Boolean) as string[]
              return (
                <button
                  key={q.qid}
                  type="button"
                  title={
                    p.mode === 'preview'
                      ? `${q.flagged ? '⚠ 已标记有误 · ' : ''}第 ${q.question_number} 题 · ${
                          why.length
                            ? `${why.join('、')}（不能确认）`
                            : q.verify_state === 'all'
                              ? '已确认'
                              : q.verify_state === 'partial'
                                ? `部分确认（${q.confirmed_units ?? 0}/${q.total_units ?? 0} 小问）`
                                : '待确认'
                        }`
                      : `${q.flagged ? '⚠ 已标记有误（修改已停用） · ' : ''}第 ${q.question_number} 题 · ${
                          q.split ? `已切分（${(q.sub_labels ?? []).join('/')}）· ` : ''
                        }${
                          q.status === 'done'
                            ? `已打标${q.difficulty ? '' : '（难度未标）'}${
                                q.primary_ready === false ? '（未定主标签）' : ''
                              }`
                            : q.status === 'partial'
                              ? '只有难度、缺知识点标签'
                              : '未打标'
                        }`
                  }
                  className={`h-8 ${
                    p.mode === 'preview'
                      ? verifyColor((q.verify_state ?? 'none') as VerifyState, i === p.currentIndex, pending)
                      : statusColor(q.status as Status, i === p.currentIndex)
                  } ${q.flagged ? 'outline outline-2 outline-offset-1 outline-red-400' : ''}`}
                  onClick={() => p.onJump(i)}
                >
                  {q.question_number}
                  {q.split && <span className="ml-0.5 align-super text-[9px] leading-none">abc</span>}
                  {q.flagged && (
                    <span className="ml-0.5 align-super text-[9px] font-bold leading-none text-red-600">
                      !
                    </span>
                  )}
                </button>
              )
            })}
          </div>
          {p.mode !== 'preview' ? (
            <p className="mt-2 text-[11px] leading-4 text-gray-400">
              <span className="mr-2 inline-flex h-2.5 w-2.5 rounded-sm bg-green-300 align-middle" />
              已打标
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-amber-300 align-middle" />
              缺标签
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-gray-200 align-middle" />
              未打标
            </p>
          ) : (
            <p className="mt-2 text-[11px] leading-4 text-gray-400">
              <span className="mr-2 inline-flex h-2.5 w-2.5 rounded-sm bg-green-300 align-middle ring-1 ring-green-700" />
              已确认
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-gray-200 align-middle ring-1 ring-gray-400" />
              待补（难度／主标签）
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-amber-300 align-middle" />
              部分确认
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-green-100 align-middle ring-1 ring-green-300" />
              待确认
            </p>
          )}
        </div>
      )}
    </aside>
  )
}
