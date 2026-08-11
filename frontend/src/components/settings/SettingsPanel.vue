<template>
  <div class="settings-panel">
    <div class="settings-header">
      <h2>⚙️ 设置与资源管理</h2>
      <button class="close-btn" @click="$emit('close')" title="关闭">✕</button>
    </div>
    <div class="settings-tabs">
      <button
        v-for="t in tabs" :key="t.key"
        :class="['tab', { active: activeTab === t.key }]"
        @click="activeTab = t.key"
      >{{ t.icon }} {{ t.label }}</button>
    </div>

    <div class="settings-body">
      <!-- ═══ 设置 ═══ -->
      <div v-if="activeTab === 'settings'" class="tab-pane">
        <section class="card">
          <h3>模型档案 <span class="hint-inline">(多模型切换, 即时生效)</span></h3>
          <div v-if="!profiles.length" class="empty">暂无模型档案 — 添加一个开始 (未配置时使用环境变量默认)</div>
          <div v-for="p in profiles" :key="p.id" class="ds-item">
            <div class="ds-info">
              <span class="ds-name">{{ p.name }}</span>
              <span class="badge">{{ p.provider }}</span>
              <span class="ds-detail">{{ p.model || '未指定模型' }}</span>
              <span v-if="p.id === activeId" class="badge on">✓ 当前模型</span>
            </div>
            <div class="ds-actions">
              <button v-if="p.id !== activeId" class="mini" @click="activate(p)">切换</button>
              <button class="mini" @click="editProfile(p)">编辑</button>
              <button class="mini" @click="testProfile(p)">测试</button>
              <button class="mini danger" @click="removeProfile(p)">删除</button>
            </div>
          </div>
        </section>
        <section class="card">
          <h3>{{ editingId ? '编辑模型档案' : '添加模型档案' }}</h3>
          <div class="field">
            <label>名称</label>
            <input v-model="profileForm.name" placeholder="如: DeepSeek 主力 / OpenAI 备用" />
          </div>
          <div class="field">
            <label>提供商</label>
            <select v-model="profileForm.provider" @change="onProviderChange">
              <option value="deepseek">DeepSeek</option>
              <option value="openai">OpenAI</option>
              <option value="ollama">Ollama (本地)</option>
              <option value="custom">自定义 (OpenAI 兼容)</option>
            </select>
          </div>
          <div class="field">
            <label>Base URL</label>
            <input v-model="profileForm.base_url" placeholder="https://api.deepseek.com/v1" />
          </div>
          <div class="field">
            <label>模型</label>
            <input v-model="profileForm.model" placeholder="deepseek-chat / gpt-4o / llama3.1" />
          </div>
          <div class="field">
            <label>API Key</label>
            <input
              v-model="profileForm.api_key" type="password"
              :placeholder="editingId ? '留空保持不变' : 'sk-...'"
            />
          </div>
          <div class="field">
            <label>温度 <code>{{ profileForm.temperature }}</code></label>
            <input v-model.number="profileForm.temperature" type="range" min="0" max="1" step="0.1" />
          </div>
          <div class="actions">
            <button class="btn primary" @click="saveProfile">{{ editingId ? '保存修改' : '添加' }}</button>
            <button v-if="editingId" class="btn" @click="resetForm">取消编辑</button>
            <span v-if="modelTest" class="hint" :class="{ ok: modelTest.ok }">{{ modelTest.message }}</span>
          </div>
        </section>
        <section class="card">
          <h3>界面主题</h3>
          <div class="theme-row">
            <label class="check"><input type="radio" value="light" v-model="theme" /> ☀️ 亮色</label>
            <label class="check"><input type="radio" value="dark" v-model="theme" /> 🌙 暗色</label>
          </div>
        </section>
      </div>

      <!-- ═══ 数据源 ═══ -->
      <div v-else-if="activeTab === 'datasources'" class="tab-pane">
        <section class="card">
          <h3>添加数据库连接</h3>
          <div class="field">
            <label>类型</label>
            <select v-model="dsForm.db_type">
              <option value="mysql">MySQL</option>
              <option value="postgres">PostgreSQL</option>
              <option value="sqlite">SQLite (文件路径)</option>
            </select>
          </div>
          <div class="field"><label>名称</label><input v-model="dsForm.name" placeholder="生产订单库" /></div>
          <div class="field">
            <label>Host</label>
            <input v-model="dsForm.host" :placeholder="dsForm.db_type === 'sqlite' ? '(本地文件)' : '127.0.0.1'" />
          </div>
          <div class="field">
            <label>Port</label>
            <input v-model.number="dsForm.port" type="number"
              :placeholder="dsForm.db_type === 'mysql' ? '3306' : dsForm.db_type === 'postgres' ? '5432' : ''" />
          </div>
          <div class="field">
            <label>Database</label>
            <input v-model="dsForm.database" :placeholder="dsForm.db_type === 'sqlite' ? '/path/to/data.db' : '库名'" />
          </div>
          <template v-if="dsForm.db_type !== 'sqlite'">
            <div class="field"><label>Username</label><input v-model="dsForm.username" /></div>
            <div class="field"><label>Password</label><input v-model="dsForm.password" type="password" /></div>
          </template>
          <div class="field row">
            <label class="check"><input type="checkbox" v-model="dsForm.read_only" /> 只读连接 (生产安全基线, 推荐)</label>
          </div>
          <div class="actions">
            <button class="btn" @click="testDs">测试连接</button>
            <button class="btn primary" @click="addDs">保存连接</button>
            <span v-if="dsTest" class="hint" :class="{ ok: dsTest.ok }">
              {{ dsTest.ok ? `✓ ${dsTest.message} (${dsTest.latency_ms}ms)` : `✗ ${dsTest.message}` }}
            </span>
          </div>
        </section>
        <section class="card">
          <h3>已连接数据源 ({{ datasources.length }})</h3>
          <div v-if="!datasources.length" class="empty">暂无连接 — 上传文件或添加数据库连接</div>
          <div v-for="ds in datasources" :key="ds.id" class="ds-item">
            <div class="ds-info">
              <span class="ds-name">{{ ds.name }}</span>
              <span class="badge">{{ ds.db_type }}</span>
              <span v-if="ds.read_only" class="badge ro">🔒 只读</span>
              <span :class="['badge', ds.enabled ? 'on' : 'off']">{{ ds.enabled ? '启用' : '禁用' }}</span>
              <span class="ds-detail">{{ ds.host || ds.database }}</span>
            </div>
            <div class="ds-actions">
              <button class="mini" @click="toggleDs(ds)">{{ ds.enabled ? '禁用' : '启用' }}</button>
              <button class="mini danger" @click="removeDs(ds)">删除</button>
            </div>
          </div>
        </section>
      </div>

      <!-- ═══ 文件 ═══ -->
      <div v-else class="tab-pane">
        <section class="card">
          <h3>上传数据文件 <span class="hint-inline">(CSV / Excel, ≤50MB)</span></h3>
          <div class="upload-row">
            <label class="btn upload-btn">
              选择文件
              <input type="file" accept=".csv,.xlsx,.xls" hidden @change="onUpload" />
            </label>
            <span v-if="uploading" class="hint">上传中…</span>
          </div>
        </section>
        <section class="card">
          <h3>文件列表 ({{ files.length }})</h3>
          <div v-if="!files.length" class="empty">暂无上传文件</div>
          <div v-for="f in files" :key="f.source_id" class="ds-item">
            <div class="ds-info">
              <span class="ds-name">📄 {{ f.name }}</span>
              <span class="ds-detail">{{ f.rows.toLocaleString() }} 行 · {{ f.columns }} 列 · {{ formatSize(f.size) }}</span>
            </div>
            <div class="ds-actions">
              <button class="mini danger" @click="removeFile(f)">删除</button>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import {
  listDatasources, testDatasource, createDatasource, updateDatasource, toggleDatasource, deleteDataSource,
  listFiles, deleteFile, uploadFile,
  listModels, createModel, updateModel, deleteModel, activateModel, testModelProfile,
} from '@/api'

defineEmits<{ close: [] }>()

const tabs = [
  { key: 'settings', icon: '⚙️', label: '设置' },
  { key: 'datasources', icon: '🗄️', label: '数据源' },
  { key: 'files', icon: '📁', label: '文件' },
]
const activeTab = ref('settings')

// ── 设置 Tab: 模型档案 ──
const profiles = ref<any[]>([])
const activeId = ref('')
const presets = ref<Record<string, { base_url: string; default_model: string }>>({})
const editingId = ref('')
const profileForm = ref({ name: '', provider: 'deepseek', base_url: '', model: '', api_key: '', temperature: 0.2 })
const modelTest = ref<{ ok: boolean; message: string } | null>(null)

async function loadModels() {
  try {
    const m = await listModels()
    profiles.value = m.profiles || []
    activeId.value = m.active_id || ''
    presets.value = m.presets || {}
  } catch (e: any) {
    modelTest.value = { ok: false, message: `读取模型失败: ${e.message}` }
  }
}
function onProviderChange() {
  const preset = presets.value[profileForm.value.provider]
  if (preset && !editingId.value) {
    profileForm.value.base_url = preset.base_url
    profileForm.value.model = preset.default_model
  }
}
function resetForm() {
  editingId.value = ''
  profileForm.value = { name: '', provider: 'deepseek', base_url: '', model: '', api_key: '', temperature: 0.2 }
  modelTest.value = null
}
function editProfile(p: any) {
  editingId.value = p.id
  profileForm.value = {
    name: p.name, provider: p.provider, base_url: p.base_url, model: p.model,
    api_key: '', temperature: p.temperature ?? 0.2,
  }
  modelTest.value = null
}
async function saveProfile() {
  const body = {
    name: profileForm.value.name,
    provider: profileForm.value.provider,
    base_url: profileForm.value.base_url,
    model: profileForm.value.model,
    temperature: profileForm.value.temperature,
  }
  if (profileForm.value.api_key) body.api_key = profileForm.value.api_key
  try {
    if (editingId.value) {
      await updateModel(editingId.value, body)
    } else {
      await createModel(body)
    }
    resetForm()
    await loadModels()
    modelTest.value = { ok: true, message: editingId.value ? '' : '已添加 (首个档案自动激活)' }
  } catch (e: any) {
    modelTest.value = { ok: false, message: `保存失败: ${e.message}` }
  }
}
async function activate(p: any) {
  await activateModel(p.id)
  activeId.value = p.id
  modelTest.value = { ok: true, message: `已切换到 ${p.name}, 下次分析生效` }
}
async function testProfile(p: any) {
  modelTest.value = await testModelProfile(p.id)
}
async function removeProfile(p: any) {
  if (!confirm(`删除模型档案「${p.name}」?`)) return
  await deleteModel(p.id)
  await loadModels()
}

// ── 主题 ──
const theme = ref(localStorage.getItem('dia_theme') || 'light')
watch(theme, (v) => {
  document.documentElement.dataset.theme = v
  localStorage.setItem('dia_theme', v)
})

// ── 数据源 Tab ──
const dsForm = ref({ name: '', db_type: 'mysql', host: '', port: 0, database: '', username: '', password: '', read_only: true })
const dsTest = ref<{ ok: boolean; message: string; latency_ms: number } | null>(null)
const datasources = ref<any[]>([])

async function loadDatasources() {
  try {
    const d = await listDatasources()
    datasources.value = d.datasources || []
  } catch (e: any) {
    dsTest.value = { ok: false, message: `加载失败: ${e.message}`, latency_ms: 0 }
  }
}
async function testDs() {
  dsTest.value = null
  dsTest.value = await testDatasource(dsForm.value)
}
async function addDs() {
  if (!dsForm.value.name) { dsTest.value = { ok: false, message: '请填写名称', latency_ms: 0 }; return }
  try {
    const r = await createDatasource(dsForm.value)
    dsTest.value = { ok: true, message: `已保存 (${r.id.slice(0, 8)}…)`, latency_ms: 0 }
    dsForm.value = { name: '', db_type: 'mysql', host: '', port: 0, database: '', username: '', password: '', read_only: true }
    await loadDatasources()
  } catch (e: any) {
    dsTest.value = { ok: false, message: e.message, latency_ms: 0 }
  }
}
async function toggleDs(ds: any) {
  const r = await toggleDatasource(ds.id)
  ds.enabled = r.enabled
}
async function removeDs(ds: any) {
  if (!confirm(`删除数据源「${ds.name}」? 不可恢复。`)) return
  await deleteDataSource(ds.id)
  await loadDatasources()
}

// ── 文件 Tab ──
const files = ref<any[]>([])
const uploading = ref(false)

async function loadFiles() {
  try {
    const f = await listFiles()
    files.value = f.files || []
  } catch { /* ignore */ }
}
async function onUpload(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    await uploadFile(file)
    await loadFiles()
  } finally {
    uploading.value = false
    input.value = ''
  }
}
async function removeFile(f: any) {
  if (!confirm(`删除文件「${f.name}」及其数据源? 不可恢复。`)) return
  await deleteFile(f.source_id)
  await loadFiles()
}
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return bytes + ' B'
}

onMounted(() => {
  loadModels()
  loadDatasources()
  loadFiles()
})
</script>

<style scoped>
.settings-panel {
  display: flex; flex-direction: column; height: 100%; overflow: hidden;
}
.settings-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 24px; border-bottom: 1px solid var(--border-primary);
  flex-shrink: 0;
}
.settings-header h2 { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.close-btn {
  width: 30px; height: 30px; border-radius: var(--radius-xs);
  border: 1px solid var(--border-primary); background: var(--bg-surface);
  color: var(--text-secondary); cursor: pointer; font-size: 13px;
}
.close-btn:hover { background: var(--danger-dim); color: var(--danger); border-color: var(--danger); }
.settings-tabs {
  display: flex; gap: 8px; padding: 12px 24px 0; border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.tab {
  padding: 8px 16px; border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  border: 1px solid transparent; border-bottom: none; background: transparent;
  color: var(--text-secondary); font-size: 13px; font-weight: 600; cursor: pointer;
}
.tab.active { background: var(--bg-surface); border-color: var(--border-primary); color: var(--accent); }
.settings-body { flex: 1; overflow-y: auto; padding: 20px 24px; }
.tab-pane { display: flex; flex-direction: column; gap: 20px; max-width: 720px; }
.card {
  background: var(--bg-surface); border: 1px solid var(--border-primary);
  border-radius: var(--radius-md); padding: 20px 24px;
}
.card h3 { font-size: 14px; font-weight: 700; color: var(--text-primary); margin-bottom: 16px; }
.hint-inline { font-size: 11px; color: var(--text-muted); font-weight: 400; }
.field { margin-bottom: 12px; }
.field label {
  display: block; font-size: 12px; color: var(--text-secondary);
  margin-bottom: 4px; font-weight: 500;
}
.field input, .field select {
  width: 100%; padding: 8px 12px; border-radius: var(--radius-xs);
  border: 1px solid var(--border-primary); background: var(--bg-elevated);
  color: var(--text-primary); font-size: 13px; outline: none;
}
.field input:focus, .field select:focus { border-color: var(--accent); }
.field.row { display: flex; align-items: center; }
.check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-secondary); cursor: pointer; }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 16px; }
.btn {
  padding: 8px 18px; border-radius: var(--radius-xs);
  border: 1px solid var(--border-primary); background: var(--bg-elevated);
  color: var(--text-primary); font-size: 13px; font-weight: 600; cursor: pointer;
  transition: all var(--transition);
}
.btn:hover { border-color: var(--accent-border); color: var(--accent); }
.btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.btn.primary:hover { opacity: 0.9; color: #fff; }
.btn.upload-btn { display: inline-block; }
.hint { font-size: 12px; color: var(--danger); }
.hint.ok { color: var(--success); }
.theme-row { display: flex; gap: 24px; }
.empty { font-size: 13px; color: var(--text-muted); padding: 12px 0; text-align: center; }
.ds-item {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle); margin-bottom: 8px;
}
.ds-info { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ds-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.ds-detail { font-size: 12px; color: var(--text-muted); }
.badge {
  font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px;
  background: var(--bg-elevated); color: var(--text-secondary);
}
.badge.ro { background: var(--accent-soft); color: var(--accent); }
.badge.on { background: rgba(52, 211, 153, 0.12); color: var(--success); }
.badge.off { background: rgba(148, 163, 184, 0.12); color: var(--text-muted); }
.ds-actions { display: flex; gap: 6px; flex-shrink: 0; }
.mini {
  padding: 4px 10px; border-radius: var(--radius-xs); font-size: 12px;
  border: 1px solid var(--border-primary); background: transparent; color: var(--text-secondary);
  cursor: pointer;
}
.mini:hover { color: var(--accent); border-color: var(--accent-border); }
.mini.danger:hover { color: var(--danger); border-color: var(--danger); background: var(--danger-dim); }
.upload-row { display: flex; align-items: center; gap: 12px; }
</style>
