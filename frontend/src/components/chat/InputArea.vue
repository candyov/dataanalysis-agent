<template>
  <div class="input-area">
    <!-- 上传文件后的小提示 -->
    <div v-if="uploadedFile" class="uploaded-chip">
      📄 {{ uploadedFile.name }} · {{ uploadedFile.rows }}行
    </div>
    <div v-if="uploadError" class="upload-error">{{ uploadError }}</div>

    <!-- 人机协同 (P2): 分析置信度低 → 用户确认选项 -->
    <div v-if="chat.pendingConfirm && !chat.loading" class="confirm-bar">
      <span class="confirm-text">⚠️ 分析置信度 {{ Math.round(chat.pendingConfirm.confidence * 100) }}%</span>
      <button class="confirm-btn primary" @click="chat.resolveConfirm('continue')">继续出报告</button>
      <button class="confirm-btn" @click="chat.resolveConfirm('reanalyze')">重新分析</button>
    </div>

    <div class="input-row">
      <!-- 数据源下拉（紧凑） -->
      <div class="source-group">
        <select v-model="selectedSource" class="source-select" @change="onSourceChange">
          <option value="" disabled>选择数据源</option>
          <option v-for="s in sources" :key="s.id" :value="s.id">
            {{ s.id.startsWith('file_') ? '📄' : '🗄️' }} {{ s.name }}
          </option>
        </select>
        <label class="upload-mini-btn" :class="{ uploading }" title="上传 CSV / Excel">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          <input ref="fileInputEl" type="file" accept=".csv,.xlsx,.xls" @change="onFileSelect" :disabled="uploading || chat.loading" hidden />
        </label>
        <!-- 数据源信息提示 -->
        <div v-if="selectedSource && sourceInfo" class="source-tip">{{ sourceInfo.table_count }}表·{{ sourceInfo.total_rows.toLocaleString() }}行</div>
      </div>

      <input
        v-model="input"
        @keyup.enter="onSend"
        :placeholder="selectedSource ? '输入分析问题…' : '请先选择或上传数据源'"
        :disabled="chat.loading"
        ref="inputEl"
      />

      <button v-if="chat.loading" @click="chat.stop()" class="stop-btn" title="停止">
        <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
      </button>
      <button v-else @click="onSend" :disabled="!input.trim() || !selectedSource" class="send-btn" title="发送">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useChatStore } from '@/stores/chatStore'
import { uploadFile, listDatasources, getDatasourceInfo } from '@/api'

const emit = defineEmits<{ run: [text: string] }>()

const chat = useChatStore()
const input = ref('')
const inputEl = ref<HTMLInputElement | null>(null)
const fileInputEl = ref<HTMLInputElement | null>(null)
const selectedSource = ref('')
const sources = ref<{ id: string; name: string; db_type: string }[]>([])
const uploading = ref(false)
const uploadError = ref('')
const uploadedFile = ref<{ name: string; rows: number; source_id: string } | null>(null)
const sourceInfo = ref<{ table_count: number; total_rows: number } | null>(null)

async function loadSources() {
  try {
    const data = await listDatasources()
    sources.value = data.datasources || []
  } catch (e: any) {
    chat.setToast(`加载数据源失败: ${e.message}`)
  }
}

async function onSourceChange() {
  sourceInfo.value = null
  if (!selectedSource.value) return
  try {
    sourceInfo.value = await getDatasourceInfo(selectedSource.value)
  } catch (e: any) {
    chat.setToast(`获取数据源信息失败: ${e.message}`)
  }
}

async function onFileSelect(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  uploadError.value = ''
  uploading.value = true
  try {
    const result = await uploadFile(file)
    uploadedFile.value = { name: file.name, rows: result.rows, source_id: result.source_id }
    await loadSources()
    selectedSource.value = result.source_id
    await onSourceChange()
    inputEl.value?.focus()
  } catch (err: any) {
    uploadError.value = `上传失败: ${err.message || err}`
  } finally {
    uploading.value = false
    if (fileInputEl.value) fileInputEl.value.value = ''
  }
}

function onSend() {
  if (!input.value.trim() || !selectedSource.value) return
  emit('run', input.value)
  input.value = ''
}

defineExpose({ focus: () => inputEl.value?.focus(), selectedSource })
onMounted(loadSources)
</script>

<style scoped>
/* ── chip ── */
.uploaded-chip {
  font-size: 11px; color: var(--color-success, #3A8C3A);
  padding: 2px 0; margin-bottom: 2px;
}
.upload-error { color: var(--color-error, #D14343); font-size: 11px; margin-bottom: 2px; }

/* ── 主体行 ── */
.input-row {
  display: flex; align-items: center; gap: 6px;
}

/* ── 数据源组 ── */
.source-group {
  display: flex; align-items: center; gap: 4px;
  flex-shrink: 0;
}
.source-select {
  height: 34px; padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--border-secondary, #D3D1C7);
  background: var(--bg-surface, #fff);
  font-size: 12px; color: var(--text-primary);
  cursor: pointer; min-width: 150px; max-width: 200px;
}
.source-select:focus { outline: none; border-color: var(--accent, #534AB7); }

.upload-mini-btn {
  display: flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: 6px;
  border: 1px dashed var(--border-secondary, #D3D1C7);
  cursor: pointer; color: var(--text-secondary, #5A5A55);
  flex-shrink: 0; transition: all 0.15s;
}
.upload-mini-btn:hover { border-color: var(--accent, #534AB7); color: var(--accent); background: var(--bg-hover, #F5F4EF); }
.upload-mini-btn.uploading { opacity: 0.4; pointer-events: none; }
.upload-mini-btn svg { width: 14px; height: 14px; }

.source-tip {
  font-size: 10px; color: var(--text-tertiary, #A09F98);
  white-space: nowrap;
}

/* ── 输入框 ── */
input[type="text"] {
  flex: 1; height: 34px; padding: 0 12px;
  border-radius: 8px;
  border: 1px solid var(--border-secondary, #D3D1C7);
  background: var(--bg-surface, #fff);
  font-size: 13px; color: var(--text-primary); outline: none;
  min-width: 0;
}
input[type="text"]:focus { border-color: var(--accent, #534AB7); box-shadow: 0 0 0 3px rgba(83,74,183,0.08); }
input[type="text"]:disabled { opacity: 0.5; }

/* ── 按钮 ── */
.send-btn, .stop-btn {
  width: 34px; height: 34px; border-radius: 8px; border: none;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; flex-shrink: 0; transition: all 0.15s;
}
.send-btn { background: var(--accent, #534AB7); color: #fff; }
.send-btn:hover:not(:disabled) { opacity: 0.85; }
.send-btn:disabled { opacity: 0.25; cursor: not-allowed; }
.stop-btn { background: var(--danger-dim); color: var(--danger); }
.send-btn svg, .stop-btn svg { width: 16px; height: 16px; }

/* ── 人机协同确认条 (P2) ── */
.confirm-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; margin-bottom: 8px;
  background: rgba(251, 191, 36, 0.08);
  border: 1px solid var(--warning, #F59E0B);
  border-radius: 8px; font-size: 12px;
}
.confirm-text { color: var(--warning, #B45309); flex: 1; }
.confirm-btn {
  padding: 4px 12px; border-radius: 6px; font-size: 12px;
  border: 1px solid var(--border-secondary, #D3D1C7);
  background: var(--bg-surface, #fff); color: var(--text-secondary);
  cursor: pointer; transition: all 0.15s;
}
.confirm-btn.primary { background: var(--accent, #534AB7); color: #fff; border-color: var(--accent); }
.confirm-btn:hover { opacity: 0.85; }
</style>