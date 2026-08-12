/** ECharts 图表实例管理 Composable — 动态导入实现代码分割 */
import { ref, onBeforeUnmount } from 'vue'
import type { ECharts } from 'echarts'

/**
 * 自动补图例: 系列带 name 但 option 无 legend 时注入 (否则折线/柱状图
 * 无法区分哪条线/哪个柱子代表什么). 同时下移 grid 顶部防与图例重叠.
 */
function ensureLegend(option: Record<string, any>): Record<string, any> {
  if (!option || !Array.isArray(option.series) || option.series.length === 0) return option
  const hasNamedSeries = option.series.some(
    (s: any) => s && typeof s.name === 'string' && s.name.trim() !== ''
  )
  if (!hasNamedSeries || option.legend != null) return option
  const patched = { ...option, legend: { top: 0, left: 'center', textStyle: { fontSize: 11 } } }
  if (patched.grid) {
    const g = { ...patched.grid }
    const top = g.top
    if (typeof top === 'number' && top < 30) g.top = top + 28
    else if (typeof top === 'string' && top.replace('%', '') && parseFloat(top) < 12) g.top = '12%'
    patched.grid = g
  }
  return patched
}

export function useCharts() {
  const chartInstances = new Map<string, ECharts>()
  const chartRefs = ref<Record<string, HTMLElement>>({})

  function setChartRef(id: string, el: HTMLElement | null) {
    if (el) chartRefs.value[id] = el
  }

  async function renderChart(id: string, option: Record<string, any>) {
    const el = chartRefs.value[id]
    if (!el) return

    let instance = chartInstances.get(id)
    if (!instance) {
      const echarts = await import('echarts')
      instance = echarts.init(el)
      chartInstances.set(id, instance)
    }

    // resize 适配 — 先断开旧 observer 防止泄漏
    const existing = (el as any).__resizeObserver as ResizeObserver | undefined
    if (existing) existing.disconnect()
    const observer = new ResizeObserver(() => instance?.resize())
    observer.observe(el)
    ;(el as any).__resizeObserver = observer

    instance.setOption(ensureLegend(option), true)
  }

  function disposeChart(id: string) {
    const instance = chartInstances.get(id)
    if (instance) {
      instance.dispose()
      chartInstances.delete(id)
    }
    const el = chartRefs.value[id]
    if (el && (el as any).__resizeObserver) {
      ;(el as any).__resizeObserver.disconnect()
    }
  }

  function disposeAll() {
    chartInstances.forEach((inst) => inst.dispose())
    chartInstances.clear()
  }

  onBeforeUnmount(() => disposeAll())

  return { chartInstances, chartRefs, setChartRef, renderChart, disposeChart, disposeAll }
}
