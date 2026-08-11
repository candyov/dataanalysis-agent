/** SSE 事件类型定义 — 对应后端 app/events.py */

export interface SSEEvent {
  type: string
}

export interface StartEvent extends SSEEvent {
  type: 'start'
  message: string
}

export interface DoneEvent extends SSEEvent {
  type: 'done'
  status: 'completed' | 'failed'
  message?: string
}

export interface CompleteEvent extends SSEEvent {
  type: 'complete'
}

export interface ThinkingEvent extends SSEEvent {
  type: 'thinking'
  text: string
}

export interface StreamEvent extends SSEEvent {
  type: 'stream'
  text: string
}

export interface BotEvent extends SSEEvent {
  type: 'bot'
  text: string
  /** 报告分段 (文本段/图表段交替, 图表数据内嵌) — 后端确定性生成, 前端只渲染 */
  segments?: {
    type: 'text' | 'chart'
    text?: string
    title?: string
    chart_type?: string
    echarts_option?: Record<string, any>
  }[]
}

export interface SummaryEvent extends SSEEvent {
  type: 'summary'
  text: string
}

export interface StageEvent extends SSEEvent {
  type: 'stage'
  agent: string
  label: string
}

export interface PlanEvent extends SSEEvent {
  type: 'plan'
  steps: { agent: string; goal?: string }[]
}

export interface StatusEvent extends SSEEvent {
  type: 'status'
  status_type?: string
  text: string
}

export interface ToolCallEvent extends SSEEvent {
  type: 'tool_call'
  tool: string
  agent: string
}

export interface AnalysisResultEvent extends SSEEvent {
  type: 'analysis_result'
  tool: string
  data?: Record<string, any>
}

export interface ChartEvent extends SSEEvent {
  type: 'chart'
  title: string
  chart_type: string
  echarts_option: Record<string, any>
}

export interface TokenSummaryEvent extends SSEEvent {
  type: 'token_summary'
  trace_id: string
  by_agent: Record<string, {
    calls: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    total_elapsed_ms: number
  }>
  totals: {
    calls: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    total_elapsed_ms: number
  }
}

export interface DegradedEvent extends SSEEvent {
  type: 'degraded'
  agent: string
  reason: string
  partial_result?: Record<string, any>
}

export interface ConfirmRequiredEvent extends SSEEvent {
  type: 'confirm_required'
  reason: string
  confidence: number
  options?: string[]
}

export interface ErrorEvent extends SSEEvent {
  type: 'error'
  message: string
}

/** 所有 SSE 事件联合类型 */
export type SSEEventUnion =
  | StartEvent | DoneEvent | CompleteEvent
  | ThinkingEvent | StreamEvent | BotEvent | SummaryEvent
  | StageEvent | PlanEvent | StatusEvent
  | ToolCallEvent | AnalysisResultEvent
  | ChartEvent | TokenSummaryEvent
  | DegradedEvent | ConfirmRequiredEvent
  | ErrorEvent
