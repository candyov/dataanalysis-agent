/** 工具标签 — 单一来源 (后端工具 v2: Curator 3 + Analyst 8)
 *
 * 同步要求: 后端 ANALYST_TOOLS / CURATOR_TOOLS 变更时, 这里必须同步.
 * 不要在其他文件里再定义工具字典 — 统一从这里 import.
 */
export const TOOL_LABELS: Record<string, string> = {
  inspect: '扫描结构', assess_quality: '评估质量', date_range: '检测时间跨度',
  explore: '探索数据', test_difference: '验证差异', attribution: '归因分析',
  forecast: '预测趋势', seasonal_analysis: '分析季节性', compare: '环比对比',
  detect: '检测异常', build_chart: '生成图表',
}

export const TOOL_ICONS: Record<string, string> = {
  inspect: '🔍', assess_quality: '✅', date_range: '📅',
  explore: '📊', test_difference: '📏', attribution: '🎯',
  forecast: '🔮', seasonal_analysis: '🌊', compare: '⚖️',
  detect: '🚨', build_chart: '📉',
}

export const TOOL_ACTIONS: Record<string, string> = {
  inspect: '正在扫描数据结构', assess_quality: '正在评估数据质量', date_range: '正在检测时间跨度',
  explore: '正在探索数据', test_difference: '正在验证差异', attribution: '正在归因分析',
  forecast: '正在预测趋势', seasonal_analysis: '正在分析季节性', compare: '正在环比对比',
  detect: '正在检测异常', build_chart: '正在生成图表',
}
