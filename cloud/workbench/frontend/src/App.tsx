import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { AuthUser, Paper } from './api'
import { api, authApi } from './api'
import LoginPage, { ChangePasswordPage } from './components/LoginPage'
import AppRoutes from './routes'

/**
 * 应用外壳（2026-11-20 路由化，方案 docs/136）。
 *
 * 这里只做四件事：探测身份、拉卷列表、管登录/改密这两个整页、把剩下的交给路由表。
 * 打标台本体在 pages/WorkbenchPage.tsx（原来 1620 行的 App.tsx 整块搬过去）。
 *
 * ⚠️ 登录页与改密页**不进路由表**，是整页 return。这样登录期间地址栏一个字不动，
 *    登录成功后路由自动按原地址渲染 —— "粘链接 → 要登录 → 登录完直接到那道题"
 *    是免费得到的，不需要 ?next= 参数。详见 docs/136 §3.9。
 */
export default function App() {
  const navigate = useNavigate()

  const [me, setMe] = useState<AuthUser | null>(null)
  /** 首次挂载时还不知道登没登录：先显示一屏空白，避免闪一下登录页再跳回来 */
  const [booting, setBooting] = useState(true)
  /** 主动改密（非强制）时的整页切换 */
  const [changingPw, setChangingPw] = useState(false)

  // 卷列表放壳里：路由切换会重挂页面组件，放页面里就成了"每次进打标台重新拉 1200 卷"
  const [papers, setPapers] = useState<Paper[]>([])
  const [papersErr, setPapersErr] = useState<string | null>(null)

  /** 改密成功后的轻提示（页面已切走，用固定定位的小条，2.5s 自动消失） */
  const [flash, setFlash] = useState<string | null>(null)
  const flashTimer = useRef<number | undefined>(undefined)
  const showFlash = useCallback((text: string) => {
    setFlash(text)
    window.clearTimeout(flashTimer.current)
    flashTimer.current = window.setTimeout(() => setFlash(null), 2500)
  }, [])

  // ── 身份探测（只跑一次）──
  // 注意：默认学科/学段不再在这里改 —— 那是打标台的事，而且必须等 URL 有定论
  // （带卷时以 URL 为准）。见 WorkbenchPage 里的同名 effect。
  useEffect(() => {
    let cancelled = false
    authApi
      .me()
      .then((u) => {
        if (!cancelled) setMe(u)
      })
      .catch(() => {
        /* 401 → me 保持 null，下面渲染登录页 */
      })
      .finally(() => {
        if (!cancelled) setBooting(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 会话过期（任意接口返回 401）→ 退回登录页，并清掉上一段会话的数据。
  // 地址栏不动，所以重新登录后还能回到原来那道题。
  useEffect(() => {
    const onUnauth = () => {
      setMe(null)
      setPapers([])
      setPapersErr(null)
    }
    window.addEventListener('qb-unauthorized', onUnauth)
    return () => window.removeEventListener('qb-unauthorized', onUnauth)
  }, [])

  // 拉卷列表。
  // ⚠️ 依赖必须带 must_change_password —— 只写 me?.username 会踩这个坑：
  //    登录后 me 有值但还停在「强制改密」页，这个 effect 立刻发 /api/papers 拿到 428，
  //    弹一句「加载卷列表失败：首次登录，请先修改初始密码」；改密成功后用户名没变，
  //    effect 不重跑 → 打标台里卷列表一直是空的，只能刷页面才好。
  useEffect(() => {
    if (!me || me.must_change_password) return
    api
      .papers()
      .then((ps) => {
        setPapers(ps)
        setPapersErr(null)
      })
      .catch((e: Error) => setPapersErr(`加载卷列表失败：${e.message}`))
  }, [me?.username, me?.must_change_password]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleLogout = useCallback(async () => {
    try {
      await authApi.logout()
    } catch {
      /* 即使请求失败也把本地状态清掉 */
    }
    setMe(null)
    setPapers([])
    setPapersErr(null)
    // 退出后回到干净的 /annotate：别把上一个人的深链留在地址栏里
    navigate('/', { replace: true })
  }, [navigate])

  // ── 身份闸门：未登录只渲染登录页，首次登录强制改密 ──
  if (booting) {
    return (
      <div className="grid h-screen place-items-center bg-white text-sm text-gray-400">
        正在加载…
      </div>
    )
  }
  if (!me) {
    return <LoginPage onLogin={setMe} />
  }
  if (me.must_change_password) {
    return (
      <ChangePasswordPage
        user={me}
        onDone={(u) => {
          setMe(u)
        }}
      />
    )
  }
  if (changingPw) {
    return (
      <ChangePasswordPage
        user={me}
        onDone={(u) => {
          setMe(u)
          setChangingPw(false)
          showFlash('密码已修改')
        }}
        onCancel={() => setChangingPw(false)}
      />
    )
  }

  return (
    <>
      {flash && (
        <div className="fixed right-4 top-4 z-50 rounded-md bg-green-100 px-3 py-1.5 text-xs text-green-800 shadow-sm">
          {flash}
        </div>
      )}
      <AppRoutes
        me={me}
        papers={papers}
        setPapers={setPapers}
        papersErr={papersErr}
        onChangePassword={() => setChangingPw(true)}
        onLogout={handleLogout}
      />
    </>
  )
}
