import { useNavigate } from 'react-router-dom'

/**
 * 作业页 —— 占位（2026-11-20）。
 *
 * 只放一句说明和一个出口。打标台没有入口，后端也没有对应接口 ——
 * 先占住 `/assignments/:id` 这个地址形状，将来做作业功能时不用再改链接。
 *
 * 权限刻意只要求「已登录」（App 的登录闸门在前），不设角色守卫：
 * 这个页面将来大概率要给学生看，学生不该被 tagger 之类的条件挡在外面。
 */
export default function AssignmentPage() {
  const navigate = useNavigate()

  return (
    <div className="grid min-h-screen place-items-center bg-white px-4 text-gray-900">
      <div className="text-center">
        <p className="text-sm text-gray-600">这里将来是作业页</p>
        <button
          type="button"
          onClick={() => navigate('/annotate')}
          className="mt-4 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700"
        >
          回到打标台
        </button>
      </div>
    </div>
  )
}
