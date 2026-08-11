/** 管线状态管理 -- 三阶段进度追踪 (curator → analyst → reporter) + 任务列表 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { PipelineStep } from '@/types/message'
import { TOOL_ACTIONS, TOOL_LABELS } from '@/utils/toolLabels'

export interface TaskTool {
  name: string
  label: string
  status: 'active' | 'done'
}

export interface TaskStep {
  agent: string
  label: string
  goal: string
  status: 'pending' | 'active' | 'done'
  tools: TaskTool[]
}

const AGENT_LABELS: Record<string, string> = {
  curator: '数据准备', analyst: '分析引擎', reporter: '报告生成',
}

export const usePipelineStore = defineStore('pipeline', () => {
  const stageAgent = ref('')
  const dataReady = ref(false)
  const analysisDone = ref(false)
  const reportDone = ref(false)
  const pipelineAction = ref('')
  const tasks = ref<TaskStep[]>([])

  // ── 任务列表 (plan/stage/tool_call 事件驱动) ──

  function setTaskPlan(steps: { agent: string; goal?: string }[]) {
    tasks.value = steps.map(s => ({
      agent: s.agent,
      label: AGENT_LABELS[s.agent] || s.agent,
      goal: s.goal || '',
      status: 'pending',
      tools: [],
    }))
  }

  function setTaskStep(agent: string) {
    // 前一个 active → done (其子工具也全部 done)
    for (const st of tasks.value) {
      if (st.status === 'active') {
        st.status = 'done'
        st.tools.forEach(t => { t.status = 'done' })
      }
    }
    if (agent === 'finish') {
      tasks.value.forEach(st => { st.status = 'done'; st.tools.forEach(t => { t.status = 'done' }) })
      return
    }
    const step = tasks.value.find(s => s.agent === agent)
    if (step) step.status = 'active'
  }

  function addTaskTool(tool: string) {
    const step = tasks.value.find(s => s.status === 'active')
    if (!step) return
    const last = step.tools[step.tools.length - 1]
    if (last) last.status = 'done'
    step.tools.push({ name: tool, label: TOOL_LABELS[tool] || tool, status: 'active' })
  }

  const steps = computed<PipelineStep[]>(() => {
    const s: PipelineStep[] = [
      { key: 'data', label: '数据准备', status: 'pending' },
      { key: 'analysis', label: '分析引擎', status: 'pending' },
      { key: 'reporter', label: '报告生成', status: 'pending' },
    ]
    // 后端 stage 事件路由值: curator/analyst/reporter/finish
    // (路由到某阶段 = 前一阶段完成; finish = 全完成)
    if (stageAgent.value === 'curator') s[0].status = 'active'
    if (dataReady.value) s[0].status = 'done'
    if (stageAgent.value === 'analyst' && !analysisDone.value) s[1].status = 'active'
    if (analysisDone.value) s[1].status = 'done'
    if (stageAgent.value === 'reporter' && !reportDone.value) s[2].status = 'active'
    if (reportDone.value) s[2].status = 'done'
    return s
  })

  function setToolAction(tool: string) {
    pipelineAction.value = TOOL_ACTIONS[tool] || `正在执行 ${tool}`
  }

  function reset() {
    stageAgent.value = ''
    dataReady.value = false
    analysisDone.value = false
    reportDone.value = false
    pipelineAction.value = ''
    tasks.value = []
  }

  return {
    stageAgent, dataReady, analysisDone, reportDone,
    pipelineAction, tasks, steps,
    setToolAction, setTaskPlan, setTaskStep, addTaskTool, reset,
  }
})
