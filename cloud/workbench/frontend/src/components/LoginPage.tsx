import { useEffect, useRef, useState } from 'react'
import type { AuthUser } from '../api'
import { AuthError, authApi } from '../api'

/**
 * 登录页。云端版的唯一入口——所有 /api 与 /files 都要身份。
 *
 * 有意做得极简：这是老师每天上课前要用的东西，不是演示页。
 * 只有三个要素：用户名、密码、登录按钮，错了就说清楚错在哪。
 */
export default function LoginPage({ onLogin }: { onLogin: (u: AuthUser) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const userRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    userRef.current?.focus()
  }, [])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setErr('')
    setBusy(true)
    try {
      const u = await authApi.login(username.trim(), password)
      onLogin(u)
    } catch (e2) {
      setErr(e2 instanceof AuthError || e2 instanceof Error ? e2.message : '登录失败')
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-gray-50 px-4 text-gray-900">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-lg font-semibold">题库审核打标台</h1>
          <p className="mt-1 text-xs text-gray-500">请使用分配的账号登录</p>
        </div>
        <form
          onSubmit={submit}
          className="space-y-3 rounded-xl border border-gray-200 bg-white p-5 shadow-sm"
        >
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-gray-600">用户名</span>
            <input
              ref={userRef}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
              placeholder="例如 phy01"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-gray-600">密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
              placeholder="初始密码见分发文件"
            />
          </label>
          {err && (
            <p className="rounded-md bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{err}</p>
          )}
          <button
            type="submit"
            disabled={busy || !username || !password}
            className="w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
          >
            {busy ? '登录中…' : '登录'}
          </button>
        </form>
        <p className="mt-4 text-center text-[11px] leading-5 text-gray-400">
          忘记密码请联系管理员重置。
          <br />
          登录状态保持 12 小时，到期需重新登录。
        </p>
      </div>
    </div>
  )
}

/**
 * 改密页。两种进入方式：首次登录强制改（forced）、自己主动改。
 * 强制模式下没有「返回」——不改完进不了打标台。
 */
export function ChangePasswordPage({
  user,
  onDone,
  onCancel,
}: {
  user: AuthUser
  onDone: (u: AuthUser) => void
  onCancel?: () => void
}) {
  const forced = user.must_change_password
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [newPw2, setNewPw2] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setErr('')
    if (newPw !== newPw2) {
      setErr('两次输入的新密码不一致')
      return
    }
    setBusy(true)
    try {
      await authApi.changePassword(oldPw, newPw)
      onDone({ ...user, must_change_password: false })
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : '修改失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-gray-50 px-4 text-gray-900">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-3 rounded-xl border border-gray-200 bg-white p-5 shadow-sm"
      >
        <div className="mb-1">
          <h1 className="text-base font-semibold">
            {forced ? '首次登录，请设置新密码' : '修改密码'}
          </h1>
          <p className="mt-1 text-xs text-gray-500">
            {forced
              ? '初始密码是统一生成的，改掉它才能继续使用。'
              : `${user.name}（${user.username}）`}
          </p>
        </div>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-gray-600">
            {forced ? '初始密码' : '原密码'}
          </span>
          <input
            type="password"
            value={oldPw}
            onChange={(e) => setOldPw(e.target.value)}
            autoComplete="current-password"
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-gray-600">新密码（至少 8 位）</span>
          <input
            type="password"
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
            autoComplete="new-password"
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-gray-600">再输一次</span>
          <input
            type="password"
            value={newPw2}
            onChange={(e) => setNewPw2(e.target.value)}
            autoComplete="new-password"
            className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100"
          />
        </label>
        {err && (
          <p className="rounded-md bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{err}</p>
        )}
        <div className="flex gap-2">
          {!forced && onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm text-gray-700 hover:bg-gray-50"
            >
              取消
            </button>
          )}
          <button
            type="submit"
            disabled={busy || !oldPw || newPw.length < 8 || !newPw2}
            className="flex-1 rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
          >
            {busy ? '提交中…' : '确认修改'}
          </button>
        </div>
      </form>
    </div>
  )
}
