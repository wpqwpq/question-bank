export type Status = 'none' | 'partial' | 'done'

/** 审核确认进度（预览模式左侧配色用）：all=全部小问已确认 / partial=部分 / none=未确认 */
export type VerifyState = 'all' | 'partial' | 'none'

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
  /** 全部小问都已确认的主题题数（docs/11） */
  verified_count?: number
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
  /** 全部单元都有 ≥1 标签（切分题按小问聚合）——预览列表判据，docs/13 */
  tags_ready?: boolean
  /** 全部单元都有难度——缺则预览里显示为「待补难度（灰）」 */
  difficulty_ready?: boolean
  split?: boolean
  sub_labels?: string[]
  /** 审核确认进度（仅预览模式展示） */
  verify_state?: VerifyState
  confirmed_units?: number
  total_units?: number
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
  /** 该小问是否已被审核确认 */
  verified?: boolean
}

export interface SubDraft {
  label: string
  tags: string[]
  difficulty: string | null
}

export interface SaveBody {
  tags?: string[]
  difficulty?: string | null
  subs?: { label: string; tags: string[]; difficulty: string | null }[]
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

export interface ConfirmResult {
  qid: string
  confirmed: boolean
  at: string | null
  /** 本次随确认写入的难度（未写则为 null） */
  difficulty?: string | null
  /** 写入难度后重算的题目状态 */
  status?: Status | null
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
  /** 请求的这个 qid（作为确认单元）是否已确认；非切分题即整题确认状态 */
  verified?: boolean
  /** 主题级确认聚合（仅预览模式使用） */
  verify_state?: VerifyState
  confirmed_units?: number
  total_units?: number
}

// ── 标签管理页（词表整文件读写）──
export interface TaxTag {
  id: string
  zh: string
  en: string
  group: string
}
export interface TaxGroup {
  id: string
  zh: string
  en: string
}
export interface TaxUnit {
  groups: TaxGroup[]
  tags: TaxTag[]
}
/** 词表文件：单元键 → 单元；另含 `$comment` 之类的 `$` 元数据键 */
export type TaxFile = Record<string, TaxUnit | string>
export interface TaxFileResp {
  board: string
  qual: string
  subject: string
  path: string
  data: TaxFile
}
export interface TaxSaveResp {
  path: string
  backup: string
  units: number
  tags: number
}
/** 词表目录树：考试局 → 资格 → 学科[]（喂「标签管理」页的三个下拉） */
export type TaxTree = Record<string, Record<string, string[]>>

export const unitKeys = (file: TaxFile): string[] =>
  Object.keys(file).filter((k) => !k.startsWith('$'))

export const asUnit = (file: TaxFile, unit: string): TaxUnit => file[unit] as TaxUnit

/** 英文短语 → url-safe slug（与 scripts/gen_tags_json.py 的 slug() 一致） */
export function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/['’.\u2019]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const ctype = res.headers.get('content-type') ?? ''
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
  // 接口返回 HTML = 请求落到了 SPA 的兜底路由（后端里没有这条 API）。
  // 最常见的原因是打标台后端还在跑旧代码，重启即可；直接抛原始报错会是一句看不懂的
  // "Unexpected token '<' ... is not valid JSON"，所以这里换成能照着做的话。
  if (!ctype.includes('application/json')) {
    throw new Error(`接口 ${url} 返回的不是 JSON —— 打标台后端可能是旧版本，重启一下就好`)
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
  confirmQuestion: (qid: string, confirmed: boolean, difficulty?: string | null) =>
    req<ConfirmResult>(`/api/questions/${qid}/confirm`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmed, difficulty: difficulty ?? null }),
    }),

  taxonomyTree: () => req<TaxTree>('/api/taxonomy'),
  taxonomyFile: (board: string, qual: string, subject: string) =>
    req<TaxFileResp>(`/api/taxonomy/${board}/${qual}/${subject}`),
  saveTaxonomy: (board: string, qual: string, subject: string, data: TaxFile) =>
    req<TaxSaveResp>(`/api/taxonomy/${board}/${qual}/${subject}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data }),
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

/**
 * 预览模式下题号格配色（docs/11 §1 + docs/13 §3.2）。优先级：
 *   已确认（all） > 待补难度（needsDifficulty）> 部分确认（partial）> 待确认。
 * 「待补难度」= 该题有标签但（至少一个单元）没难度 → 灰，且**不能勾确认框**。
 * 注意：仍守「浅底 + 深字、不用实心色块」的配色规范。
 */
export function verifyColor(s: VerifyState, current: boolean, needsDifficulty = false): string {
  const base =
    'inline-flex items-center justify-center rounded-md border text-sm font-medium transition-colors'
  const ring = current ? ' ring-2 ring-blue-500 ring-offset-1' : ''
  if (s === 'all') return `${base} bg-green-300 text-green-950 border-green-700 font-semibold${ring}`
  if (needsDifficulty) return `${base} bg-gray-100 text-gray-500 border-gray-300${ring}`
  if (s === 'partial') return `${base} bg-amber-100 text-amber-800 border-amber-400${ring}`
  return `${base} bg-green-100 text-green-800 border-green-300${ring}`
}
