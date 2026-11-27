import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App'

/**
 * history 路由（docs/136）：
 *   - 线上 nginx 是 proxy_pass 全转发，FastAPI 的 `@app.get("/{full_path:path}")`
 *     会把 /annotate/xxx 这类深层路径兜回 index.html，所以后端与 nginx 都不用改；
 *   - 万一哪天津螺配置变了、兜底没了，把 BrowserRouter 换成 HashRouter 就能立刻
 *     退回 #/xxx 形式，代价只有网址难看一点。
 */
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
