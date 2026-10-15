import type { Paper, QuestionSummary, Status } from '../api'
import { SESSION_LABELS, statusColor } from '../api'

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
        </select>
      </div>
      <div>
        <Label text={`试卷（${p.visiblePapers.length} 卷）`} />
        <select className={selectCls} value={p.paperId} onChange={(e) => p.onPaper(e.target.value)}>
          <option value="">— 选择试卷 —</option>
          {p.visiblePapers.map((paper) => (
            <option key={paper.id} value={paper.id}>
              {paper.year} {SESSION_LABELS[paper.session] ?? paper.session} · {paper.paper_code}
              （{paper.annotated_count}/{paper.question_count}）
            </option>
          ))}
        </select>
      </div>

      {currentPaper && (
        <div className="mt-1">
          <div className="mb-2 flex items-baseline justify-between">
            <Label text={p.mode === 'preview' ? '已打标题目（点击查看）' : '题号（点击跳题）'} />
            <span className="text-xs font-medium text-gray-600">
              {currentPaper.annotated_count}/{currentPaper.question_count} 已标
            </span>
          </div>
          <div className="grid grid-cols-5 gap-1.5">
            {p.questions.map((q, i) => (
              <button
                key={q.qid}
                type="button"
                title={`第 ${q.question_number} 题 · ${
                  q.split ? `已切分（${(q.sub_labels ?? []).join('/')}）· ` : ''
                }${
                  q.status === 'done'
                    ? `已打标（${q.difficulty ?? '子题齐全'}）`
                    : q.status === 'partial'
                      ? '标签/难度缺其一'
                      : '未打标'
                }`}
                className={`h-8 ${statusColor(q.status as Status, i === p.currentIndex)}`}
                onClick={() => p.onJump(i)}
              >
                {q.question_number}
                {q.split && <span className="ml-0.5 align-super text-[9px] leading-none">abc</span>}
              </button>
            ))}
          </div>
          {p.mode !== 'preview' && (
            <p className="mt-2 text-[11px] leading-4 text-gray-400">
              <span className="mr-2 inline-flex h-2.5 w-2.5 rounded-sm bg-green-300 align-middle" />
              齐全
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-amber-300 align-middle" />
              缺其一
              <span className="mx-2 inline-flex h-2.5 w-2.5 rounded-sm bg-gray-200 align-middle" />
              未打标
            </p>
          )}
        </div>
      )}
    </aside>
  )
}
