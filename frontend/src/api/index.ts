/** 后端 API 通信层 — SSE 流式 + REST */
import type { SSEEventUnion } from '@/types/events'

const BASE = import.meta.env.VITE_API_BASE || '/api/v1'
// 单用户 API Key 鉴权 (与后端 APP_API_KEY 对应, 未配置则不携带)
const API_KEY = import.meta.env.VITE_API_KEY || ''

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return API_KEY ? { ...extra, 'X-API-Key': API_KEY } : extra
}

export async function* sendMessage(
  message: string,
  sourceId = '',
  sessionId = '',
  signal?: AbortSignal | null,
  confirmation = '',
): AsyncGenerator<SSEEventUnion> {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({
      message,
      source_id: sourceId,
      session_id: sessionId,
      confirmation,
    }),
    signal: signal || undefined,
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          yield JSON.parse(line.slice(6)) as SSEEventUnion
        } catch { /* skip malformed */ }
      }
    }
  }
}

/** 列出数据源 */
export async function listDatasources(): Promise<{ datasources: { id: string; name: string; db_type: string }[] }> {
  const res = await fetch(`${BASE}/datasources`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

/** 获取数据源详情 */
export async function getDatasourceInfo(sourceId: string): Promise<{ table_count: number; total_rows: number }> {
  const res = await fetch(`${BASE}/datasources/${encodeURIComponent(sourceId)}/info`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function listSessions(): Promise<{ sessions: any[] }> {
  const res = await fetch(`${BASE}/sessions`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function getSession(sessionId: string): Promise<any> {
  const res = await fetch(`${BASE}/sessions/${sessionId}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function deleteSession(sessionId: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/sessions/${sessionId}`, { method: 'DELETE', headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export function getReportUrl(sessionId: string): string {
  return `${BASE}/sessions/${sessionId}/report`
}

/** 上传 CSV/Excel 文件 → 后端导入为临时数据源 */
export async function uploadFile(file: File): Promise<{
  status: string
  source_id: string
  name: string
  rows: number
  columns: string[]
}> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/datasources/upload`, {
    method: 'POST',
    body: form,
    headers: authHeaders(),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `Upload failed: ${res.status}`)
  }
  return await res.json()
}

/** 删除数据源 */
export async function deleteDataSource(sourceId: string): Promise<void> {
  const res = await fetch(`${BASE}/datasources/${encodeURIComponent(sourceId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
}

/** 测试数据源连接 (不落库) */
export async function testDatasource(body: Record<string, any>): Promise<{ ok: boolean; latency_ms: number; message: string }> {
  const res = await fetch(`${BASE}/datasources/test`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  return await res.json()
}

/** 注册数据源连接 (非 SQLite 后端会先测试连接) */
export async function createDatasource(body: Record<string, any>): Promise<{ status: string; id: string }> {
  const res = await fetch(`${BASE}/datasources`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return await res.json()
}

/** 更新数据源 (密码留空 = 不变) */
export async function updateDatasource(id: string, body: Record<string, any>): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/datasources/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return await res.json()
}

/** 启用/禁用数据源 */
export async function toggleDatasource(id: string): Promise<{ status: string; enabled: boolean }> {
  const res = await fetch(`${BASE}/datasources/${encodeURIComponent(id)}/toggle`, {
    method: 'POST',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

// ── 文件管理 ──

export interface FileInfo {
  source_id: string
  name: string
  size: number
  rows: number
  columns: number
  db_type: string
  enabled: boolean
  created_at: number
}

/** 上传文件列表 */
export async function listFiles(): Promise<{ files: FileInfo[] }> {
  const res = await fetch(`${BASE}/files`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

/** 删除上传文件 (数据源 + 表 + 物理文件) */
export async function deleteFile(sourceId: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/files/${encodeURIComponent(sourceId)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

// ── 设置 ──

/** 读设置 (敏感项脱敏) */
export async function getSettings(): Promise<any> {
  const res = await fetch(`${BASE}/settings`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

/** 更新设置 (API Key 留空 = 不变) */
export async function updateSettings(body: Record<string, any>): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/settings`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

/** 测试模型连接 */
export async function testModelConnection(): Promise<{ ok: boolean; message: string }> {
  const res = await fetch(`${BASE}/settings/test`, {
    method: 'POST',
    headers: authHeaders(),
  })
  return await res.json()
}

// ── 模型档案 (多模型切换) ──

export interface ModelProfile {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  api_key_set: boolean
  temperature: number
}

export async function listModels(): Promise<{
  profiles: ModelProfile[]
  active_id: string
  presets: Record<string, { base_url: string; default_model: string }>
}> {
  const res = await fetch(`${BASE}/models`, { headers: authHeaders() })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function createModel(body: Record<string, any>): Promise<{ status: string; id: string }> {
  const res = await fetch(`${BASE}/models`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function updateModel(id: string, body: Record<string, any>): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/models/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function deleteModel(id: string): Promise<{ status: string }> {
  const res = await fetch(`${BASE}/models/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function activateModel(id: string): Promise<{ status: string; active_id: string }> {
  const res = await fetch(`${BASE}/models/${encodeURIComponent(id)}/activate`, {
    method: 'POST',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return await res.json()
}

export async function testModelProfile(id: string): Promise<{ ok: boolean; message: string }> {
  const res = await fetch(`${BASE}/models/${encodeURIComponent(id)}/test`, {
    method: 'POST',
    headers: authHeaders(),
  })
  return await res.json()
}
