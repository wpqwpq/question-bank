import type { Dispatch, ReactNode, SetStateAction } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import type { AuthUser, Paper } from './api'
import { canAudit, canEditTaxonomy, canOverview } from './api'
import AssignmentPage from './pages/AssignmentPage'
import AuditPage from './pages/AuditPage'
import NotFound from './pages/NotFound'
import OverviewPage from './pages/OverviewPage'
import TaxonomyPage from './pages/TaxonomyPage'
import WorkbenchPage from './pages/WorkbenchPage'

/**
 * 路由表（docs/136）。
 *
 * 登录页 / 强制改密页 **不在这里** —— 它们是 App 壳里的整页 return。
 * 这不是偷懒：正因为登录时地址栏一个字不动，登录成功后路由才能自动按原地址渲染，
 * 「粘链接 → 要求登录 → 登录完直接到那道题」才不需要 `?next=` 参数（也就没有
 * 开放重定向那类坑）。
 *
 * 这里的角色守卫只负责「别让人看见一个 403 页面」；真正的闸门在每个接口里
 * （auth.can_level / subjects / WRITE_ROLES / OVERVIEW_ROLES …）。
 */

interface Props {
  me: AuthUser
  papers: Paper[]
  setPapers: Dispatch<SetStateAction<Paper[]>>
  papersErr: string | null
  onChangePassword: () => void
  onLogout: () => void
}

/** 角色不够 → 弹回打标台，并带一句提示过去（由 WorkbenchPage 读 location.state 弹 toast） */
function RequireRole({ ok, children }: { ok: boolean; children: ReactNode }) {
  if (!ok) {
    return (
      <Navigate to="/annotate" replace state={{ flash: '没有权限访问该页面（当前账号角色不足）' }} />
    )
  }
  return <>{children}</>
}

export default function AppRoutes({
  me,
  papers,
  setPapers,
  papersErr,
  onChangePassword,
  onLogout,
}: Props) {
  /**
   * 打标台：/annotate 与 /preview 共用 WorkbenchPage（模式由地址栏第一段推导）；
   * 带卷 / 不带卷各一条 —— 不带卷就是「还没选卷」的空白态。
   *
   * ⚠️ 四条 Route 的 element 必须长得一模一样（同一组件、同样的 props），
   *    这样 React 在同一位置只会更新 props 而不是卸载重挂，切模式时
   *    那些「切过去要保留什么」的状态才不会白丢。
   */
  const wb = (
    <WorkbenchPage
      me={me}
      papers={papers}
      setPapers={setPapers}
      papersErr={papersErr}
      onChangePassword={onChangePassword}
      onLogout={onLogout}
    />
  )

  return (
    <Routes>
      <Route path="/" element={<Navigate to="/annotate" replace />} />

      <Route path="/annotate" element={wb} />
      <Route path="/annotate/:board/:qual/:subject/:code" element={wb} />
      <Route path="/preview" element={wb} />
      <Route path="/preview/:board/:qual/:subject/:code" element={wb} />

      <Route
        path="/overview"
        element={
          <RequireRole ok={canOverview(me.role)}>
            <OverviewPage me={me} />
          </RequireRole>
        }
      />
      <Route
        path="/audit"
        element={
          <RequireRole ok={canAudit(me.role)}>
            <AuditPage />
          </RequireRole>
        }
      />
      <Route
        path="/taxonomy"
        element={
          <RequireRole ok={canEditTaxonomy(me.role)}>
            <TaxonomyPage me={me} />
          </RequireRole>
        }
      />
      <Route
        path="/taxonomy/:board/:qual/:subject"
        element={
          <RequireRole ok={canEditTaxonomy(me.role)}>
            <TaxonomyPage me={me} />
          </RequireRole>
        }
      />

      {/* 作业页：占位（2026-11-20）。不设角色守卫 —— 将来学生也要看 */}
      <Route path="/assignments" element={<AssignmentPage />} />
      <Route path="/assignments/:id" element={<AssignmentPage />} />

      <Route path="*" element={<NotFound />} />
    </Routes>
  )
}
