import { useNavigate } from 'react-router-dom'
import type { AuthUser } from '../api'
import Overview from '../components/Overview'

/**
 * 全局概览（/overview）。
 *
 * 权限：admin / lead / viewer（`api.canOverview` ↔ 后端 `auth.OVERVIEW_ROLES`）。
 * viewer 于 2026-11-20 由培琪决定放行 —— 概览只返回完成度三态、不含标注内容。
 *
 * 薄壳：真正的 UI 在 components/Overview.tsx，这里只做「路由 ↔ props」的转接。
 */
export default function OverviewPage({ me }: { me: AuthUser }) {
  const navigate = useNavigate()
  return (
    <Overview
      onExit={() => navigate('/annotate')}
      // lead 能看到全库地图，但题目仍受学科隔离：越界的卷按钮置灰，免得白跳一次 404
      canOpen={(p) => me.subjects.includes('*') || me.subjects.includes(p.subject)}
      onOpen={(p) => {
        // 卷目录名 = paper_id 去掉 `board-qual-subject-` 前缀（后端 paper_id_for 的逆运算）
        const code = p.id.slice(`${p.board}-${p.qualification}-${p.subject}-`.length)
        navigate(`/annotate/${p.board}/${p.qualification}/${p.subject}/${code}`)
      }}
    />
  )
}
