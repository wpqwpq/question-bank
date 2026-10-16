export type Status = 'none' | 'partial' | 'done'

export interface Paper {
  id: string
  board: string
  qualification: string
  subject: string
  paper_code: string
  year: number
  session: string
  unit: string
  question_count: number
  annotated_count: number
  source?: string
}

export interface QuestionSummary {
  qid: string
  question_number: string
  status: Status
  total_marks: number | null
  question_type?: string
  tags: string[]
  difficulty: string | null
  split?: boolean
  sub_labels?: string[]
}

export interface TagDef {
  id: string
  zh: string
  en?: string
  group?: string
}

export interface GroupDef {
  id: string
  zh: string
  en?: string
  /** 跨单元卷（如 AQA U5）才有：该组所属单元，用于在自身单元与其他单元之间插分隔 */
  unit?: string
}

export interface SubInfo {
  qid: string
  label: string
  tags: string[]
  difficulty: string | null
  status: Status
  image_url?: string | null
}

export interface SubDraft {
  label: string
  tags: string[]
  difficulty: string | null
}

export interface SaveBody {
  tags?: string[]
  difficulty?: string
  subs?: { label: string; tags: string[]; difficulty: string }[]
}

export interface SaveResult {
  qid: string
  tags: string[]
  difficulty: string | null
  status: Status
  split?: boolean
  sub_labels?: string[]
  warnings?: string[]
}

export interface QuestionDetail {
  qid: string
  paper_id: string
  unit: string
  meta: Record<string, unknown>
  body: string
  image_url: string | null
  tags: string[]
  difficulty: string | null
  available_tags: TagDef[]
  available_groups: GroupDef[]
  split_eligible: boolean
  top_labels: string[]
  split_active: boolean
  subs: SubInfo[]
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = (await res.json()) as { detail?: string }
      detail = j.detail ?? detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  papers: () => req<Paper[]>('/api/papers'),
  questions: (paperId: string) => req<QuestionSummary[]>(`/api/papers/${paperId}/questions`),
  question: (qid: string) => req<QuestionDetail>(`/api/questions/${qid}`),
  saveAnnotation: (qid: string, body: SaveBody) =>
    req<SaveResult>(`/api/questions/${qid}/annotation`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
}

export const SESSION_LABELS: Record<string, string> = {
  jan: '1月',
  mar: '3月',
  'may-jun': '5-6月',
  june: '6月',
  oct: '10月',
  'oct-nov': '10-11月',
}

export const DIFFICULTY_LABELS: ReadonlyArray<readonly [string, string, string]> = [
  ['very-easy', '极易', 'bg-emerald-100 text-emerald-800 border-emerald-400 hover:bg-emerald-200'],
  ['easy', '简单', 'bg-green-100 text-green-800 border-green-400 hover:bg-green-200'],
  ['medium', '中等', 'bg-amber-100 text-amber-800 border-amber-400 hover:bg-amber-200'],
  ['hard', '困难', 'bg-orange-100 text-orange-800 border-orange-400 hover:bg-orange-200'],
  ['very-hard', '极难', 'bg-red-100 text-red-800 border-red-400 hover:bg-red-200'],
]

export function statusColor(s: Status, current: boolean): string {
  const base =
    'inline-flex items-center justify-center rounded-md border text-sm font-medium transition-colors'
  const ring = current ? ' ring-2 ring-blue-500 ring-offset-1' : ''
  switch (s) {
    case 'done':
      return `${base} bg-green-100 text-green-800 border-green-300${ring}`
    case 'partial':
      return `${base} bg-amber-100 text-amber-800 border-amber-300${ring}`
    default:
      return `${base} bg-gray-100 text-gray-500 border-gray-200${ring}`
  }
}
