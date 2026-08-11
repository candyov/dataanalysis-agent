<template>
  <div class="message-list" ref="chatArea">
    <div v-if="chat.visibleMessages.length === 0 && !chat.loading" class="empty-state">
      <div class="empty-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M3 3v18h18"/><path d="M7 16l4-4 4 4 5-5"/>
          <circle cx="8.5" cy="8.5" r="1.5" fill="currentColor" stroke="none"/>
        </svg>
      </div>
      <h2>AI 数据分析平台</h2>
      <p>连接数据源，用自然语言发起分析 — 自动探查、统计、出图、报告</p>
      <div class="example-prompts">
        <span v-for="p in examples" :key="p" class="example-chip" @click="$emit('run', p)">{{ p }}</span>
      </div>
      <div class="empty-hints">
        <div class="hint-item">
          <span class="hint-icon">🔍</span>
          <div><strong>数据探查</strong><br/><span>自动扫描结构、评估质量、设计指标体系</span></div>
        </div>
        <div class="hint-item">
          <span class="hint-icon">📊</span>
          <div><strong>统计分析</strong><br/><span>对比验证、趋势预测、归因诊断</span></div>
        </div>
        <div class="hint-item">
          <span class="hint-icon">📋</span>
          <div><strong>报告生成</strong><br/><span>图表可视化 + 商业洞察 + 行动建议</span></div>
        </div>
      </div>
    </div>

    <MessageItem v-for="msg in chat.visibleMessages" :key="msg.id" :msg="msg" />

    <div v-if="chat.loading" class="loading-row">
      <span class="loading-text">{{ stageLabel ? stageLabel + ' 思考中…' : '分析中' }}</span>
      <span class="loading-dots"><span class="dot" /><span class="dot" /><span class="dot" /></span>
    </div>

    <div ref="scrollAnchor" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { usePipelineStore } from '@/stores/pipelineStore'
import MessageItem from './MessageItem.vue'

const emit = defineEmits<{ run: [text: string] }>()
const chat = useChatStore()
const pipeline = usePipelineStore()
const chatArea = ref<HTMLElement | null>(null)
const scrollAnchor = ref<HTMLElement | null>(null)

// LLM 调用期间 (无 SSE 事件) 的等待反馈: 显示当前阶段, 避免"卡住"错觉
const STAGE_LABELS: Record<string, string> = { curator: '数据准备', analyst: '分析引擎', reporter: '报告生成' }
const stageLabel = computed(() => STAGE_LABELS[pipeline.stageAgent] || '')

const examples = [
  '这份数据整体质量如何？',
  '各区域销售额对比分析',
  '哪些品类增长最快？',
  '分析利润的关键驱动因素',
]

function scrollToBottom() {
  nextTick(() => scrollAnchor.value?.scrollIntoView({ behavior: 'smooth' }))
}

watch(() => chat.messages, scrollToBottom, { deep: true })
onMounted(scrollToBottom)
</script>

<style scoped>
.message-list {
  flex: 1; overflow-y: auto; padding: 20px 24px;
  display: flex; flex-direction: column; gap: 14px;
}

.empty-state {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 48px 20px 32px; text-align: center;
  animation: fade-up .4s cubic-bezier(.16,1,.3,1);
}
@keyframes fade-up { from{opacity:0;transform:translateY(12px)} to{opacity:1;transform:translateY(0)} }
.empty-icon {
  width: 56px; height: 56px; border-radius: 14px;
  background: var(--accent-glow); display: flex;
  align-items: center; justify-content: center; margin-bottom: 16px;
}
.empty-icon svg { width: 28px; height: 28px; color: var(--accent); }
.empty-state h2 { font-size: 22px; font-weight: 700; color: var(--text-primary); margin-bottom: 4px; }
.empty-state p { font-size: 14px; color: var(--text-secondary); margin-bottom: 20px; }

.example-prompts { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; max-width: 480px; margin-bottom: 28px; }
.example-chip {
  padding: 8px 16px; border-radius: 20px; background: var(--bg-surface);
  border: 1px solid var(--border-primary); font-size: 13px;
  color: var(--text-secondary); cursor: pointer; transition: all .15s; font-weight: 500;
}
.example-chip:hover { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-border); transform: translateY(-1px); }

.empty-hints {
  display: flex; gap: 24px; max-width: 560px;
}
.hint-item {
  display: flex; gap: 10px; text-align: left; padding: 14px;
  background: var(--bg-elevated); border-radius: 12px; flex: 1; border: 1px solid var(--border-subtle);
}
.hint-icon { font-size: 22px; flex-shrink: 0; }
.hint-item strong { font-size: 13px; color: var(--text-primary); display: block; margin-bottom: 2px; }
.hint-item span { font-size: 11px; color: var(--text-muted); line-height: 1.5; }

.loading-row {
  display: flex; align-items: center; gap: 8px; padding: 8px 0;
}
.loading-text { font-size: 13px; color: var(--text-muted); font-weight: 500; }
.loading-dots { display: flex; gap: 4px; }
.loading-dots .dot {
  width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
  animation: pulse 1.4s infinite ease-in-out;
}
.loading-dots .dot:nth-child(2) { animation-delay: .2s; }
.loading-dots .dot:nth-child(3) { animation-delay: .4s; }
@keyframes pulse { 0%,80%,100%{opacity:.3;transform:scale(.7)} 40%{opacity:1;transform:scale(1)} }
</style>
