import { useEffect, useRef, useState } from 'react'

interface Props {
  /** 当前面板宽度（px） */
  width: number
  min: number
  max: number
  /** 双击恢复的宽度 */
  defaultWidth: number
  onChange: (w: number) => void
}

/**
 * 竖直拖拽分隔条：放在「中间题图区」与「右侧面板」之间。
 * 向右拖 = 右侧面板变窄，向左拖 = 变宽。双击恢复默认宽度。
 */
export default function ResizeHandle({ width, min, max, defaultWidth, onChange }: Props) {
  const [dragging, setDragging] = useState(false)
  const start = useRef({ x: 0, w: 0 })

  // 拖拽期间禁用全局文本选择，避免选中页面内容
  useEffect(() => {
    if (!dragging) return
    const prev = document.body.style.userSelect
    document.body.style.userSelect = 'none'
    return () => {
      document.body.style.userSelect = prev
    }
  }, [dragging])

  const clamp = (w: number) => Math.min(max, Math.max(min, w))

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="调整右侧面板宽度"
      title="拖动调整右侧面板宽度（双击恢复默认）"
      onPointerDown={(e) => {
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        start.current = { x: e.clientX, w: width }
        setDragging(true)
      }}
      onPointerMove={(e) => {
        if (!dragging) return
        onChange(clamp(start.current.w - (e.clientX - start.current.x)))
      }}
      onPointerUp={(e) => {
        if (!dragging) return
        e.currentTarget.releasePointerCapture(e.pointerId)
        setDragging(false)
      }}
      onPointerCancel={() => setDragging(false)}
      onDoubleClick={() => onChange(clamp(defaultWidth))}
      className={`relative z-10 w-1.5 shrink-0 cursor-col-resize transition-colors ${
        dragging ? 'bg-blue-500' : 'bg-transparent hover:bg-blue-300'
      }`}
    />
  )
}
