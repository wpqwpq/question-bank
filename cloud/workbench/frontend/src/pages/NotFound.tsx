import { useLocation, useNavigate } from 'react-router-dom'

/**
 * 404 —— 地址栏里的路径对不上任何一条路由。
 *
 * 注意：**"卷不存在或无权访问"不走这里**。那种情况路径本身是合法的（路由匹配上了），
 * 是数据层查不到，由 WorkbenchPage 提示后退回 /annotate —— 因为后端对越权回 404
 * 而故意不说"存在但你不能看"，前端也得跟着含糊。
 */
export default function NotFound() {
  const navigate = useNavigate()
  const location = useLocation()
  return (
    <div className="grid h-screen place-items-center bg-white px-4 text-gray-900">
      <div className="w-full max-w-md text-center">
        <p className="text-sm font-medium text-gray-500">404</p>
        <h1 className="mt-2 text-lg font-medium">这个地址不存在</h1>
        <p className="mt-2 break-all rounded-md bg-gray-50 px-3 py-2 font-mono text-xs text-gray-500">
          {location.pathname}
        </p>
        <button
          type="button"
          onClick={() => navigate('/annotate', { replace: true })}
          className="mt-5 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          回到打标台
        </button>
      </div>
    </div>
  )
}
