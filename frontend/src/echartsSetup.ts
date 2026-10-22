/** ECharts 按需注册与初始化包装（组件与测试都经此入口）。 */

import * as echarts from 'echarts/core'
import { HeatmapChart, LineChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  PolarComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  ScatterChart,
  HeatmapChart,
  LineChart,
  GridComponent,
  PolarComponent,
  TooltipComponent,
  LegendComponent,
  VisualMapComponent,
  CanvasRenderer,
])

export interface ChartLike {
  setOption(option: unknown): void
  resize(): void
  dispose(): void
  on(event: string, handler: (params: unknown) => void): void
  getOption?: () => unknown
  convertToPixel?: (finder: unknown, value: unknown) => unknown
  containPixel?: (finder: unknown, value: unknown) => boolean
  getZr?: () => { storage: { getDisplayList: () => unknown[] } }
}

export function initChart(element: HTMLElement): ChartLike {
  return echarts.init(element) as unknown as ChartLike
}
