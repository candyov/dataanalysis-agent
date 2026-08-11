<template>
  <div :class="['msg-row', msg.role]">
    <!-- User -->
    <template v-if="msg.role === 'user'">
      <div class="msg-bubble user">{{ msg.text }}</div>
    </template>

    <!-- Bot / Summary -->
    <template v-else-if="msg.role === 'bot' || msg.role === 'summary'">
      <div class="msg-container">
        <div class="msg-avatar" :class="msg.role">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4v2a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z"/><path d="M2 21c0-4.4 4.5-8 10-8s10 3.6 10 8"/></svg>
        </div>
        <div class="msg-body">
          <!-- 报告 + 内联图表 (文本段 / 图表段交替) -->
          <template v-if="msg.reportSegments?.length">
            <div class="msg-bubble bot">
              <template v-for="(seg, i) in msg.reportSegments" :key="i">
                <div v-if="seg.type === 'text'" v-html="sanitizeHtml(markdownToHtml(seg.text || ''))" />
                <div v-else class="inline-chart">
                  <div class="chart-card-header">
                    <span class="chart-card-tag">📊</span>
                    <span class="chart-card-title">{{ seg.title }}</span>
                  </div>
                  <!-- key 与 renderChart 查的 id 一致: 'rpt-' + msg.id + '-' + i -->
                  <div
                    :id="'rpt-' + msg.id + '-' + i"
                    class="chart-container"
                    :ref="(el) => setChartRef('rpt-' + msg.id + '-' + i, el as HTMLElement)"
                  />
                </div>
              </template>
            </div>
          </template>
          <div v-else class="msg-bubble bot" v-html="sanitizeHtml(msg.text)" />
          <div v-if="msg.tokenInfo" class="token-info">{{ formatTokens(msg.tokenInfo) }}</div>
        </div>
      </div>
    </template>

    <!-- Report -->
    <template v-else-if="msg.role === 'report'">
      <div v-if="msg.htmlReport" class="report-card">
        <div class="report-card-header">
          <span class="report-card-tag">📋</span>
          <span class="report-card-title">分析报告</span>
          <button class="report-fullscreen-btn" @click="openFullReport(msg.htmlReport)">全屏查看</button>
        </div>
        <iframe
          :srcdoc="msg.htmlReport"
          class="report-iframe"
          sandbox="allow-scripts"
          @load="onReportLoaded"
        />
      </div>
    </template>

    <!-- Thinking — inline, subtle -->
    <template v-else-if="msg.role === 'thinking'">
      <div class="thinking-row" @click="msg.thinkingExpanded = !msg.thinkingExpanded">
        <span class="thinking-tag">🧠</span>
        <span class="thinking-summary">{{ msg.thinkingExpanded ? '收起推理' : '查看推理过程' }}</span>
        <span class="thinking-arrow">{{ msg.thinkingExpanded ? '▲' : '▼' }}</span>
      </div>
      <div v-if="msg.thinkingExpanded" class="thinking-content">{{ msg.thinkingText }}</div>
    </template>

    <!-- Tool call — grouped into analysis process -->
    <template v-else-if="msg.role === 'tool'">
      <div v-if="showToolProcess(msg)" class="tool-process-bar" @click="toolExpanded = !toolExpanded">
        <span class="tool-dot" :class="{ active: !isToolDone(msg) }" />
        <span class="tool-progress">
          <template v-if="!isToolDone(msg)">
            {{ agentLabel(msg.agent) }} {{ toolAction(msg.tool) }}
          </template>
          <template v-else>
            {{ agentLabel(msg.agent) }} 完成
          </template>
        </span>
        <span class="tool-expand">{{ toolExpanded ? '收起' : '展开' }}</span>
      </div>
      <div v-if="toolExpanded && showToolHistory(msg)" class="tool-history">
        <div class="tool-history-item" v-for="(t, ti) in recentTools" :key="ti">
          <span class="tool-badge">{{ agentLabel(msg.agent) }}</span>
          <span class="tool-icon">{{ toolIcon(t.tool) }}</span>
          <span class="tool-name">{{ t.label }}</span>
          <span v-if="t.status === 'done'" class="tool-done">✓</span>
        </div>
      </div>
    </template>

    <!-- Chart -->
    <template v-else-if="msg.role === 'chart'">
      <div v-if="msg.echartsOption" class="chart-card">
        <div class="chart-card-header">
          <span class="chart-card-tag">📊</span>
          <span class="chart-card-title">{{ msg.chartTitle }}</span>
        </div>
        <!-- key 必须与 renderChart 查的 id 一致: 'chart-' + msg.id -->
        <div
          :id="'chart-' + msg.id"
          class="chart-container"
          :ref="(el) => setChartRef('chart-' + msg.id, el as HTMLElement)"
        />
      </div>
    </template>

    <!-- Status -->
    <template v-else-if="msg.role === 'status'">
      <div :class="['status-msg', msg.statusType || 'info']">
        {{ msg.text }}
        <button v-if="msg.retryable" class="retry-btn" @click="chat.retryLast()">重试</button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import type { ChatMessage } from '@/types/message'
import { useCharts } from '@/composables/useCharts'
import { useChatStore } from '@/stores/chatStore'
import { sanitizeHtml, markdownToHtml } from '@/utils/markdown'
import { TOOL_ICONS, TOOL_ACTIONS } from '@/utils/toolLabels'

const props = defineProps<{ msg: ChatMessage }>()
const { setChartRef, renderChart } = useCharts()
const chat = useChatStore()
const toolExpanded = ref(false)

/** 报告内联图表: 按容器 id ('rpt-{msgId}-{i}') 渲染, 容器已由模板 setChartRef 注册 */
function renderInlineCharts() {
  ;(props.msg.reportSegments || []).forEach((seg, i) => {
    if (seg.type === 'chart' && seg.echartsOption) {
      renderChart(`rpt-${props.msg.id}-${i}`, seg.echartsOption)
    }
  })
}

const AGENT_LABELS: Record<string, string> = {
  curator: 'Curator', analyst: 'Analyst', reporter: 'Reporter',
  ingestor: 'Ingestor', supervisor: 'Supervisor',
}

function agentLabel(name?: string) { return name ? (AGENT_LABELS[name] || name) : 'Agent' }
function toolIcon(name?: string) { return name ? (TOOL_ICONS[name] || '🛠️') : '🛠️' }
function toolAction(name?: string) { return name ? (TOOL_ACTIONS[name] || `调用了 ${name}`) : '工作中' }

function showToolProcess(m: ChatMessage) {
  return (m as any).isFirstTool
}
function isToolDone(m: ChatMessage) {
  return !!(m as any).toolResult
}
// 工具历史收敛在第一条 tool 消息的 tools 数组里 (chatStore 不再为每个工具追加消息)
const recentTools = computed(() => {
  const first = chat.messages.find(m => m.role === 'tool' && (m as any).isFirstTool) as any
  return (first?.tools || []).slice(-8)
})
function showToolHistory(m: ChatMessage) {
  return (m as any).isLastTool && ((m as any).tools?.length > 0)
}

function formatTokens(t: { prompt: number; completion: number; total: number }) {
  const k = (n: number) => (n / 1000).toFixed(1) + 'k'
  return `消耗 ${k(t.total)} tokens · prompt ${k(t.prompt)} · completion ${k(t.completion)}`
}
function openFullReport(html: string) {
  // 用 Blob URL + sandbox iframe 全屏展示, 不直接 document.write —
  // 报告 HTML 含 LLM/数据源注入内容, 无沙箱的裸窗口会以完整源权限执行脚本
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const w = window.open('', '_blank', 'width=1200,height=800')
  if (w) {
    w.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%}iframe{width:100%;height:100%;border:none}</style></head><body><iframe sandbox="allow-scripts" src="${url}"></iframe></body></html>`)
    w.document.close()
    // 窗口关闭时释放 blob URL
    w.addEventListener('unload', () => URL.revokeObjectURL(url))
  } else {
    URL.revokeObjectURL(url)
  }
}
function onReportLoaded(e: Event) {
  const iframe = e.target as HTMLIFrameElement
  if (iframe) iframe.style.height = '600px'
}

onMounted(() => {
  if (props.msg.role === 'chart' && props.msg.echartsOption) {
    renderChart('chart-' + props.msg.id, props.msg.echartsOption)
  }
  renderInlineCharts()
})
watch(() => props.msg.echartsOption, (opt) => {
  if (opt && props.msg.role === 'chart') {
    setTimeout(() => renderChart('chart-' + props.msg.id, opt), 100)
  }
})
</script>

<style scoped>
.msg-row { display: flex; }
.msg-row.user { justify-content: flex-end; }
.msg-bubble.user {
  background: linear-gradient(135deg, var(--accent), #818cf8);
  color: #fff; padding: 10px 18px; border-radius: 16px 16px 4px 16px;
  font-size: 14px; max-width: 72%; box-shadow: 0 2px 8px rgba(99,102,241,.15);
  line-height: 1.5;
}

.msg-container { display: flex; gap: 10px; max-width: 88%; }
.msg-avatar {
  width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-soft); color: var(--accent);
  margin-top: 2px;
}
.msg-avatar svg { width: 16px; height: 16px; }
.msg-avatar.summary { background: rgba(52, 211, 153, 0.12); color: var(--success); }

.msg-bubble.bot {
  font-size: 14px; color: var(--text-primary); line-height: 1.75;
  padding: 2px 0;
}
.msg-bubble.bot :deep(h1), .msg-bubble.bot :deep(h2), .msg-bubble.bot :deep(h3) {
  margin: 14px 0 8px; font-size: 15px; font-weight: 700; color: var(--text-primary);
}
.msg-bubble.bot :deep(p) { margin: 6px 0; }
.msg-bubble.bot :deep(ul), .msg-bubble.bot :deep(ol) { margin: 6px 0; padding-left: 18px; }
.msg-bubble.bot :deep(li) { margin: 3px 0; }
.msg-bubble.bot :deep(code) {
  background: var(--bg-elevated); padding: 2px 6px; border-radius: 4px;
  font-size: 12px; font-family: var(--font-mono);
}
.msg-bubble.bot :deep(strong) { font-weight: 600; color: var(--accent); }
.msg-bubble.bot :deep(table) {
  border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12px;
  border-radius: 8px; overflow: hidden;
}
.msg-bubble.bot :deep(th) {
  background: var(--bg-elevated); font-weight: 600; padding: 8px 12px;
  color: var(--text-secondary); text-align: left; border-bottom: 2px solid var(--border-primary);
}
.msg-bubble.bot :deep(td) { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); }

.token-info { font-size: 11px; color: var(--text-muted); margin-top: 8px; font-family: var(--font-mono); opacity: .6; }

.thinking-row {
  display: flex; align-items: center; gap: 6px; padding: 4px 0;
  cursor: pointer; opacity: .7; font-size: 11px; color: var(--thinking-color);
  font-style: italic;
  user-select: none;
}
.thinking-row:hover { opacity: .9; }
.thinking-arrow { font-size: 9px; }
.thinking-content {
  font-size: 11px; font-family: var(--font-mono); color: var(--thinking-color); font-style: italic;
  background: var(--bg-elevated); border-left: 2px solid var(--border-primary); border-radius: 0 6px 6px 0;
  padding: 10px 14px; margin: 4px 0 8px; max-height: 180px; overflow-y: auto;
  line-height: 1.5; white-space: pre-wrap;
}

.tool-process-bar {
  display: flex; align-items: center; gap: 8px; padding: 6px 12px;
  background: var(--bg-elevated); border-radius: 8px; border: 1px solid var(--border-primary);
  cursor: pointer; font-size: 12px; color: var(--text-secondary);
  transition: background .15s;
}
.tool-process-bar:hover { background: var(--bg-hover); }
.tool-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }
.tool-dot.active { background: var(--accent); animation: pulse-dot 1.4s infinite; }
@keyframes pulse-dot { 0%,100%{opacity:.4} 50%{opacity:1} }
.tool-progress { flex: 1; }
.tool-expand { font-size: 11px; color: var(--accent); }
.tool-history { margin: 6px 0; border-left: 2px solid var(--border-primary); padding-left: 12px; }
.tool-history-item { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 11px; }
.tool-badge {
  font-size: 10px; font-weight: 600; padding: 1px 6px; border-radius: 8px;
  background: var(--accent-soft); color: var(--accent);
}
.tool-name { color: var(--text-secondary); } .tool-cost { color: var(--text-muted); margin-left: auto; }

.chart-card { margin: 12px 0; }
.chart-card-header {
  display: flex; align-items: center; gap: 6px; padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.chart-card-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.chart-container { height: 340px; padding: 6px 0; }

.report-card {
  margin: 12px 0; border: 1px solid var(--border-primary); border-radius: 10px;
  overflow: hidden; background: var(--bg-surface);
}
.report-card-header {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  background: var(--bg-elevated); border-bottom: 1px solid var(--border-primary);
}
.report-card-title { font-weight: 600; font-size: 14px; color: var(--text-primary); flex: 1; }
.report-fullscreen-btn {
  padding: 4px 10px; font-size: 12px; border: 1px solid var(--border-primary);
  border-radius: 5px; background: var(--bg-surface); color: var(--text-secondary); cursor: pointer;
}
.report-fullscreen-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
.report-iframe { width: 100%; border: none; min-height: 400px; max-height: 80vh; }

.status-msg {
  padding: 9px 16px; border-radius: 8px; font-size: 13px; font-weight: 500;
}
.status-msg.info { background: var(--accent-soft); color: var(--accent); border-left: 3px solid var(--accent); }
.status-msg.error { background: var(--danger-dim); color: var(--danger); border-left: 3px solid var(--danger); }
.status-msg.warning { background: rgba(251, 191, 36, 0.08); color: var(--warning); border-left: 3px solid var(--warning); }
.retry-btn {
  margin-left: 8px; padding: 2px 10px; border: 1px solid currentColor;
  background: transparent; color: inherit; border-radius: 4px;
  cursor: pointer; font-size: 12px;
}
.retry-btn:hover { background: currentColor; color: #fff; }
</style>
