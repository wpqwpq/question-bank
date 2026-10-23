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
  /** 本卷被标记为「有误」的题数 */
  flagged_count?: number
  source?: string
}

export interface QuestionSummary {
  qid: string
  question_number: string
  status: Status
  total_marks: number | null
  question_type?: string
  tags: string[]
  /** 已解析的主标签（plans/主标签实施方案 §2）；切分题在主题层是 null，逐问看子题文件 */
  primary_tag?: string | null
  difficulty: string | null
  /** 全部单元都有 ≥1 标签（切分题按小问聚合）——「齐备」判据，也用于确认门禁 */
  tags_ready?: boolean
  /**
   * 任意一个单元有 ≥1 标签（切分题按小问看）——**预览列表的可见性判据**。
   * 2026-10-18 起：小问允许跳过保存，只要有一问打了标，这道题就该出现在预览里
   * （显示为「待补齐」灰色，可回打标模式补完）。
   */
  any_tagged?: boolean
  /** 全部单元都有难度——缺则预览里显示为「待补难度（灰）」 */
  difficulty_ready?: boolean
  /** 全部单元都能解析出主标签（≥2 标签却无主 → false）——确认门禁之一 */
  primary_ready?: boolean
  split?: boolean
  sub_labels?: string[]
  /** 审核确认进度（仅预览模式展示） */
  verify_state?: VerifyState
  confirmed_units?: number
  total_units?: number
  /** 是否被标记为「有误」（审核意见，冻结该题的一切修改） */
  flagged?: boolean
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
  /** 已解析的主标签；多标签却无主时为 null（plans/主标签实施方案 §2） */
  primary_tag?: string | null
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
  /** 主标签草稿（整题同款规则；1 个标签时即该标签，≥2 必填） */
  primaryTag: string | null
}

export interface SaveBody {
  tags?: string[]
  difficulty?: string | null
  primary_tag?: string | null
  subs?: {
    label: string
    tags: string[]
    difficulty: string | null
    primary_tag?: string | null
  }[]
}

export interface SaveResult {
  qid: string
  tags: string[]
  /** 后端归一后的主标签（单标签也会显式落盘） */
  primary_tag?: string | null
  difficulty: string | null
  status: Status
  split?: boolean
  sub_labels?: string[]
  warnings?: string[]
  /** 本次保存因「标签/难度/主标签有改动」而被自动撤销的确认单元（docs/12 §③） */
  confirm_cleared?: string[]
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
  /** 已解析的主标签（读侧双保险已由后端做过回退） */
  primary_tag: string | null
  /** 主标签是否就绪（切分题按小问聚合）：预览模式确认门禁用 */
  primary_ready?: boolean
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
  /** 是否被标记为「有误」——标了之后后端拒绝一切写入（409） */
  flagged?: boolean
  flagged_at?: string | null
  flagged_by?: string | null
  flagged_note?: string | null
}

// ── 标签管理页（词表整文件读写）──
export interface TaxTag {
  id: string
  zh: string
  en: string
  group: string
  /**
   * IGCSE 专用：该知识点考在哪几份 paper（`p1` / `2c` / `u1` / `4mb1p1`）。
   * A-Level 词表没有这个键；缺失 = 不按 paper 过滤（全可见）。
   */
  papers?: string[]
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
  /** 乐观锁版本号（保存时作为 base_sig 回传） */
  sig: string
}
export interface TaxSaveResp {
  path: string
  backup: string
  units: number
  tags: number
  sig: string
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

// ── 认证（云端版特有）────────────────────────────────────────────────
export type Role = 'admin' | 'lead' | 'tagger' | 'viewer' | 'service'

export interface AuthUser {
  username: string
  name: string
  role: Role
  /** 学科 token，与 data/<board>/<qual>/<subject>/ 的目录名一致；["*"] = 全部 */
  subjects: string[]
  must_change_password: boolean
}

export interface AuditRecord {
  ts: string
  user: string | null
  name: string | null
  role: string | null
  via: string | null
  action: string
  target: string
  ip: string | null
  detail: Record<string, unknown>
}

/** 401 / 428 单独成一类，App 据此弹登录页或强制改密页，而不是当普通报错弹 toast */
export class AuthError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'AuthError'
  }
}

export const isRoleAtLeastLead = (r: Role | undefined) => r === 'admin' || r === 'lead'

/** 能不能改标签词表：tagger 也可以（2026-10-19 放开），但学科范围由后端兜住。
 *  viewer 只读；service 是 AI 账号，浏览器不会登录它。 */
export const canEditTaxonomy = (r: Role | undefined) =>
  r === 'admin' || r === 'lead' || r === 'tagger'

/** 409：词表乐观锁冲突——你编辑期间别人保存过。currentSig 是磁盘上的当前版本号。 */
export class ConflictError extends Error {
  currentSig: string
  constructor(message: string, currentSig: string) {
    super(message)
    this.currentSig = currentSig
    this.name = 'ConflictError'
  }
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    // 会话是 cookie，同源必须带上；跨源时不带（也不会有人跨源用）
    credentials: 'same-origin',
    ...init,
  })
  const ctype = res.headers.get('content-type') ?? ''
  if (!res.ok) {
    let detail = res.statusText
    try {
      const j = (await res.json()) as { detail?: unknown }
      if (typeof j.detail === 'string') detail = j.detail
    } catch {
      /* keep statusText */
    }
    if (res.status === 401 || res.status === 428) {
      // 会话过期/未登录 → 通知 App 回到登录页。用全局事件是为了让所有调用点
      // 自动获得这个行为，不用在每个 catch 里各写一遍。
      if (res.status === 401) window.dispatchEvent(new Event('qb-unauthorized'))
      throw new AuthError(res.status, detail)
    }
    if (res.status === 409) {
      throw new ConflictError(detail, res.headers.get('X-QB-Current-Sig') ?? '')
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

/** JSON 请求的公共头（省得每处都写重复的三行） */
const JSON_INIT = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

/** 全局概览：每卷的三态标记。all 与卷列表那个 ✓ 完全同义。 */
export type OverviewState = 'all' | 'some' | 'none'

export interface OverviewPaper {
  id: string
  board: string
  qualification: string
  subject: string
  paper_code: string | null
  year: number | null
  session: string | null
  unit: string | null
  state: OverviewState
  question_count: number
  verified_count: number
  flagged_count: number
}

export interface OverviewResp {
  papers: OverviewPaper[]
  summary: { total: number; all: number; some: number; none: number }
  generated_at: string
}

export const authApi = {
  me: () => req<AuthUser>('/api/auth/me'),
  login: (username: string, password: string) =>
    req<AuthUser>('/api/login', JSON_INIT('POST', { username, password })),
  logout: () => req<{ ok: boolean }>('/api/logout', { method: 'POST' }),
  changePassword: (oldPassword: string, newPassword: string) =>
    req<{ ok: boolean }>(
      '/api/auth/password',
      JSON_INIT('POST', { old_password: oldPassword, new_password: newPassword }),
    ),
  audit: (limit = 200) => req<AuditRecord[]>(`/api/auth/audit?limit=${limit}`),
}

export const api = {
  papers: () => req<Paper[]>('/api/papers'),
  /** 全局概览（仅 admin）。后端有 60 秒缓存，force 用于「刷新」按钮。 */
  overview: (force = false) => req<OverviewResp>(`/api/overview${force ? '?force=1' : ''}`),
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
  /** 标记 / 取消「此题有误」。标记只写审核意见，不动题库文件。 */
  setFlag: (qid: string, flagged: boolean, note?: string | null) =>
    req<{ qid: string; flagged: boolean; at: string | null }>(
      `/api/questions/${qid}/flag`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ flagged, note: note ?? null }),
      },
    ),

  taxonomyTree: () => req<TaxTree>('/api/taxonomy'),
  taxonomyFile: (board: string, qual: string, subject: string) =>
    req<TaxFileResp>(`/api/taxonomy/${board}/${qual}/${subject}`),
  saveTaxonomy: (board: string, qual: string, subject: string, data: TaxFile, baseSig: string) =>
    req<TaxSaveResp>(`/api/taxonomy/${board}/${qual}/${subject}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data, base_sig: baseSig }),
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

/**
 * 主标签规则（plans/主标签实施方案 §2.1）——前端唯一判据，与后端 `_validate_primary` 同口径：
 *   0 个标签 → 不就绪（连标签都没有，谈不上主标签）；
 *   1 个标签 → 天然就绪（它就是主标签，落盘由后端补全）；
 *   ≥2 个    → 必须显式指定一个且必须 ∈ tags。
 */
export function primaryOk(tags: string[], primary: string | null | undefined): boolean {
  if (tags.length === 0) return false
  if (tags.length === 1) return true
  return !!primary && tags.includes(primary)
}

/** 读侧回退：解析某单元「实际生效」的主标签（手改/悬空引用时宁可返回 null，不返回假值） */
export function resolvePrimary(
  tags: string[],
  primary: string | null | undefined,
): string | null {
  if (tags.length === 0) return null
  if (primary && tags.includes(primary)) return primary
  return tags.length === 1 ? tags[0] : null
}

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
 * 预览模式下题号格配色（docs/11 §1 + docs/13 §3.2 + plans/主标签实施方案 §2）。优先级：
 *   已确认（all） > 待补齐（needsPending）> 部分确认（partial）> 待确认。
 * 「待补齐」= 该题有标签但（至少一个单元）缺难度或缺主标签 → 灰，且**不能勾确认框**。
 * 注意：仍守「浅底 + 深字、不用实心色块」的配色规范。
 */
export function verifyColor(s: VerifyState, current: boolean, needsPending = false): string {
  const base =
    'inline-flex items-center justify-center rounded-md border text-sm font-medium transition-colors'
  const ring = current ? ' ring-2 ring-blue-500 ring-offset-1' : ''
  if (s === 'all') return `${base} bg-green-300 text-green-950 border-green-700 font-semibold${ring}`
  if (needsPending) return `${base} bg-gray-100 text-gray-500 border-gray-300${ring}`
  if (s === 'partial') return `${base} bg-amber-100 text-amber-800 border-amber-400${ring}`
  return `${base} bg-green-100 text-green-800 border-green-300${ring}`
}
