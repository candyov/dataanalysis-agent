<template>
  <header class="app-header">
    <div class="header-left-group">
      <button class="sidebar-toggle" @click="$emit('toggle-sidebar')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="6" x2="21" y2="6" />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>
      <div class="header-brand">
        <div class="brand-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M3 3v18h18" /><path d="M7 14l4-4 4 4 5-5" />
          </svg>
        </div>
        <div class="brand-text">
          <h1>AI 数据分析平台</h1>
          <span class="brand-sub">自然语言数据洞察</span>
        </div>
        <span class="mode-badge"><span class="mode-dot" />Multi-Agent</span>
      </div>
    </div>
    <div class="header-right">
      <button v-if="viewingReport" class="report-btn" @click="$emit('back-to-chat')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
        </svg>
        返回对话
      </button>
      <button v-else class="report-btn" @click="$emit('view-report')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
        </svg>
        查看报告
      </button>
    </div>
  </header>
</template>

<script setup lang="ts">
const props = defineProps<{ viewingReport?: boolean }>()
defineEmits<{ 'toggle-sidebar': []; 'view-report': []; 'back-to-chat': [] }>()
</script>

<style scoped>
/* 主要样式由全局 styles.css 提供，此处预留组件级覆盖 */
.sidebar-toggle {
  flex-shrink: 0;
}
.header-left-group {
  flex-shrink: 0;
}
</style>
