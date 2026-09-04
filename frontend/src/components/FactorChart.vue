<script setup>
import { BarChart, CandlestickChart, HeatmapChart, LineChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, MarkPointComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

echarts.use([BarChart, CandlestickChart, HeatmapChart, LineChart, DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, MarkPointComponent, TooltipComponent, VisualMapComponent, CanvasRenderer])

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: String, default: '360px' },
})

const chartElement = ref(null)
let chart = null
let resizeObserver = null

function render() {
  if (!chart || !props.option) return
  chart.setOption(props.option, { notMerge: true, lazyUpdate: true })
}

onMounted(() => {
  chart = echarts.init(chartElement.value, 'dark', { renderer: 'canvas' })
  render()
  resizeObserver = new ResizeObserver(() => chart?.resize())
  resizeObserver.observe(chartElement.value)
})

watch(() => props.option, render, { deep: true })

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="chartElement" class="factor-chart" :style="{ height }"></div>
</template>
