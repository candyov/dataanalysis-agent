/** 前端聊天消息类型定义 */

export type MessageRole =
  | 'user'
  | 'bot'
  | 'stream'
  | 'thinking'
  | 'tool'
  | 'chart'
  | 'summary'
  | 'status'
  | 'report'

/** 报告内联段: 文本段 + 图表段 (报告与图表整合为一个视觉单元) */
export interface ReportSegment {
  type: 'text' | 'chart'
  text?: string
  title?: string
  chartType?: string
  echartsOption?: Record<string, any>
}

export interface ChatMessage {
  id: string
  role: MessageRole
  text?: string
  /** 报告内联段 (bot 消息: 文本 + 内嵌图表) */
  reportSegments?: ReportSegment[]
  /** thinking 消息 */
  thinkingText?: string
  thinkingExpanded?: boolean
  /** tool 消息 */
  tool?: string
  agent?: string
  toolResult?: any
  /** chart 消息 */
  chartTitle?: string
  chartType?: string
  echartsOption?: Record<string, any>
  /** report 消息 — Reporter 输出的 HTML 仪表盘 */
  htmlReport?: string
  /** status 消息 */
  statusType?: 'info' | 'warning' | 'error'
  /** user 消息的文件上下文 */
  filePath?: string
  /** 降级标记 */
  degraded?: boolean
  /** token 统计 */
  tokenInfo?: { prompt: number; completion: number; total: number }
  /** 是否可重试 */
  retryable?: boolean
}

export interface PipelineStep {
  key: string
  label: string
  status: 'pending' | 'active' | 'done'
}

export interface SessionSummary {
  session_id: string
  first_message: string
  msg_count: number
  last_access: number
}

export interface UploadedFile {
  path: string
  name: string
  size?: number
}
