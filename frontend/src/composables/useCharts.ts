/** ECharts 图表实例管理 Composable — 动态导入实现代码分割 */
import { ref, onBeforeUnmount } from 'vue'
import type { ECharts } from 'echarts'

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

    instance.setOption(option, true)
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
