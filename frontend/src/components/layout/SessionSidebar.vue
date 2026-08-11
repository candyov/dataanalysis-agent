<template>
  <div class="session-sidebar">
    <div class="sidebar-header">
      <h3>会话历史</h3>
      <button @click="chat.newSession()" title="新会话">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </button>
    </div>
    <div class="session-list">
      <div v-if="chat.sessionsLoading" class="sidebar-hint">加载中…</div>
      <div v-else-if="chat.sessions.length === 0" class="sidebar-hint">暂无会话</div>
      <div
        v-for="s in chat.sessions" :key="s.session_id"
        :class="['session-item', { active: s.session_id === chat.sessionId }]"
        @click="chat.switchSession(s.session_id)"
      >
        <div class="session-content">
          <div class="session-title">{{ s.first_message || s.session_id.slice(0, 8) }}</div>
          <div class="session-meta">{{ s.msg_count || 0 }} 条消息</div>
        </div>
        <button class="session-del" @click.stop="chat.removeSession(s.session_id)" title="删除">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="6" y1="6" x2="18" y2="18" /><line x1="6" y1="18" x2="18" y2="6" />
          </svg>
        </button>
      </div>
    </div>
    <div class="sidebar-footer">
      <button class="settings-entry" @click="$emit('open-settings')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        设置与资源管理
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useChatStore } from '@/stores/chatStore'
const chat = useChatStore()
defineEmits<{ 'open-settings': [] }>()
</script>

<style scoped>
/* 主要样式由全局 styles.css 提供，此处预留组件级覆盖 */
</style>