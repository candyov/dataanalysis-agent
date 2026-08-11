<template>
  <div class="report-view">
    <!-- Loading -->
    <div v-if="loading" class="report-loading">
      <div class="spinner"></div>
      <p>加载报告中...</p>
    </div>

    <!-- Error: 仅当既无完整报告也无蓝图时才提示 (蓝图 404 不阻塞完整报告渲染) -->
    <div v-else-if="!reportSegments.length && !blueprint" class="report-error">
      <p>{{ error || '该会话暂无报告数据，请先完成一次分析' }}</p>
      <button @click="$emit('back')" class="btn-back">返回对话</button>
    </div>

    <!-- Report -->
    <div v-else class="report-body">
      <!-- top bar -->
      <div class="report-topbar">
        <button @click="$emit('back')" class="btn-back">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          返回对话
        </button>
      </div>
      <!-- Header -->
      <div class="report-header">
        <h1>{{ reportTitle }}</h1>
        <div class="subtitle">{{ subtitle }}</div>
        <div class="meta">
          <span>{{ dimCount }} 个维度</span>
          <span>{{ metricCount }} 个指标</span>
          <span>质量 {{ qualityGrade }}</span>
        </div>
      </div>

      <!-- KPI Cards -->
      <div v-if="kpis.length" class="kpi-grid">
        <div
          v-for="(kpi, i) in kpis"
          :key="i"
          class="kpi-card"
          :class="kpiColors[i % kpiColors.length]"
        >
          <div class="kpi-label">{{ kpi.label }}</div>
          <div class="kpi-value">{{ kpi.value }}</div>
          <div v-if="kpi.detail" class="kpi-sub">{{ kpi.detail }}</div>
        </div>
      </div>

      <!-- 数据质量 (蓝图 quality 章节保留, 管理层需要) -->
      <div
        v-for="ch in qualityChapters"
        :key="ch.id"
        class="section"
      >
        <div class="section-title">
          <span class="icon">{{ chapterIcon(ch.type) }}</span>
          {{ ch.title }}
        </div>
        <template v-if="ch.type === 'quality'">
          <div class="dq-grid">
            <div class="dq-item ok">
              <div class="dq-label">数据源</div>
              <div class="dq-value">{{ sessionId?.slice(0, 12) || '--' }}</div>
            </div>
            <div class="dq-item" :class="qualityClass">
              <div class="dq-label">综合质量等级</div>
              <div class="dq-value">{{ qualityGrade }}</div>
            </div>
            <div
              v-for="(b, bi) in (blueprint?.quality?.blockers || [])"
              :key="'b'+bi"
              class="dq-item danger"
            >
              <div class="dq-label">阻塞问题</div>
              <div class="dq-value" style="font-size:13px">{{ b.slice(0, 40) }}</div>
            </div>
          </div>
          <div v-if="qualityBlockers.length" class="insight-box warning">
            <h5>数据质量风险提示</h5>
            <ul>
              <li v-for="(b, bi) in qualityBlockers" :key="bi">{{ b }}</li>
            </ul>
          </div>
        </template>
      </div>

      <!-- 完整报告 (优先): 报告 segments 文字段 + 图内联, 与聊天流一致 -->
      <div v-if="reportSegments.length" class="section report-full">
        <template v-for="(seg, i) in reportSegments" :key="i">
          <div
            v-if="seg.type === 'text'"
            class="report-markdown"
            v-html="sanitizeHtml(markdownToHtml(seg.text || ''))"
          />
          <div v-else class="report-inline-chart">
            <div class="chart-box">
              <h4>{{ seg.title }}</h4>
              <div
                :id="'rv-' + i"
                :ref="el => setChartRef('rv-' + i, el as HTMLElement)"
                class="chart-wrapper"
              ></div>
            </div>
          </div>
        </template>
      </div>

      <!-- 兜底: 旧会话无报告 segments → 蓝图章节渲染 -->
      <template v-else>
        <div
          v-for="ch in chapters"
          :key="ch.id"
          class="section"
        >
          <div class="section-title">
            <span class="icon">{{ chapterIcon(ch.type) }}</span>
            {{ ch.title }}
          </div>
          <div v-if="ch.description" class="section-desc">{{ ch.description }}</div>

          <!-- Charts -->
          <div v-if="ch.charts && ch.charts.length" class="chart-row">
            <div
              v-for="(chart, ci) in ch.charts"
              :key="`${ch.id}_${ci}`"
              class="chart-box"
            >
              <h4>{{ chart.title }}</h4>
              <div
                :ref="el => setChartRef(chartRefId(ch.id, ci), el as HTMLElement)"
                class="chart-wrapper"
              ></div>
            </div>
          </div>

          <!-- Findings (insight box) -->
          <div
            v-if="ch.findings && ch.findings.length"
            class="insight-box"
          >
            <h5>{{ ch.title }}洞察</h5>
            <ul>
              <li v-for="(f, fi) in splitFindings(ch.findings)" :key="fi">
                {{ findingText(f) }}
              </li>
            </ul>
          </div>
        </div>
      </template>

      <!-- Footer -->
      <div class="report-footer">
        由 AI 数据分析平台自动生成
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useCharts } from '@/composables/useCharts'
import { sanitizeHtml, markdownToHtml } from '@/utils/markdown'

const props = defineProps<{
  sessionId: string
}>()

defineEmits<{ back: [] }>()

interface BlueprintChapter {
  id: string
  title: string
  type: string
  description?: string
  dimension?: string
  dimensions?: string[]
  charts?: ChartItem[]
  findings?: FindingItem[]
}

interface ChartItem {
  title?: string
  echarts_option?: any
  option?: any
  chart_type?: string
  categories?: string[]
  data?: any
  series?: any[]
}

type FindingItem = { claim?: string; level?: string; evidence?: string } | string

interface KpiItem {
  label: string
  value: string
  detail?: string
}

const blueprint = ref<any>(null)
const loading = ref(true)
const error = ref('')
const kpis = ref<KpiItem[]>([])
// 完整报告: 从会话 messages 提取最后一条 bot 报告消息的 segments (文字+图内联)
const reportSegments = ref<any[]>([])
const { setChartRef, renderChart, disposeAll } = useCharts()

const chapters = computed<BlueprintChapter[]>(() => {
  const all = blueprint.value?.chapters || []
  // 隐藏空章节: 无图表且无发现的章节不渲染 (如未出图的交叉分析/Top N)
  return all.filter(ch => {
    if (ch.type === 'quality') return true
    return (ch.charts && ch.charts.length > 0) || (ch.findings && ch.findings.length > 0)
  })
})

// 数据质量章节独立渲染 (完整报告模式下仍保留, 管理层需要)
const qualityChapters = computed<BlueprintChapter[]>(() => {
  return (blueprint.value?.chapters || []).filter((ch: any) => ch.type === 'quality')
})

const reportTitle = computed(() => {
  return blueprint.value?.user_request || '数据分析报告'
})

const subtitle = computed(() => {
  const bp = blueprint.value
  if (!bp) return ''
  const parts: string[] = []
  if (props.sessionId) parts.push(`会话: ${props.sessionId.slice(0, 12)}`)
  const ov = bp.overview || {}
  if (ov.time_span) parts.push(`时间跨度: ${ov.time_span}`)
  if (ov.row_count) parts.push(`${ov.row_count} 条记录`)
  return parts.join(' | ')
})

const dimCount = computed(() => (blueprint.value?.dimensions || []).length)
const metricCount = computed(() => (blueprint.value?.metrics || []).length)
const qualityGrade = computed(() => blueprint.value?.quality?.grade || 'B')
const qualityClass = computed(() => {
  const g = qualityGrade.value
  return g === 'A' ? 'ok' : g === 'B' ? '' : g === 'C' ? 'warning' : 'danger'
})
const qualityBlockers = computed(() => blueprint.value?.quality?.blockers || [])

const kpiColors = ['', 'green', 'orange', 'purple']

function chapterIcon(type: string): string {
  const icons: Record<string, string> = {
    quality: '🔍', time_series: '📈', group_compare: '📊',
    cross_analysis: '🔥', year_over_year: '📅', top_n: '🏆',
  }
  return icons[type] || '📋'
}

function chartRefId(chapterId: string, chartIdx: number): string {
  return `rp_${chapterId}_${chartIdx}`
}

function findingText(f: FindingItem): string {
  if (typeof f === 'string') return f
  return f?.claim || JSON.stringify(f)
}

function splitFindings(findings: FindingItem[]): FindingItem[] {
  const result: FindingItem[] = []
  for (const f of findings) {
    if (typeof f === 'string' && f.length > 300) {
      // 超长文本: 先按换行, 无换行再按句号/分号拆成独立条目 (洞察框逐条展示)
      const byLine = f.split('\n').map(l => l.trim()).filter(l => l.length > 5)
      if (byLine.length > 1) {
        result.push(...byLine)
      } else {
        const bySentence = f.split(/[。；;]\s*/).map(s => s.trim()).filter(s => s.length > 8)
        result.push(...(bySentence.length > 1 ? bySentence : [f]))
      }
    } else {
      result.push(f)
    }
  }
  return result
}

function toEchartsOption(c: ChartItem): any {
  if (c.echarts_option || c.option) return c.echarts_option || c.option

  const ctype = c.chart_type || 'bar'
  const categories = c.categories || []
  const data = c.data
  const series = c.series

  if (ctype === 'pie') {
    let pieData = data
    if (!pieData && categories && Array.isArray(data)) {
      pieData = categories.map((name, i) => ({ name, value: Array.isArray(data) ? data[i] : 0 }))
    }
    return {
      title: { text: c.title || '', left: 'center' },
      tooltip: { trigger: 'item' },
      series: [{ type: 'pie', radius: '60%', data: pieData || [] }],
    }
  }

  if (ctype === 'line' || ctype === 'bar') {
    let echartsSeries = series
    if (!echartsSeries && categories && data) {
      echartsSeries = [{ name: c.title || '', type: ctype, data: Array.isArray(data) ? data : [] }]
    }
    if (categories.length > 0 || (echartsSeries && echartsSeries.length > 0)) {
      return {
        title: { text: c.title || '' },
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: categories },
        yAxis: { type: 'value' },
        series: echartsSeries || [],
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
      }
    }
  }

  return null
}

function renderAllCharts() {
  chapters.value.forEach(ch => {
    if (!ch.charts) return
    ch.charts.forEach((c, ci) => {
      const eo = toEchartsOption(c)
      if (!eo) return
      nextTick(() => {
        renderChart(chartRefId(ch.id, ci), eo)
      })
    })
  })
}

function renderReportCharts() {
  reportSegments.value.forEach((seg, i) => {
    if (seg.type === 'chart' && seg.echartsOption) {
      renderChart('rv-' + i, seg.echartsOption)
    }
  })
}

/** 从会话消息提取完整报告 (最后一条带 segments 的 bot 消息 = 报告 + 内联图) */
async function loadReport() {
  try {
    const res = await fetch(`/api/v1/sessions/${props.sessionId}`)
    if (!res.ok) return
    const json = await res.json()
    const msgs: any[] = json.messages || []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]
      if (m.role === 'bot' && (m.segments || []).length) {
        reportSegments.value = (m.segments || []).map((s: any) =>
          s.type === 'chart'
            ? { type: 'chart', title: s.title || '', echartsOption: s.echarts_option || s.echartsOption || {} }
            : { type: 'text', text: s.text || '' }
        )
        return
      }
    }
  } catch {
    // 加载失败 → 兜底蓝图章节渲染
  }
}

async function loadBlueprint() {
  loading.value = true
  error.value = ''
  try {
    const res = await fetch(`/api/v1/sessions/${props.sessionId}/data`)
    if (!res.ok) {
      if (res.status === 404) {
        // 无蓝图 (如 curator 缓存命中场景, 会话只有报告没有 blueprint) —
        // 不设 error, 完整报告 (reportSegments) 照常渲染
        blueprint.value = null
        return
      }
      throw new Error(`加载失败 (${res.status})`)
    }
    const json = await res.json()
    blueprint.value = json.report_blueprint || json
    kpis.value = extractKpis(blueprint.value)
  } catch (e: any) {
    error.value = e.message || '加载报告失败'
  } finally {
    loading.value = false
  }
}

function extractKpis(bp: any): KpiItem[] {
  const result: KpiItem[] = []
  const overview = bp?.overview || {}
  if (overview.row_count) {
    result.push({ label: '记录数', value: String(overview.row_count), detail: '原始数据' })
  }
  const dims = bp?.dimensions || []
  result.push({ label: '维度', value: String(dims.length) })
  const metrics = bp?.metrics || []
  result.push({ label: '指标', value: String(metrics.length) })
  result.push({ label: '质量', value: bp?.quality?.grade || 'B' })
  return result
}

watch(blueprint, () => {
  nextTick(() => renderAllCharts())
})
watch(reportSegments, () => {
  nextTick(() => renderReportCharts())
})

onMounted(() => {
  if (props.sessionId) {
    loadBlueprint()
    loadReport()
  }
})

onBeforeUnmount(() => disposeAll())
</script>

<style scoped>
.report-view { height: 100%; overflow-y: auto; padding: 24px; }
.report-loading, .report-error {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 200px; gap: 16px; color: var(--text-secondary);
}
.spinner { width: 32px; height: 32px; border: 3px solid var(--border-primary); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.btn-back {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 14px; border: 1px solid var(--border-primary); border-radius: 8px;
  background: var(--bg-surface); cursor: pointer; font-size: 13px; color: var(--text-secondary);
}
.btn-back:hover { background: var(--bg-hover); border-color: var(--border-primary); }
.btn-back svg { flex-shrink: 0; }

.report-topbar { max-width: 1200px; margin: 0 auto 16px; }

.report-body { max-width: 1200px; margin: 0 auto; }
.report-header { background: linear-gradient(135deg, #1e40af, #3b82f6); color: #fff; border-radius: 16px; padding: 32px 40px; margin-bottom: 24px; }
.report-header h1 { font-size: 28px; font-weight: 700; margin-bottom: 8px; }
.report-header .subtitle { font-size: 14px; opacity: .85; }
.report-header .meta { display: flex; gap: 24px; margin-top: 16px; font-size: 13px; opacity: .9; flex-wrap: wrap; }

.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
.kpi-card { background: var(--bg-surface); border-radius: 12px; padding: 20px 24px; box-shadow: var(--shadow-sm); border-left: 4px solid var(--accent); transition: transform .2s; }
.kpi-card:hover { transform: translateY(-2px); }
.kpi-card.green { border-left-color: var(--success); }
.kpi-card.orange { border-left-color: var(--warning); }
.kpi-card.purple { border-left-color: #a78bfa; }
.kpi-label { font-size: 13px; color: var(--text-muted); margin-bottom: 6px; }
.kpi-value { font-size: 26px; font-weight: 700; color: var(--text-primary); }
.kpi-sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

.section { background: var(--bg-surface); border-radius: 12px; padding: 24px 28px; margin-bottom: 24px; box-shadow: var(--shadow-sm); }
.section-title { font-size: 18px; font-weight: 700; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; color: var(--text-primary); }
.section-desc { font-size: 13px; color: var(--text-secondary); margin-bottom: 20px; }

.dq-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 16px; }
.dq-item { background: var(--bg-elevated); border-radius: 10px; padding: 16px 20px; border: 1px solid var(--border-primary); }
.dq-item.warning { border-color: var(--warning); background: rgba(251, 191, 36, 0.08); }
.dq-item.danger { border-color: var(--danger); background: var(--danger-dim); }
.dq-item.ok { border-color: var(--success); background: rgba(52, 211, 153, 0.10); }
.dq-label { font-size: 13px; color: var(--text-muted); }
.dq-value { font-size: 22px; font-weight: 700; color: var(--text-primary); }

.chart-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 20px; margin-bottom: 16px; }
.chart-box { background: var(--bg-elevated); border-radius: 10px; padding: 16px; border: 1px solid var(--border-primary); }
.chart-box h4 { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: var(--text-primary); }
.chart-wrapper { height: 300px; }

.insight-box { background: rgba(99, 102, 241, 0.10); border-left: 4px solid var(--accent); border-radius: 8px; padding: 16px 20px; margin-top: 16px; }
.insight-box.warning { background: rgba(251, 191, 36, 0.08); border-left-color: var(--warning); }
.insight-box h5 { font-size: 14px; font-weight: 700; margin-bottom: 8px; color: var(--text-primary); }
.insight-box ul { padding-left: 20px; }
.insight-box li { font-size: 13px; margin-bottom: 4px; color: var(--text-secondary); }

.report-footer { text-align: center; padding: 24px; color: var(--text-muted); font-size: 13px; }

/* 完整报告: markdown 排版与聊天消息一致 */
.report-markdown { font-size: 14px; color: var(--text-primary); line-height: 1.8; }
.report-markdown h1, .report-markdown h2, .report-markdown h3, .report-markdown h4 {
  margin: 18px 0 8px; font-weight: 700; color: var(--text-primary);
}
.report-markdown h1 { font-size: 20px; } .report-markdown h2 { font-size: 18px; }
.report-markdown h3 { font-size: 16px; } .report-markdown h4 { font-size: 14px; }
.report-markdown p { margin: 8px 0; }
.report-markdown ul, .report-markdown ol { margin: 8px 0; padding-left: 20px; }
.report-markdown li { margin: 4px 0; }
.report-markdown code {
  background: var(--bg-elevated); padding: 2px 6px; border-radius: 4px;
  font-size: 12px; font-family: var(--font-mono);
}
.report-markdown strong { font-weight: 600; color: var(--accent); }
.report-markdown table {
  border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 13px;
  border-radius: 8px; overflow: hidden;
}
.report-markdown th {
  background: var(--bg-elevated); font-weight: 600; padding: 8px 12px;
  color: var(--text-secondary); text-align: left; border-bottom: 2px solid var(--border-primary);
}
.report-markdown td { padding: 8px 12px; border-bottom: 1px solid var(--border-subtle); }
.report-inline-chart { margin: 18px 0; }
</style>
