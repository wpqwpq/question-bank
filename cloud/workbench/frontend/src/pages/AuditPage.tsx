import { useNavigate } from 'react-router-dom'
import AuditLog from '../components/AuditLog'

/**
 * 操作日志（/audit）。仅 admin —— 后端 `main.py` 的 `/api/auth/audit`
 * 另有 `ident.is_admin` 判断，前端守卫只是别让人看见一个 403。
 */
export default function AuditPage() {
  const navigate = useNavigate()
  return <AuditLog onExit={() => navigate('/annotate')} />
}
