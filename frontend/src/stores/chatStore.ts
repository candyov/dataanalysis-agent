/** 聊天/会话状态管理 — 消息流、SSE、会话 CRUD */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { ChatMessage, SessionSummary } from '@/types/message'
import type { SSEEventUnion } from '@/types/events'
import { sendMessage, listSessions, deleteSession, getSession } from '@/api'
import { markdownToHtml } from '@/utils/markdown'
import { TOOL_LABELS } from '@/utils/toolLabels'
import { findChart, buildReportSegments, buildTailCharts } from '@/utils/reportCharts'
import type { ChartEntry } from '@/utils/reportCharts'
import { usePipelineStore } from './pipelineStore'

let _msgId = 0
function nextId(): string {
  return `msg_${++_msgId}`
}

/** 后端 segments → 前端 ReportSegment (snake_case → camelCase) */
function normalizeSegments(raw: any[]): ReportSegment[] {
  return (raw || []).map(s => {
    if (s.type === 'chart') {
      return {
        type: 'chart',
        title: s.title || '',
        chartType: s.chart_type || s.chartType || '',
        echartsOption: s.echarts_option || s.echartsOption || {},
      }
    }
    return { type: 'text', text: s.text || '' }
  })
}

// ── 图表池: 报告内联用 (ChartEvent 入池 → 报告图表引用匹配内嵌) ──
const chartPool = new Map<string, ChartEntry>()

export const useChatStore = defineStore('chat', () => {
  // ── 状态 ──
  const sessionId = ref('sess_' + Date.now())
  const sessions = ref<SessionSummary[]>([])
  const sessionsLoading = ref(true)
  const messages = ref<ChatMessage[]>([])
  const loading = ref(false)
  const toast = ref('')
  const lastInput = ref('')
  const lastSourceId = ref('')
  // 人机协同 (P2): 等待用户确认的请求 (confirm_required 事件触发, 展示选项按钮)
  const pendingConfirm = ref<{ reason: string; confidence: number } | null>(null)

  let abortController: AbortController | null = null

  const visibleMessages = computed(() =>
    messages.value.filter(m =>
      m.role !== 'stream' && !(m.role === 'status' && m.statusType !== 'error')
    )
  )

  // ── 消息工厂 ──
  function addMessage(msg: Partial<ChatMessage> & { role: ChatMessage['role'] }) {
    const m: ChatMessage = {
      id: nextId(),
      text: '',
      ...msg,
    }
    messages.value.push(m)
    return m
  }

  /** 报告消息: 内联图表引用 + 末尾图表兜底清单 — 报告与图始终同屏
   *
   * 后端 segments (BotEvent.segments) 优先: 确定性分段已内嵌图表数据, 前端只渲染,
   * 彻底摆脱标题匹配/图表池依赖。旧逻辑 (chartPool 匹配) 保留为兼容兜底。
   */
  function attachReport(text: string, backendSegments?: ReportSegment[]): ChatMessage {
    if (backendSegments && backendSegments.length) {
      // 后端分段: 图表数据内嵌, 独立 chart 消息全部收敛进报告 (避免重复展示)
      messages.value = messages.value.filter(m => m.role !== 'chart')
      return addMessage({ role: 'bot', text: markdownToHtml(text), reportSegments: normalizeSegments(backendSegments) })
    }
    const { segments, embeddedTitles } = buildReportSegments(text, chartPool)
    if (chartPool.size > 0) {
      const rest = buildTailCharts(chartPool, embeddedTitles)
      if (rest.length) {
        segments.push({ type: 'text', text: '\n\n**图表**' })
        segments.push(...rest)
      }
      // 独立图表消息收敛: 全部进报告 (内联 + 末尾清单)
      messages.value = messages.value.filter(m => m.role !== 'chart')
    }
    return addMessage({ role: 'bot', text: markdownToHtml(text), reportSegments: segments })
  }

  function appendToLast(text: string) {
    const last = messages.value[messages.value.length - 1]
    if (last) last.text = (last.text || '') + text
  }

  function setToast(text: string, duration = 3000) {
    toast.value = text
    setTimeout(() => { toast.value = '' }, duration)
  }

  // ── SSE 流式对话 ──
  async function run(userInput: string, sourceId: string, confirmation = '') {
    if (loading.value || !userInput.trim()) return

    loading.value = true
    lastInput.value = userInput
    lastSourceId.value = sourceId
    pendingConfirm.value = null  // 新请求清除待确认状态
    abortController = new AbortController()
    chartPool.clear()  // 新一轮分析: 图表池重置 (报告接管本轮图表)

    addMessage({ role: 'user', text: userInput })

    try {
      const events = sendMessage(userInput, sourceId, sessionId.value, abortController.signal, confirmation)

      for await (const evt of events as AsyncGenerator<SSEEventUnion>) {
        handleEvent(evt)
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        addMessage({ role: 'status', text: `错误: ${err.message}`, statusType: 'error', retryable: true })
      }
    } finally {
      loading.value = false
      abortController = null
    }
  }

  function retryLast() {
    if (!lastInput.value) return
    stop()
    run(lastInput.value, lastSourceId.value)
  }

  function stop() {
    abortController?.abort()
    loading.value = false
  }

  function handleEvent(evt: SSEEventUnion) {
    const pipeline = usePipelineStore()
    switch (evt.type) {
      case 'thinking': {
        // 累积到上一条 thinking，折叠显示 (分析中自动展开, 报告到达时折叠)
        const last = messages.value[messages.value.length - 1]
        if (last?.role === 'thinking') {
          last.thinkingText = (last.thinkingText || '') + evt.text
        } else {
          const tm = addMessage({ role: 'thinking', thinkingText: evt.text })
          ;(tm as any).thinkingExpanded = loading.value
        }
        break
      }
      case 'stream':
        appendToLast(evt.text)
        break
      case 'bot': {
        // 自动折叠之前的 thinking
        for (let i = messages.value.length - 1; i >= 0; i--) {
          if (messages.value[i].role === 'thinking') (messages.value[i] as any).thinkingExpanded = false
        }
        attachReport(evt.text, evt.segments)
        break
      }
      case 'summary':
        for (let i = messages.value.length - 1; i >= 0; i--) {
          if (messages.value[i].role === 'thinking') (messages.value[i] as any).thinkingExpanded = false
        }
        addMessage({ role: 'summary', text: markdownToHtml(evt.text) })
        break
      case 'tool_call':
        // 更新管线进度文字 + 任务列表子项
        pipeline.setToolAction(evt.tool)
        pipeline.addTaskTool(evt.tool)
        // 工具历史收敛到第一条 tool 消息 (tools 数组), 不再每条工具追加消息 —
        // 否则中间 N-2 条 tool 消息渲染为空行 (首尾才有内容), 消息流出现大量空白
        const label = TOOL_LABELS[evt.tool] || evt.tool
        const existingFirst = messages.value.find(m => m.role === 'tool' && (m as any).isFirstTool)
        if (!existingFirst) {
          const tm = addMessage({ role: 'tool', tool: evt.tool, agent: evt.agent })
          ;(tm as any).isFirstTool = true
          ;(tm as any).isLastTool = true
          ;(tm as any).tools = [{ tool: evt.tool, label, status: 'active' }]
          ;(tm as any).currentTool = label
        } else {
          ;(existingFirst as any).isLastTool = true
          const list = (existingFirst as any).tools || []
          if (list.length) list[list.length - 1].status = 'done'
          list.push({ tool: evt.tool, label, status: 'active' })
          ;(existingFirst as any).tools = list
          ;(existingFirst as any).currentTool = label
        }
        break
      case 'analysis_result':
        // 更新工具完成状态 (tools 数组内对应项 → done)
        const firstTool = messages.value.find(m => m.role === 'tool' && (m as any).isFirstTool)
        if (firstTool) {
          const list = (firstTool as any).tools || []
          const t = list.find((x: any) => x.tool === evt.tool)
          if (t) t.status = 'done'
          ;(firstTool as any).toolResult = evt.data
        }
        break
      case 'stage':
        // 后端发 supervisor 路由决策值 (curator/analyst/reporter/finish):
        // 路由到某阶段 = 前一阶段已完成 (含复用跳过场景)
        pipeline.stageAgent = evt.agent
        pipeline.setTaskStep(evt.agent)
        if (evt.agent === 'curator') pipeline.dataReady = false
        if (evt.agent === 'analyst') { pipeline.dataReady = true; pipeline.analysisDone = false }
        if (evt.agent === 'reporter') { pipeline.dataReady = true; pipeline.analysisDone = true; pipeline.reportDone = false }
        if (evt.agent === 'finish') { pipeline.dataReady = true; pipeline.analysisDone = true; pipeline.reportDone = true }
        break
      case 'plan':
        // 任务列表初始化 (supervisor 首次规划): 步骤骨架 + 目标
        pipeline.setTaskPlan(evt.steps || [])
        // SSE 事件顺序是 stage → plan: setTaskStep(stage) 先执行时 tasks 为空被丢弃,
        // plan 重置后又全部 pending → 补回当前阶段, 否则任务列表永远显示"下一阶段"
        if (pipeline.stageAgent && pipeline.stageAgent !== 'finish') {
          pipeline.setTaskStep(pipeline.stageAgent)
        }
        break
      case 'status':
        // 数据就绪摘要 / 状态提示 (Curator 探查结论 3 行: 质量/口径/能力边界)
        addMessage({ role: 'status', text: evt.text, statusType: evt.status_type || 'info' })
        break
      case 'chart':
        chartPool.set(evt.title, { title: evt.title, chartType: evt.chart_type, echartsOption: evt.echarts_option })
        addMessage({
          role: 'chart',
          chartTitle: evt.title,
          chartType: evt.chart_type,
          echartsOption: evt.echarts_option,
        })
        break
      case 'token_summary':
        // 挂到最后一条 bot/summary 消息上（字段映射: prompt_tokens → prompt）
        for (let i = messages.value.length - 1; i >= 0; i--) {
          if (messages.value[i].role === 'bot' || messages.value[i].role === 'summary') {
            messages.value[i].tokenInfo = {
              prompt: evt.totals.prompt_tokens,
              completion: evt.totals.completion_tokens,
              total: evt.totals.total_tokens,
            }
            break
          }
        }
        break
      case 'degraded':
        addMessage({
          role: 'status',
          text: `[降级] ${evt.agent}: ${evt.reason}`,
          statusType: 'warning',
        })
        break
      case 'confirm_required':
        // 人机协同 (P2): 分析置信度低 → 展示确认选项 (继续 / 重新分析)
        pendingConfirm.value = { reason: evt.reason, confidence: evt.confidence }
        addMessage({
          role: 'status',
          text: `分析置信度 ${(evt.confidence * 100).toFixed(0)}% — ${evt.reason}`,
          statusType: 'warning',
        })
        break
      case 'error':
        addMessage({ role: 'status', text: evt.message, statusType: 'error', retryable: true })
        break
    }
  }

  // ── 会话管理 ──
  async function loadSessions() {
    sessionsLoading.value = true
    try {
      const data = await listSessions()
      sessions.value = data.sessions || []
    } catch {
      // ignore
    } finally {
      sessionsLoading.value = false
    }
  }

  async function switchSession(sid: string) {
    sessionId.value = sid
    messages.value = []
    chartPool.clear()  // 恢复会话: 重建图表池供报告内联
    try {
      const session = await getSession(sid)
      if (session?.messages) {
        for (const m of session.messages) {
          if (m.role === 'chart' && m.echarts_option) {
            const title = m.title || m.chartTitle || ''
            chartPool.set(title, { title, chartType: m.chart_type || m.chartType, echartsOption: m.echarts_option })
            addMessage({
              role: 'chart',
              chartTitle: title,
              chartType: m.chart_type || m.chartType,
              echartsOption: m.echarts_option,
            })
          } else if (m.role === 'bot') {
            // 后端持久化的 segments 优先 (图表数据内嵌, 零匹配直接渲染);
            // 旧会话无 segments → 回退 chartPool 匹配
            if ((m as any).segments?.length) {
              addMessage({ role: 'bot', text: markdownToHtml(m.text || ''), reportSegments: normalizeSegments((m as any).segments) })
            } else {
              attachReport(m.text || '')
            }
          } else if (m.role === 'user') {
            addMessage({ role: 'user', text: m.text || '' })
          }
        }
      }
    } catch {
      setToast('会话加载失败')
    }
  }

  async function removeSession(sid: string) {
    await deleteSession(sid)
    sessions.value = sessions.value.filter(s => s.session_id !== sid)
    if (sid === sessionId.value) {
      newSession()
    }
  }

  function newSession() {
    sessionId.value = 'sess_' + Date.now()
    messages.value = []
    pendingConfirm.value = null
  }

  /** 人机协同 (P2): 用户确认选项 → 带 confirmation 重发同请求
   *  - continue: 复用已有分析结果, 直接生成报告
   *  - reanalyze: 清空分析重新跑
   */
  function resolveConfirm(choice: 'continue' | 'reanalyze') {
    if (!lastInput.value) return
    run(lastInput.value, lastSourceId.value, choice)
  }

  return {
    sessionId, sessions, sessionsLoading,
    messages, visibleMessages, loading, toast, lastInput, lastSourceId,
    pendingConfirm, resolveConfirm,
    run, stop, retryLast, setToast,
    loadSessions, switchSession, removeSession, newSession,
    addMessage,
  }
})
