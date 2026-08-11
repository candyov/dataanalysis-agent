/** 报告-图表内联工具: 引用解析 + 模糊匹配 (纯函数, 可独立测试) */
import type { ReportSegment } from '@/types/message'

export interface ChartEntry {
  title: string
  chartType: string
  echartsOption: Record<string, any>
}

/** 图表池模糊匹配: 精确 → 去空格包含 */
export function findChart(pool: Map<string, ChartEntry>, title: string): ChartEntry | undefined {
  const exact = pool.get(title)
  if (exact) return exact
  const t = title.replace(/\s+/g, '')
  for (const [k, v] of pool) {
    const kk = k.replace(/\s+/g, '')
    if (kk.includes(t) || t.includes(kk)) return v
  }
  return undefined
}

/** 解析报告文本中的图表引用 → 拆成 [文本段, 图, 文本段…]
 *
 * 支持格式: (见图: X) / (见图：X) / 见图: X / 图: X / 图表: X
 * (无括号形式到句号/换行/分号结束)
 */
export function buildReportSegments(
  text: string,
  pool: Map<string, ChartEntry>,
): { segments: ReportSegment[]; embeddedTitles: string[] } {
  const segments: ReportSegment[] = []
  const embeddedTitles: string[] = []
  const anchorRe = /\(?(?:见图|图|图表)[:：]\s*([^)）\n。；;]+)\)?/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = anchorRe.exec(text))) {
    const chart = findChart(pool, m[1].trim())
    if (!chart) continue
    if (m.index > last) segments.push({ type: 'text', text: text.slice(last, m.index) })
    segments.push({ type: 'chart', title: chart.title, chartType: chart.chartType, echartsOption: chart.echartsOption })
    embeddedTitles.push(chart.title)
    last = anchorRe.lastIndex
  }
  if (last < text.length) segments.push({ type: 'text', text: text.slice(last) })
  return { segments, embeddedTitles }
}

/** 报告末尾兜底清单: 未被内联引用的图表附加到末尾 (引用格式漂移也不丢图) */
export function buildTailCharts(
  pool: Map<string, ChartEntry>,
  embeddedTitles: string[],
): ReportSegment[] {
  const embedded = new Set(embeddedTitles)
  const rest: ReportSegment[] = []
  for (const entry of pool.values()) {
    if (embedded.has(entry.title)) continue
    rest.push({ type: 'chart', title: entry.title, chartType: entry.chartType, echartsOption: entry.echartsOption })
  }
  return rest
}
