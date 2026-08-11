<template>
  <div v-if="loading" class="pipeline-bar">
    <div class="pipeline-steps-row">
      <div v-for="(step, i) in pipeline.steps" :key="step.key" :class="['pipeline-step', step.status]">
        <div class="step-indicator">
          <svg v-if="step.status === 'done'" class="step-check-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          <span v-else-if="step.status === 'active'" class="step-spinner" />
          <span v-else class="step-num">{{ i + 1 }}</span>
        </div>
        <span class="step-label">{{ step.label }}</span>
        <div v-if="i < pipeline.steps.length - 1" :class="['step-connector', step.status === 'done' ? 'done' : '']" />
      </div>
    </div>
    <!-- 当前工具动作已由任务列表的 task-tools chips 实时呈现, 不再单独重复一行 -->

    <!-- 任务列表: plan/stage/tool_call 事件驱动的实时进展 (只显示阶段名+工具子项, 不显示 goal 细节;
         工具子项只展开当前活动阶段, 完成阶段收起为单行 — 全历史摊开会占大量纵向空间) -->
    <div v-if="pipeline.tasks.length" class="task-list">
      <div v-for="(st, si) in pipeline.tasks" :key="si" :class="['task-step', st.status]">
        <span class="task-status">{{ st.status === 'done' ? '✓' : st.status === 'active' ? '●' : '○' }}</span>
        <span class="task-label">{{ st.label }}</span>
        <div v-if="st.status === 'active' && st.tools.length" class="task-tools">
          <span v-for="(t, ti) in st.tools" :key="ti" :class="['task-tool', t.status]">
            {{ t.status === 'done' ? '✓' : '●' }} {{ t.label }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { usePipelineStore } from '@/stores/pipelineStore'

defineProps<{ loading: boolean }>()
const pipeline = usePipelineStore()
</script>

<style scoped>
.task-list {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 12px; margin-top: 4px;
  background: var(--bg-elevated); border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
}
.task-step {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-muted);
}
.task-step.active { color: var(--text-secondary); }
.task-step.done { color: var(--text-muted); opacity: 0.8; }
.task-status { width: 14px; text-align: center; flex-shrink: 0; }
.task-step.active .task-status { color: var(--accent); }
.task-step.done .task-status { color: var(--success); }
.task-label { font-weight: 600; color: inherit; }
.task-step.done .task-label { color: var(--text-muted); }
.task-goal {
  font-size: 11px; color: var(--text-muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px;
}
.task-tools {
  display: flex; flex-wrap: wrap; gap: 4px 10px;
  margin-left: 20px; margin-top: 2px;
}
.task-tool { font-size: 11px; color: var(--text-muted); }
.task-tool.active { color: var(--accent); }
.task-tool.done { color: var(--text-muted); opacity: 0.7; }
</style>
