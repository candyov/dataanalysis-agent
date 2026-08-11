/** ECharts 图表相关类型 */

export type ChartType = 'line' | 'bar' | 'pie' | 'scatter' | 'radar' | 'heatmap' | 'funnel'

export interface ChartConfig {
  title: string
  chart_type: ChartType
  echarts_option: Record<string, any>
}
