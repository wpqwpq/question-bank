import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { QuestionDetail } from '../api'

const ZOOM_MIN = 0.3
const ZOOM_MAX = 4
const ZOOM_STEP = 0.1
const ZOOM_KEY = 'wb.zoom'
/** 图片可用宽度里要扣掉的固定开销：外层 p-4(32) + 图片框 p-2+边框(18) */
const CHROME = 50

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
}

const clampZoom = (z: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(z * 100) / 100))

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
}: Props) {
  const meta = detail?.meta as
    | { paper_code?: string; source?: string; total_marks?: number; question_type?: string }
    | undefined
  const imgSrc = imageOverride ?? detail?.image_url ?? null

  // ── 缩放 ──
  const [zoom, setZoom] = useState(loadZoom)
  const [availW, setAvailW] = useState(0)
  const [naturalW, setNaturalW] = useState(0)
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

  // 换图重测原图宽度
  useEffect(() => setNaturalW(0), [imgSrc])

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
  const fitW = availW > 0 ? Math.max(80, availW - CHROME) : 0
  const imgW = naturalW > 0 && fitW > 0 ? Math.round(Math.min(naturalW, fitW) * zoom) : 0

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto bg-gray-100 p-4">
        {imgSrc ? (
          <div className="flex min-w-fit justify-center">
            <div className="w-fit rounded-lg border border-gray-300 bg-white p-2">
              <img
                src={imgSrc}
                alt={`第 ${String(detail?.meta.question_number)} 题${subLabel ? ` ${subLabel} 问` : ''}`}
                onLoad={(e) => setNaturalW(e.currentTarget.naturalWidth)}
                style={imgW > 0 ? { width: imgW } : { maxWidth: '100%' }}
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

      {/* 缩放按钮组：浮在题图区右上角，不占布局高度 */}
      {imgSrc && (
        <div className="absolute right-3 top-3 z-10 flex items-center gap-0.5 rounded-lg border border-gray-300 bg-white/95 px-1 py-0.5 shadow-sm">
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
  )
}
