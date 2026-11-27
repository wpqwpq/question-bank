import { useNavigate, useParams } from 'react-router-dom'
import type { AuthUser } from '../api'
import TagAdmin from '../components/TagAdmin'

/**
 * 标签词表维护（/taxonomy、/taxonomy/:board/:qual/:subject）。
 *
 * 权限：admin / lead / tagger（`api.canEditTaxonomy` ↔ 后端 `auth.TAX_ROLES`）。
 * 学科段是**筛选器**而不是定位符：带上就从那一科打开，不带就落到默认科。
 *
 * `initialUnit` 不再传：改造前它取「打标台当前卷的 unit」，而现在词表页可能
 * 从任何地方直接进来（分享链接、刷新），没有"当前卷"这回事。TagAdmin 本来就
 * 允许不传（回落第一个单元）。
 */
export default function TaxonomyPage({ me }: { me: AuthUser }) {
  const navigate = useNavigate()
  const params = useParams()
  return (
    <TagAdmin
      initialBoard={params.board ?? 'edexcel'}
      initialQual={params.qual ?? 'a-level'}
      initialSubject={params.subject ?? 'physics'}
      role={me.role}
      onExit={() => navigate('/annotate')}
    />
  )
}
