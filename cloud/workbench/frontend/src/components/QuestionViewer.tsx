import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { QuestionDetail } from '../api'

const ZOOM_MIN = 0.3
const ZOOM_MAX = 4
const ZOOM_STEP = 0.1
const ZOOM_KEY = 'wb.zoom'
/** 图片可用宽度里要扣掉的固定开销：外层 p-4(32) + 图片框 p-2+边框(18)；
 *  再留 2px 余量吸收舍入误差（贴边时会把容器撑出滚动条、引发抖动） */
const CHROME = 52

interface Props {
  detail: QuestionDetail | null
  onPrev: () => void
  onNext: () => void
  hasPrev: boolean
  hasNext: boolean
  /** 覆盖题图（预览模式切分题显示小问图时传入） */
  imageOverride?: string | null
  /** 当前展示的小问标签（仅用于底部提示，如 "a"） */
  subLabel?: string | null
  /** 是否被标记为「有误」（冻结该题的一切修改） */
  flagged?: boolean
  /** 切换「有误」标记 */
  onToggleFlag?: () => void
  /** 有写权限（viewer 只读时置 false → 标记按钮禁用，别让人白点后吃 403） */
  canFlag?: boolean
}

const clampZoom = (z: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 100) / 100))

/**
 * 每张题图量到的**原始像素宽度**（进程内缓存，只存数字）。
 *
 * 为什么需要它：题图宽度要等 onLoad 才量得到。没有缓存时，换题的第一帧
 * `naturalW` 是 0 → 退回 `maxWidth:100%` 按适应宽度铺开，等 onLoad 回来才乘上
 * zoom 缩到 50%。视觉上就是「先闪一下大图再变小」——在 50% 这种缩放下一眼就看见。
 * 命中缓存的话第一帧就能算出正确宽度，完全无跳变。
 * 8200 张图全存下来也就几百 KB，不值得做淘汰。
 */
const widthCache = new Map<string, number>()

/** 读取持久化的缩放比例（跨题、跨刷新保持） */
function loadZoom() {
  const v = Number(localStorage.getItem(ZOOM_KEY))
  return Number.isFinite(v) && v > 0 ? clampZoom(v) : 1
}

export default function QuestionViewer({
  detail,
  onPrev,
  onNext,
  hasPrev,
  hasNext,
  imageOverride,
  subLabel,
  flagged,
  onToggleFlag,
  canFlag = true,
}: Props) {
  const meta = detail?.meta as
    | { paper_code?: string; source?: string; total_marks?: number; question_type?: string }
    | undefined
  const imgSrc = imageOverride ?? detail?.image_url ?? null

  // ── 缩放 ──
  const [zoom, setZoom] = useState(loadZoom)
  const [availW, setAvailW] = useState(0)
  // 原图宽度连同「量的是哪张图」一起记。换图时在渲染期自动失效，
  // ⚠️ 不要再用 effect 去清零：缓存图从 onLoad 到被动 effect 只有几十微秒，
  //    effect 会把刚量到的宽度冲回 0，之后图不再触发 load → 缩放永久失效（已复现）。
  const [measured, setMeasured] = useState<{ src: string | null; w: number }>({ src: null, w: 0 })
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    try {
      localStorage.setItem(ZOOM_KEY, String(zoom))
    } catch {
      /* 隐私模式下忽略 */
    }
  }, [zoom])

  // 题图区可视宽度（随窗口 / 右侧面板拖拽变化）
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const update = () => setAvailW(el.clientWidth)
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [imgSrc])

  // Ctrl/⌘ + 滚轮缩放（用原生监听以便 preventDefault 掉浏览器整页缩放）
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      e.preventDefault()
      setZoom((z) => clampZoom(z + (e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP)))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [imgSrc])

  // 100% = 适应宽度（与改动前的默认表现一致）；放大后超出部分由外层容器横向滚动
  // 量到的不是当前这张图 → 先查宽度缓存（换题首帧就能给出正确尺寸，避免闪一下大图）
  const measuredW = measured.src === imgSrc ? measured.w : 0
  const naturalW = measuredW > 0 ? measuredW : (imgSrc ? widthCache.get(imgSrc) || 0 : 0)
  const fitW = availW > 0 ? Math.max(80, availW - CHROME) : 0
  const imgW = naturalW > 0 && fitW > 0 ? Math.round(Math.min(naturalW, fitW) * zoom) : 0
  // 宽度还不知道（首次看这张图）时，先按「适应宽度 × 当前缩放」占位并**隐藏**。
  // 这个预测在题图（约 1654px 宽的整页扫描）上几乎总是精确值，所以显形时不会有位移；
  // 万一真值更小，也只是从占位尺寸收一点，而不是从「100% 大图」缩回来。
  const placeholderW = imgW > 0 ? 0 : fitW > 0 ? Math.round(fitW * zoom) : 0
  const boxW = imgW > 0 ? imgW : placeholderW

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* 题图区：外面这层 relative 的 wrapper 只用来给右下角的缩放控件定位。
          ⚠️ 控件不能放进滚动容器里（absolute 相对滚动内容定位，会跟着图一起滚走），
          也不能挂到最外层（bottom 会落到「上一题/下一题」那条栏上）。 */}
      <div className="relative min-h-0 flex-1">
        {/* scrollbar-gutter:stable —— 给垂直滚动条预留固定槽位，它的出现/消失不再改变
            clientWidth。否则会形成反馈循环：图片宽度 ← 容器宽度（clientWidth）← 有无滚动条
            ← 图片高度（等比缩放）→ 图片宽度…，题图区会疯狂抖动、看到残影。 */}
        <div
          ref={scrollRef}
          className="h-full overflow-auto bg-gray-100 p-4 [scrollbar-gutter:stable]"
        >
          {imgSrc ? (
            <div className={`flex justify-center ${boxW > 0 ? 'min-w-fit' : 'min-w-0'}`}>
              <div className="w-fit rounded-lg border border-gray-300 bg-white p-2">
                <img
                  src={imgSrc}
                  alt={`第 ${String(detail?.meta.question_number)} 题${subLabel ? ` ${subLabel} 问` : ''}`}
                  onLoad={(e) => {
                    const w = e.currentTarget.naturalWidth
                    if (imgSrc && w > 0) widthCache.set(imgSrc, w)
                    setMeasured({ src: imgSrc, w })
                  }}
                  // 尺寸未知时**不显示**（visibility:hidden 仍会加载、仍占位），
                  // 绝不让浏览器先按 100% 画一帧再缩小。
                  style={
                    imgW > 0
                      ? { width: imgW }
                      : placeholderW > 0
                        ? { width: placeholderW, visibility: 'hidden' }
                        : { maxWidth: '100%', visibility: 'hidden' }
                  }
                  className="block max-w-none"
                />
              </div>
            </div>
          ) : (
            <div className="grid h-full place-items-center text-sm text-gray-400">该题没有配图</div>
          )}
          {detail && !subLabel && (
            <details className="mx-auto mt-2 w-fit max-w-full text-xs text-gray-500">
              <summary className="cursor-pointer select-none">题干文本（提取自 PDF 文本层）</summary>
              <pre className="mt-1 max-w-3xl whitespace-pre-wrap rounded bg-white p-2 font-mono leading-5">
                {detail.body.trim()}
              </pre>
            </details>
          )}
        </div>

        {/* 缩放按钮组：浮在题图区右下角，不占布局高度。
            right-5 而不是 right-3 —— 让开 Windows 那条约 15px 的滚动条槽位。 */}
        {imgSrc && (
          <div className="absolute bottom-3 right-5 z-10 flex items-center gap-0.5 rounded-lg border border-gray-300 bg-white/95 px-1 py-0.5 shadow-sm">
            <button
              type="button"
              title="缩小（Ctrl + 滚轮）"
              disabled={zoom <= ZOOM_MIN}
              onClick={() => setZoom((z) => clampZoom(z - ZOOM_STEP))}
              className="grid h-7 w-7 place-items-center rounded-md text-base leading-none text-gray-600 transition-colors enabled:hover:bg-gray-100 enabled:active:bg-gray-200 disabled:opacity-30"
            >
              −
            </button>
            <button
              type="button"
              title="恢复到适应宽度（100%）"
              onClick={() => setZoom(1)}
              className="h-7 w-11 rounded-md text-xs tabular-nums text-gray-600 transition-colors hover:bg-gray-100 active:bg-gray-200"
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              title="放大（Ctrl + 滚轮）"
              disabled={zoom >= ZOOM_MAX}
              onClick={() => setZoom((z) => clampZoom(z + ZOOM_STEP))}
              className="grid h-7 w-7 place-items-center rounded-md text-base leading-none text-gray-600 transition-colors enabled:hover:bg-gray-100 enabled:active:bg-gray-200 disabled:opacity-30"
            >
              +
            </button>
          </div>
        )}
      </div>

      {/* 「有误」横幅：标记期间右侧面板整体冻结，这里说明原因，别让人以为是卡了 */}
      {flagged && (
        <div className="flex shrink-0 items-center gap-2 border-t border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
          <span className="font-medium">此题已标记为「有误」</span>
          <span className="text-red-500/90">
            打标、难度、切分与保存全部停用，右侧面板不可操作；重新上传这道题后标记会自动消失。
          </span>
        </div>
      )}

      <div className="flex items-center justify-between border-t border-gray-200 bg-white px-4 py-2">
        <button
          type="button"
          disabled={!hasPrev}
          onClick={onPrev}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 enabled:hover:bg-gray-100 disabled:opacity-40"
        >
          ◀ 上一题
        </button>
        <div className="text-sm text-gray-600">
          {detail ? (
            <>
              <span className="font-medium text-gray-900">
                第 {String(detail.meta.question_number)} 题
              </span>
              {subLabel && <span className="ml-1 font-medium text-blue-600">{subLabel} 问</span>}
              <span className="ml-2 text-gray-400">
                {meta?.paper_code}
                {meta?.total_marks != null && ` · ${meta.total_marks} 分`}
                {meta?.question_type && ` · ${meta.question_type}`}
              </span>
            </>
          ) : (
            '—'
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* 此题有误：点一下冻结本题并跳到下一题；再点一下取消 */}
          <button
            type="button"
            data-flag="1"
            disabled={!detail || !onToggleFlag || !canFlag}
            onClick={onToggleFlag}
            title={
              !canFlag
                ? '只读账号不能标记「有误」'
                : flagged
                  ? '取消「有误」标记，恢复这道题的修改权限'
                  : '标记此题有误：会冻结该题的全部修改并跳到下一题；题目重新上传后标记自动消失'
            }
            className={
              flagged
                ? 'rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 transition-colors hover:bg-red-100 disabled:opacity-40'
                : 'rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-600 transition-colors hover:border-red-300 hover:bg-red-50 hover:text-red-700 disabled:opacity-40'
            }
          >
            {flagged ? '⚠ 已标有误 · 取消' : '⚠ 此题有误'}
          </button>
          <button
            type="button"
            disabled={!hasNext}
            onClick={onNext}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 enabled:hover:bg-gray-100 disabled:opacity-40"
          >
            下一题 ▶
          </button>
        </div>
      </div>
    </div>
  )
}
