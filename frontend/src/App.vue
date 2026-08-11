<template>
  <div class="app-container">
    <AppHeader
      :viewing-report="showReport"
      @toggle-sidebar="sidebarCollapsed = !sidebarCollapsed"
      @view-report="showReport = true"
      @back-to-chat="showReport = false"
    />
    <main class="app-main" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
      <transition name="sidebar-slide">
        <SessionSidebar v-if="!sidebarCollapsed" @open-settings="showSettings = true" />
      </transition>
      <div class="chat-panel">
        <!-- Settings Panel -->
        <SettingsPanel v-if="showSettings" @close="showSettings = false" />
        <!-- Report View -->
        <ReportView
          v-else-if="showReport && chat.sessionId"
          :session-id="chat.sessionId"
          @back="showReport = false"
        />
        <!-- Chat View -->
        <template v-else>
          <PipelineBar :loading="chat.loading" />
          <MessageList @run="onRun" />
          <InputArea ref="inputAreaRef" @run="onRun" />
        </template>
      </div>
    </main>
    <transition name="toast-fade">
      <div v-if="chat.toast" class="toast">{{ chat.toast }}</div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { usePipelineStore } from '@/stores/pipelineStore'
import AppHeader from '@/components/layout/AppHeader.vue'
import SessionSidebar from '@/components/layout/SessionSidebar.vue'
import PipelineBar from '@/components/layout/PipelineBar.vue'
import MessageList from '@/components/chat/MessageList.vue'
import InputArea from '@/components/chat/InputArea.vue'
import ReportView from '@/components/report/ReportView.vue'
import SettingsPanel from '@/components/settings/SettingsPanel.vue'

const chat = useChatStore()
const pipeline = usePipelineStore()
const sidebarCollapsed = ref(false)
const showReport = ref(false)
const showSettings = ref(false)
const inputAreaRef = ref<InstanceType<typeof InputArea> | null>(null)

function onRun(input: string) {
  pipeline.reset()
  showReport.value = false
  showSettings.value = false
  const sourceId = inputAreaRef.value?.selectedSource || ''
  chat.run(input, sourceId)
}

onMounted(async () => {
  await chat.loadSessions()
  // 自动恢复最近会话 (list_sessions 按 last_access 倒序, sessions[0] 为最近)
  if (chat.sessions.length > 0) {
    await chat.switchSession(chat.sessions[0].session_id)
  }
  // 主题初始化 (localStorage, 默认亮色)
  const theme = localStorage.getItem('dia_theme') || 'light'
  document.documentElement.dataset.theme = theme
})
</script>
