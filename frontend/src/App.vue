<script setup>
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { consumeSSE } from './sse'

const FactorChart = defineAsyncComponent(() => import('./components/FactorChart.vue'))

function localDateValue(value = new Date()) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

const todayDate = localDateValue()
const symbol = ref('600519')
const market = ref('自动')
const horizon = ref('波段')
const focus = ref('综合分析')
const analysisPeriod = ref('6mo')
const snapshot = ref(null)
const analysis = ref('')
const loading = ref(false)
const error = ref('')
const health = ref({ status: 'loading', mode: 'demo' })
const databaseStatus = ref(null)
const databaseStatusLoading = ref(false)
const databaseFreshness = ref(null)
const databaseFreshnessLoading = ref(false)
const databaseFreshnessError = ref('')
const controller = ref(null)
const activeTab = ref('analysis')
const backtestStrategy = ref('均线交叉')
const backtestScope = ref('market')
const backtestStartDate = ref('2020-01-01')
const backtestEndDate = ref(todayDate)
const customStrategyCode = ref('')
const backtestCandles = ref(null)
const initialCapital = ref(100000)
const feeRate = ref(0.0003)
const slippageBps = ref(5)
const adjustmentMode = ref('前复权')
const fundamentalScoreEnabled = ref(false)
const fundamentalScoreThreshold = ref(60)
const marketSignalThreshold = ref(300)
const marketSignalRateThresholdPct = ref(0)
const marketVolumeRatioEnabled = ref(false)
const benchmarkDropFilterEnabled = ref(false)
const benchmarkDropThresholdPct = ref(4)
const backtestResult = ref(null)
const backtestLoading = ref(false)
const backtestError = ref('')
const backtestPeriod = ref('2y')
const batchSymbols = ref('600519\n000858\n601318\n600036\n000333\n300750\n600276\n601888\n600030\n601012\n510300\n510050\n510500\n512100\n159870\n515880\n159992\n512480\n588000\n512690\n512880')
const batchPeriod = ref('5y')
const batchResult = ref(null)
const batchLoading = ref(false)
const batchError = ref('')
const chartHover = ref(null)
const backtestHover = ref(null)
const newsQuery = ref('贵州茅台')
const newsLookback = ref('3d')
const newsFocus = ref('事件影响与风险')
const newsItems = ref([])
const newsAnalysis = ref('')
const newsLoading = ref(false)
const newsError = ref('')
const autoRefresh = ref(0)
const lastNewsUpdate = ref(null)
let newsTimer = null
const marketSnapshot = ref(null)
const marketLoading = ref(false)
const marketError = ref('')
const selectedIndicators = ref(['MA', 'MACD', 'RSI', 'KDJ', 'ATR', '九转', 'BOLL'])
const strategyDescription = ref('价格站上 MA20 且 MA20 向上、RSI14 小于 45 时买入；收盘跌破 MA20 或 RSI14 大于 75 时卖出')
const strategyCode = ref('')
const strategyExplanation = ref('')
const strategyLoading = ref(false)
const taskState = ref(null)
const taskLoading = ref(false)
const syncMode = ref('incremental')
const syncPriceScope = ref('front_only')
const syncConcurrency = ref(4)
const scanSignals = ref([])
const trackedPositions = ref([])
const records = ref([])
const recordsLoading = ref(false)
const recordDetail = ref(null)
const selectedRecordSummary = ref(null)
const selectedRecordId = ref('')
const recordQuery = ref('')
const recordStrategy = ref('')
const recordSortBy = ref('symbol')
const recordSortOrder = ref('asc')
const recordOffset = ref(0)
const recordLimit = 100
const selectedRecord = computed(() => records.value.find((record) => record.id === selectedRecordId.value) || null)
const compareRunIds = ref([])
const comparisonResult = ref(null)
const comparisonLoading = ref(false)
const comparisonError = ref('')
let taskTimer = null
const poolStrategy = ref('RSI反转+止盈止损')
const poolAdjustmentMode = ref('动态前复权')
const poolCandidateRanking = ref('rsi_rebound_score')
const poolInitialCapital = ref(1000000)
const poolExposureLimit = ref(100)
const poolSinglePositionLimit = ref(4)
const poolMaxPositions = ref(25)
const poolMaxOpeningGapPct = ref(3)
const poolMinimumTurnoverWan = ref(0)
const poolMarketSignalThreshold = ref(300)
const poolMarketSignalRateThresholdPct = ref(0)
const poolBenchmarkDropFilterEnabled = ref(false)
const poolBenchmarkDropThresholdPct = ref(4)
const poolVolumeRatioEnabled = ref(false)
const poolFundamentalScoreEnabled = ref(false)
const poolFundamentalScoreThreshold = ref(60)
const poolSlippageBps = ref(5)
const poolRequireAboveMa200 = ref(false)
const poolRequireMa200Rising = ref(false)
const poolTaskState = ref(null)
const poolLoading = ref(false)
const poolPlanState = ref(null)
const poolPlanLoading = ref(false)
let poolTimer = null
let poolPlanTimer = null
const poolFactorWeights = ref({ low_volatility_20: 30, fundamental_score: 25, momentum_60: 20, roe: 15, reversal_5: 10 })
const poolTradeSymbol = ref('')
const poolTradeCandles = ref(null)
const poolTradeChartLoading = ref(false)
const poolTradeChartError = ref('')
const poolFocusedTrade = ref(null)
const poolTradePage = ref(1)
const poolTradePageSize = 50
const factorCatalog = ref([])
const selectedFactors = ref(['momentum_20', 'low_volatility_20', 'volume_ratio_5_20', 'fundamental_score'])
const factorStartDate = ref('2020-01-01')
const factorEndDate = ref(todayDate)
const factorForwardDays = ref(20)
const factorUniverseLimit = ref(1200)
const factorResult = ref(null)
const factorLoading = ref(false)
const factorError = ref('')

marked.setOptions({ breaks: true })
const renderedAnalysis = computed(() => DOMPurify.sanitize(marked.parse(analysis.value || '')))
const renderedNewsAnalysis = computed(() => DOMPurify.sanitize(marked.parse(newsAnalysis.value || '')))
const positive = computed(() => Number(snapshot.value?.change_pct || 0) >= 0)
const candleBars = computed(() => {
  const rows = snapshot.value?.points || []
  if (!rows.length) return []
  const step = Math.max(1, Math.ceil(rows.length / 320))
  const visible = rows.filter((_, index) => index % step === 0 || index === rows.length - 1)
  const lows = visible.map((row) => Number(row.low ?? row.close))
  const highs = visible.map((row) => Number(row.high ?? row.close))
  const min = Math.min(...lows)
  const max = Math.max(...highs)
  const span = max - min || 1
  const y = (value) => 225 - ((Number(value) - min) / span) * 205
  const width = Math.max(1.2, Math.min(8, 760 / visible.length))
  return visible.map((row, index) => {
    const open = Number(row.open ?? row.close)
    const close = Number(row.close)
    const high = Number(row.high ?? close)
    const low = Number(row.low ?? close)
    const x = ((index + 0.5) / visible.length) * 1000
    return { row, x, width, yOpen: y(open), yClose: y(close), yHigh: y(high), yLow: y(low), bodyY: Math.min(y(open), y(close)), bodyHeight: Math.max(1.4, Math.abs(y(open) - y(close))), up: close >= open }
  })
})
function buildCandleChart(rows) {
  if (!rows?.length) return { bars: [], lines: {}, min: 0, max: 1 }
  const step = Math.max(1, Math.ceil(rows.length / 320))
  const visible = rows.filter((_, index) => index % step === 0 || index === rows.length - 1)
  const min = Math.min(...visible.map((row) => Number(row.low ?? row.close)))
  const max = Math.max(...visible.map((row) => Number(row.high ?? row.close)))
  const span = max - min || 1
  const y = (value) => 225 - ((Number(value) - min) / span) * 205
  const width = Math.max(1.2, Math.min(8, 760 / visible.length))
  const bars = visible.map((row, index) => {
    const open = Number(row.open ?? row.close), close = Number(row.close), high = Number(row.high ?? close), low = Number(row.low ?? close)
    const x = ((index + 0.5) / visible.length) * 1000
    return { row, x, width, yOpen: y(open), yClose: y(close), yHigh: y(high), yLow: y(low), bodyY: Math.min(y(open), y(close)), bodyHeight: Math.max(1.4, Math.abs(y(open) - y(close))), up: close >= open }
  })
  const lineKeys = ['ma20', 'ma60', 'ma120', 'ma200']
  const lines = Object.fromEntries(lineKeys.map((key) => [key, visible.filter((row) => row[key] !== null && row[key] !== undefined).map((row) => {
    const index = visible.indexOf(row)
    return `${(((index + 0.5) / visible.length) * 1000).toFixed(1)},${y(row[key]).toFixed(1)}`
  }).join(' ')]))
  return { bars, lines, min, max, y, visible }
}
const marketCandleChart = computed(() => buildCandleChart(marketSnapshot.value?.points || []))
const backtestCandleChart = computed(() => {
  const chart = buildCandleChart(backtestCandles.value?.points || [])
  if (!chart.visible?.length || !backtestResult.value) return { ...chart, markers: [] }
  const first = Number(chart.visible[0].timestamp), last = Number(chart.visible.at(-1).timestamp), span = last - first || 1
  const markers = (backtestResult.value.trade_events || []).map((event) => ({
    ...event,
    x: Math.max(5, Math.min(995, (Number(event.timestamp) - first) / span * 1000)),
    y: chart.y(event.price),
  }))
  return { ...chart, markers }
})
const backtestPoints = computed(() => {
  const rows = backtestResult.value?.curve || []
  if (rows.length < 2) return { equity: '', benchmark: '', markers: [] }
  const values = [...rows.map((row) => row.equity), ...rows.map((row) => row.benchmark)]
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const make = (key) => rows.map((row, index) => `${(index / (rows.length - 1) * 1000).toFixed(1)},${(220 - (row[key] - min) / span * 200).toFixed(1)}`).join(' ')
  const markers = (backtestResult.value?.trade_events || []).map((event) => ({
    ...event,
    x: event.curve_index / (rows.length - 1) * 1000,
    y: 220 - (event.equity - min) / span * 200,
  }))
  return { equity: make('equity'), benchmark: make('benchmark'), markers }
})
const poolPerformanceOption = computed(() => {
  const result = poolTaskState.value?.result
  const rows = result?.curve || []
  const capital = Number(result?.initial_capital) || 1
  return {
    backgroundColor: 'transparent', color: ['#57e3ff', '#8a949e', '#f7d154'],
    tooltip: { trigger: 'axis', valueFormatter: value => Number(value).toFixed(2) },
    legend: { top: 4, textStyle: { color: '#87919c' } },
    grid: { left: 62, right: 62, top: 58, bottom: 60 },
    xAxis: { type: 'time', axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
    yAxis: [
      { type: 'value', name: '净值（初始100）', nameTextStyle: { color: '#68737e' }, axisLabel: { color: '#68737e' }, splitLine: { lineStyle: { color: '#20262e' } }, scale: true },
      { type: 'value', name: '仓位', min: 0, max: 100, axisLabel: { color: '#68737e', formatter: '{value}%' }, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8, borderColor: '#2b323b', backgroundColor: '#11151a' }],
    series: [
      { name: '资金池策略', type: 'line', showSymbol: false, lineStyle: { width: 2.5 }, data: rows.map(row => [Number(row.timestamp) * 1000, Number(row.equity) / capital * 100]) },
      { name: '沪深300', type: 'line', showSymbol: false, lineStyle: { width: 1.5, type: 'dashed' }, data: rows.map(row => [Number(row.timestamp) * 1000, Number(row.benchmark) / capital * 100]) },
      { name: '仓位', type: 'line', yAxisIndex: 1, showSymbol: false, lineStyle: { width: 1, opacity: 0.7 }, areaStyle: { opacity: 0.06 }, data: rows.map(row => [Number(row.timestamp) * 1000, Number(row.exposure_pct || 0)]) },
    ],
  }
})
const poolRiskOption = computed(() => {
  const result = poolTaskState.value?.result
  const rows = result?.curve || []
  let strategyPeak = 0
  let benchmarkPeak = 0
  const strategy = []
  const benchmark = []
  for (const row of rows) {
    const equity = Number(row.equity)
    const benchmarkEquity = Number(row.benchmark)
    strategyPeak = Math.max(strategyPeak, equity)
    benchmarkPeak = Math.max(benchmarkPeak, benchmarkEquity)
    strategy.push([Number(row.timestamp) * 1000, strategyPeak > 0 ? (equity / strategyPeak - 1) * 100 : 0])
    benchmark.push([Number(row.timestamp) * 1000, benchmarkPeak > 0 ? (benchmarkEquity / benchmarkPeak - 1) * 100 : 0])
  }
  return {
    backgroundColor: 'transparent', color: ['#ff6476', '#747f89'],
    tooltip: { trigger: 'axis', valueFormatter: value => `${Number(value).toFixed(2)}%` },
    legend: { top: 4, textStyle: { color: '#87919c' } },
    grid: { left: 62, right: 30, top: 52, bottom: 42 },
    xAxis: { type: 'time', axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
    yAxis: { type: 'value', name: '回撤 %', max: 0, axisLabel: { color: '#68737e', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#20262e' } } },
    dataZoom: [{ type: 'inside' }],
    series: [
      { name: '策略回撤', type: 'line', showSymbol: false, areaStyle: { opacity: 0.16 }, data: strategy },
      { name: '沪深300回撤', type: 'line', showSymbol: false, lineStyle: { type: 'dashed' }, data: benchmark },
    ],
  }
})
const poolTradeSummary = computed(() => {
  const events = poolTaskState.value?.result?.trade_events || []
  const sells = events.filter(event => event.side === 'S')
  const realized = sells.reduce((sum, event) => sum + Number(event.pnl || 0), 0)
  const turnover = events.reduce((sum, event) => sum + Number(event.price || 0) * Number(event.shares || 0), 0)
  return { orders: events.length, buys: events.length - sells.length, sells: sells.length, realized, turnover }
})
const poolTradeSymbols = computed(() => {
  const result = new Map()
  for (const event of poolTaskState.value?.result?.trade_events || []) result.set(event.symbol, event.name || event.symbol)
  return [...result.entries()].map(([symbol, name]) => ({ symbol, name }))
})
const poolTradePageCount = computed(() => Math.max(1, Math.ceil((poolTaskState.value?.result?.trade_events?.length || 0) / poolTradePageSize)))
const poolVisibleTradeEvents = computed(() => {
  const events = poolTaskState.value?.result?.trade_events || []
  const start = (poolTradePage.value - 1) * poolTradePageSize
  return events.slice(start, start + poolTradePageSize)
})
const poolTradeCandleOption = computed(() => {
  const rows = poolTradeCandles.value?.points || []
  const events = (poolTaskState.value?.result?.trade_events || []).filter(event => event.symbol === poolTradeSymbol.value)
  const dates = rows.map(row => new Date(Number(row.timestamp) * 1000).toISOString().slice(0, 10))
  const focusTimestamp = Number(poolFocusedTrade.value?.timestamp || 0)
  const focusSide = poolFocusedTrade.value?.side
  const focusIndex = focusTimestamp ? rows.findIndex(row => Number(row.timestamp) === focusTimestamp) : -1
  const visibleCount = Math.min(120, Math.max(40, rows.length))
  const startIndex = focusIndex >= 0 ? Math.max(0, focusIndex - Math.floor(visibleCount / 2)) : Math.max(0, rows.length - visibleCount)
  const endIndex = Math.min(rows.length - 1, startIndex + visibleCount)
  const zoomStart = rows.length > 1 ? startIndex / (rows.length - 1) * 100 : 0
  const zoomEnd = rows.length > 1 ? endIndex / (rows.length - 1) * 100 : 100
  const markers = events.map(event => {
    const focused = Number(event.timestamp) === focusTimestamp && event.side === focusSide
    return {
    name: event.side === 'B' ? '买入' : '卖出',
    coord: [new Date(Number(event.timestamp) * 1000).toISOString().slice(0, 10), Number(event.price)],
    value: event.side,
    symbol: 'pin', symbolSize: focused ? 68 : 48,
    itemStyle: { color: event.side === 'B' ? '#ff6476' : '#35dc91', borderColor: focused ? '#ffffff' : 'transparent', borderWidth: focused ? 2 : 0 },
    label: { color: '#071014', fontWeight: 800, formatter: event.side },
    trade: event,
  }})
  return {
    backgroundColor: 'transparent', color: ['#f7d154', '#57e3ff'],
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: { top: 4, data: ['K线', 'MA20', 'MA60'], textStyle: { color: '#87919c' } },
    grid: { left: 62, right: 30, top: 58, bottom: 68 },
    xAxis: { type: 'category', data: dates, boundaryGap: true, axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#68737e' }, splitLine: { lineStyle: { color: '#20262e' } } },
    dataZoom: [{ type: 'inside', start: zoomStart, end: zoomEnd }, { type: 'slider', height: 18, bottom: 8, start: zoomStart, end: zoomEnd }],
    series: [
      { name: 'K线', type: 'candlestick', data: rows.map(row => [Number(row.open), Number(row.close), Number(row.low), Number(row.high)]), itemStyle: { color: '#ff6476', color0: '#35dc91', borderColor: '#ff6476', borderColor0: '#35dc91' }, markPoint: { data: markers, tooltip: { formatter: params => { const trade = params.data.trade || {}; return `${params.data.name} · ${new Date(Number(trade.timestamp) * 1000).toLocaleDateString('zh-CN')}<br/>成交价：${formatMoney(trade.price, 4)}<br/>数量：${Number(trade.shares || 0).toLocaleString()}股${trade.pnl == null ? '' : `<br/>盈亏：¥${formatMoney(trade.pnl)}`}<br/>${trade.reason || ''}` } } } },
      { name: 'MA20', type: 'line', showSymbol: false, smooth: true, lineStyle: { width: 1.2 }, data: rows.map(row => row.ma20 == null ? null : Number(row.ma20)) },
      { name: 'MA60', type: 'line', showSymbol: false, smooth: true, lineStyle: { width: 1.2 }, data: rows.map(row => row.ma60 == null ? null : Number(row.ma60)) },
    ],
  }
})

function comparisonCurvePoints(rows = []) {
  if (rows.length < 2) return { equity: '', benchmark: '' }
  const values = [...rows.map((row) => Number(row.equity)), ...rows.map((row) => Number(row.benchmark))].filter(Number.isFinite)
  const min = Math.min(...values), max = Math.max(...values), span = max - min || 1
  const make = (key) => rows.map((row, index) => `${(index / (rows.length - 1) * 1000).toFixed(1)},${(220 - (Number(row[key]) - min) / span * 200).toFixed(1)}`).join(' ')
  return { equity: make('equity'), benchmark: make('benchmark') }
}

function comparisonCurveItems(strategies = []) {
  return strategies.filter((item) => item.portfolio_curve?.length > 1).map((item) => ({ ...item, points: comparisonCurvePoints(item.portfolio_curve) }))
}

const marketCurveItems = computed(() => comparisonCurveItems(taskState.value?.result?.strategies || []))
const batchCurveItems = computed(() => comparisonCurveItems(batchResult.value?.summaries || []))
const recordCurveItems = computed(() => comparisonCurveItems(selectedRecordSummary.value?.summary?.strategies || []))
const comparisonCurveRows = computed(() => comparisonCurveItems(comparisonResult.value?.rows || []))
const recordPortfolioOption = computed(() => {
  const strategies = (selectedRecordSummary.value?.summary?.strategies || []).filter(item => item.portfolio_curve?.length)
  const series = strategies.map((item, index) => ({
    name: item.strategy, type: 'line', showSymbol: false,
    lineStyle: { width: 2 },
    data: item.portfolio_curve.map(point => [Number(point.timestamp) * 1000, Number(point.equity)]),
  }))
  if (strategies.length) {
    series.push({
      name: '沪深300', type: 'line', showSymbol: false,
      lineStyle: { width: 1.5, type: 'dashed', color: '#7b858f' },
      data: strategies[0].portfolio_curve.map(point => [Number(point.timestamp) * 1000, Number(point.benchmark)]),
    })
  }
  return {
    backgroundColor: 'transparent', color: factorColors,
    tooltip: { trigger: 'axis', valueFormatter: value => Number(value).toFixed(2) },
    legend: { type: 'scroll', top: 4, textStyle: { color: '#87919c' } },
    grid: { left: 62, right: 32, top: 58, bottom: 60 },
    xAxis: { type: 'time', axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
    yAxis: { type: 'value', name: '组合净值（初始100）', scale: true, axisLabel: { color: '#68737e' }, splitLine: { lineStyle: { color: '#20262e' } } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
    series,
  }
})

const factorColors = ['#57e3ff', '#35dc91', '#f7d154', '#ff6476', '#c985ff', '#ff9f43', '#6c8cff', '#d7e1e8', '#20c997', '#ffffff']
const factorMetricOption = computed(() => {
  const rows = factorResult.value?.summaries || []
  return {
    backgroundColor: 'transparent', color: factorColors,
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 4, textStyle: { color: '#87919c' } },
    grid: { left: 54, right: 54, top: 55, bottom: 52 },
    xAxis: { type: 'category', data: rows.map(row => factorResult.value.labels[row.factor]), axisLabel: { color: '#87919c', rotate: 18 }, axisLine: { lineStyle: { color: '#2b323b' } } },
    yAxis: [
      { type: 'value', name: 'Mean IC', nameTextStyle: { color: '#68737e' }, axisLabel: { color: '#68737e' }, splitLine: { lineStyle: { color: '#20262e' } } },
      { type: 'value', name: 'IC IR', nameTextStyle: { color: '#68737e' }, axisLabel: { color: '#68737e' }, splitLine: { show: false } },
    ],
    series: [
      { name: 'Mean IC', type: 'bar', barMaxWidth: 36, data: rows.map(row => row.mean_ic) },
      { name: 'IC IR', type: 'line', yAxisIndex: 1, symbolSize: 7, data: rows.map(row => row.ic_ir) },
    ],
  }
})
const factorCurveOption = computed(() => ({
  backgroundColor: 'transparent', color: factorColors,
  tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 4, textStyle: { color: '#87919c' } },
  grid: { left: 58, right: 28, top: 58, bottom: 54 },
  xAxis: { type: 'time', axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
  yAxis: { type: 'value', name: '累计多空收益 %', nameTextStyle: { color: '#68737e' }, axisLabel: { color: '#68737e', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#20262e' } } },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8, borderColor: '#2b323b', backgroundColor: '#11151a' }],
  series: (factorResult.value?.summaries || []).map((row, index) => ({ name: factorResult.value.labels[row.factor], type: 'line', showSymbol: false, smooth: 0.15, lineStyle: { width: index === (factorResult.value.summaries.length - 1) && row.factor === 'composite' ? 3 : 1.8 }, data: row.long_short_series.map(point => [point.date, point.cumulative_pct]) })),
}))
const factorIcOption = computed(() => ({
  backgroundColor: 'transparent', color: factorColors,
  tooltip: { trigger: 'axis' }, legend: { type: 'scroll', top: 4, textStyle: { color: '#87919c' } },
    grid: { left: 64, right: 36, top: 62, bottom: 62 },
  xAxis: { type: 'time', axisLabel: { color: '#68737e' }, axisLine: { lineStyle: { color: '#2b323b' } } },
  yAxis: { type: 'value', name: 'Rank IC', nameTextStyle: { color: '#68737e' }, axisLabel: { color: '#68737e' }, splitLine: { lineStyle: { color: '#20262e' } } },
  dataZoom: [{ type: 'inside' }],
  series: (factorResult.value?.summaries || []).map(row => ({ name: factorResult.value.labels[row.factor], type: 'line', showSymbol: false, data: row.ic_series.map(point => [point.date, point.value]), markLine: { silent: true, symbol: 'none', lineStyle: { color: '#3a424c', type: 'dashed' }, data: [{ yAxis: 0 }] } })),
}))
const factorCorrelationOption = computed(() => {
  const correlation = factorResult.value?.correlation || { factors: [], matrix: [] }
  const names = correlation.factors.map(factor => factorResult.value.labels[factor])
  const data = correlation.matrix.flatMap((row, y) => row.map((value, x) => [x, y, value]))
  return {
    backgroundColor: 'transparent', tooltip: { formatter: params => `${names[params.value[1]]} × ${names[params.value[0]]}<br>秩相关：${params.value[2] ?? '—'}` },
    grid: { left: 145, right: 55, top: 35, bottom: 105 },
    xAxis: { type: 'category', data: names, axisLabel: { color: '#87919c', rotate: 20, interval: 0, fontSize: 11 }, splitArea: { show: true } },
    yAxis: { type: 'category', data: names, axisLabel: { color: '#87919c', interval: 0, fontSize: 11 }, splitArea: { show: true } },
    visualMap: { min: -1, max: 1, calculable: true, orient: 'horizontal', left: 'center', bottom: 5, inRange: { color: ['#ff6476', '#151a20', '#57e3ff'] }, textStyle: { color: '#87919c' } },
    series: [{ type: 'heatmap', data, label: { show: true, color: '#e7edf2', formatter: params => params.value[2] ?? '—' }, emphasis: { itemStyle: { shadowBlur: 12, shadowColor: 'rgba(87,227,255,.35)' } } }],
  }
})
const poolFactorWeightTotal = computed(() => Object.values(poolFactorWeights.value).reduce((sum, value) => sum + (Number(value) > 0 ? Number(value) : 0), 0))

onMounted(async () => {
  try {
    health.value = await (await fetch('/api/health')).json()
  } catch {
    health.value = { status: 'offline', mode: 'demo' }
  }
  try {
    factorCatalog.value = await (await fetch('/api/factors/catalog')).json()
  } catch {
    factorCatalog.value = []
  }
  await loadDatabaseStatus()
})
onUnmounted(() => { if (newsTimer) clearInterval(newsTimer); if (taskTimer) clearInterval(taskTimer); if (poolTimer) clearInterval(poolTimer) })
watch([autoRefresh, activeTab], ([minutes, tab]) => {
  if (newsTimer) clearInterval(newsTimer)
  newsTimer = minutes && tab === 'news' ? setInterval(loadNews, Number(minutes) * 60_000) : null
})
watch(activeTab, (tab) => {
  if (tab === 'records') loadRecords()
  if (tab === 'backtest') loadDatabaseStatus()
})

async function loadDatabaseStatus() {
  if (databaseStatusLoading.value) return
  databaseStatusLoading.value = true
  try {
    const response = await fetch('/api/database/market-status?market=A%E8%82%A1')
    if (response.ok) databaseStatus.value = await response.json()
  } finally {
    databaseStatusLoading.value = false
  }
}

async function checkDatabaseFreshness() {
  if (databaseFreshnessLoading.value) return
  databaseFreshnessLoading.value = true
  databaseFreshnessError.value = ''
  try {
    const response = await fetch('/api/database/market-status/check')
    const data = await response.json()
    if (!response.ok) throw new Error(apiErrorMessage(data.detail, '最新行情检测失败'))
    databaseFreshness.value = data
    await loadDatabaseStatus()
  } catch (requestError) {
    databaseFreshnessError.value = requestError.message
  } finally {
    databaseFreshnessLoading.value = false
  }
}

async function runFactorAnalysis() {
  if (!selectedFactors.value.length || factorLoading.value) return
  factorLoading.value = true
  factorError.value = ''
  factorResult.value = null
  try {
    const response = await fetch('/api/factors/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ factors: selectedFactors.value, start_date: factorStartDate.value, end_date: factorEndDate.value, forward_days: factorForwardDays.value, universe_limit: factorUniverseLimit.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '因子分析失败')
    factorResult.value = data
  } catch (requestError) {
    factorError.value = requestError.message
  } finally {
    factorLoading.value = false
  }
}

function useResearchFactorsInPool() {
  const available = selectedFactors.value.filter(factor => factorCatalog.value.some(item => item.id === factor))
  if (!available.length) return
  const equalWeight = Number((100 / available.length).toFixed(2))
  poolFactorWeights.value = Object.fromEntries(available.map(factor => [factor, equalWeight]))
  poolStrategy.value = '多因子月度轮动'
  activeTab.value = 'capital'
}

async function analyze() {
  if (!symbol.value.trim() || loading.value) return
  loading.value = true
  snapshot.value = null
  analysis.value = ''
  error.value = ''
  controller.value = new AbortController()
  try {
    await consumeSSE('/api/analyze/stream', {
      symbol: symbol.value,
      market: market.value,
      horizon: horizon.value,
      focus: focus.value,
      period: analysisPeriod.value,
    }, {
      snapshot: (data) => { snapshot.value = data },
      token: (data) => { analysis.value += data.content },
      error: (data) => { error.value = data.message },
    }, controller.value.signal)
  } catch (requestError) {
    if (requestError.name !== 'AbortError') error.value = requestError.message
  } finally {
    loading.value = false
    controller.value = null
  }
}

function format(value, suffix = '') {
  return value === null || value === undefined ? '—' : `${value}${suffix}`
}

function apiErrorMessage(detail, fallback) {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const field = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== 'body').join('.') : '请求参数'
      return `${field || '请求参数'}：${item?.msg || '格式不正确'}`
    }).join('；')
  }
  return fallback
}

function resetBacktestDates() {
  backtestStartDate.value = '2020-01-01'
  backtestEndDate.value = localDateValue()
}

async function runBacktest() {
  if (!symbol.value.trim() || backtestLoading.value) return
  backtestLoading.value = true
  backtestResult.value = null
  backtestError.value = ''
  try {
    const isCustom = backtestStrategy.value === 'AI自定义策略'
    const response = await fetch('/api/backtests/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: `${symbol.value}-${backtestStrategy.value}`, symbols: [symbol.value], market: market.value, strategies: isCustom ? [] : [backtestStrategy.value], strategy_code: isCustom ? customStrategyCode.value : null, initial_capital: initialCapital.value, fee_rate: feeRate.value, slippage_bps: slippageBps.value, start_date: backtestStartDate.value, end_date: backtestEndDate.value, adjustment_mode: adjustmentMode.value, fundamental_score_enabled: fundamentalScoreEnabled.value, fundamental_score_threshold: fundamentalScoreThreshold.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '回测失败')
    if (!data.results?.length) throw new Error(data.errors?.[0]?.message || '没有生成有效回测结果')
    backtestResult.value = data.results[0]
    await loadBacktestCandles()
  } catch (requestError) {
    backtestError.value = requestError.message
  } finally {
    backtestLoading.value = false
  }
}

async function loadBacktestCandles() {
  const params = new URLSearchParams({ symbol: symbol.value, market: market.value, start_date: backtestStartDate.value, end_date: backtestEndDate.value, adjustment_mode: adjustmentMode.value })
  const response = await fetch(`/api/market/candles?${params}`)
  if (response.ok) backtestCandles.value = await response.json()
}

async function loadMarketCandles() {
  marketLoading.value = true; marketError.value = ''; marketSnapshot.value = null
  try {
    const params = new URLSearchParams({ symbol: symbol.value, market: market.value, start_date: backtestStartDate.value, end_date: backtestEndDate.value })
    selectedIndicators.value.forEach((value) => params.append('indicators', value))
    const response = await fetch(`/api/market/candles?${params}`)
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || 'K线查询失败')
    marketSnapshot.value = data
  } catch (requestError) { marketError.value = requestError.message } finally { marketLoading.value = false }
}

async function generateStrategy() {
  strategyLoading.value = true; strategyExplanation.value = ''
  try {
    const response = await fetch('/api/strategies/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ description: strategyDescription.value }) })
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '生成失败')
    strategyCode.value = data.code; strategyExplanation.value = data.description
  } catch (requestError) { strategyExplanation.value = requestError.message } finally { strategyLoading.value = false }
}

async function explainStrategy() {
  strategyLoading.value = true
  try {
    const response = await fetch('/api/strategies/explain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: strategyCode.value }) })
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '解释失败')
    strategyExplanation.value = data.description
  } catch (requestError) { strategyExplanation.value = requestError.message } finally { strategyLoading.value = false }
}

function useGeneratedStrategy() {
  customStrategyCode.value = strategyCode.value; backtestStrategy.value = 'AI自定义策略'; activeTab.value = 'backtest'
}

async function startTask(kind) {
  taskLoading.value = true; taskState.value = null; scanSignals.value = []
  const endpoints = { sync: '/api/jobs/data-sync', fundamental: '/api/jobs/fundamental-sync', dividend: '/api/jobs/dividend-sync', backtest: '/api/jobs/full-market-backtest', scan: '/api/jobs/market-scan' }
  const payload = kind === 'sync'
    ? { start_date: backtestStartDate.value, end_date: backtestEndDate.value, concurrency: syncConcurrency.value, mode: syncMode.value, price_scope: syncPriceScope.value }
    : kind === 'fundamental' || kind === 'dividend'
      ? { concurrency: 2, mode: syncMode.value }
    : kind === 'backtest'
      ? { name: 'A股全市场回测', strategies: [backtestStrategy.value === 'AI自定义策略' ? 'MA120回踩5日不破' : backtestStrategy.value], start_date: backtestStartDate.value, end_date: backtestEndDate.value, initial_capital: initialCapital.value, fee_rate: feeRate.value, slippage_bps: slippageBps.value, adjustment_mode: adjustmentMode.value, fundamental_score_enabled: fundamentalScoreEnabled.value, fundamental_score_threshold: fundamentalScoreThreshold.value, market_signal_threshold: ['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(backtestStrategy.value) ? marketSignalThreshold.value : 0, volume_ratio_threshold: marketVolumeRatioEnabled.value ? 1.2 : 0, benchmark_5d_drop_threshold: benchmarkDropFilterEnabled.value ? benchmarkDropThresholdPct.value / 100 : 0 }
      : { strategies: [backtestStrategy.value === 'AI自定义策略' ? 'MA120回踩5日不破' : backtestStrategy.value], start_date: backtestStartDate.value, end_date: backtestEndDate.value }
  try {
    const response = await fetch(endpoints[kind], { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '任务创建失败')
    await pollTask(data.job_id)
  } catch (requestError) { taskState.value = { status: 'failed', error: requestError.message }; taskLoading.value = false }
}

async function pollTask(jobId) {
  if (taskTimer) clearInterval(taskTimer)
  const refresh = async () => {
    const response = await fetch(`/api/jobs/${jobId}`); taskState.value = await response.json()
    if (['completed', 'failed'].includes(taskState.value.status)) {
      clearInterval(taskTimer); taskTimer = null; taskLoading.value = false
      if (taskState.value.kind === 'market_scan') {
        scanSignals.value = await (await fetch(`/api/jobs/${jobId}/signals`)).json()
        trackedPositions.value = await (await fetch('/api/strategy-positions?status=open')).json()
      }
      if (taskState.value.kind === 'data_sync' && taskState.value.status === 'completed') await loadDatabaseStatus()
    }
  }
  await refresh()
  if (!['completed', 'failed'].includes(taskState.value?.status)) taskTimer = setInterval(refresh, 2000)
}

async function startCapitalPoolBacktest() {
  if (poolLoading.value) return
  poolLoading.value = true
  poolTaskState.value = null
  poolTradeSymbol.value = ''
  poolTradeCandles.value = null
  poolTradeChartError.value = ''
  poolFocusedTrade.value = null
  poolTradePage.value = 1
  try {
    const response = await fetch('/api/jobs/capital-pool-backtest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: `A股资金池${poolStrategy.value}回测`, strategy: poolStrategy.value,
        adjustment_mode: poolAdjustmentMode.value,
        candidate_ranking: poolCandidateRanking.value,
        start_date: backtestStartDate.value, end_date: backtestEndDate.value,
        initial_capital: poolInitialCapital.value, fee_rate: feeRate.value,
        exposure_limit: poolStrategy.value === '红利质量动量' ? 1 : poolExposureLimit.value / 100,
        single_position_limit: poolStrategy.value === '红利质量动量' ? 0.1 : poolSinglePositionLimit.value / 100,
        max_positions: poolStrategy.value === '红利质量动量' ? 10 : poolMaxPositions.value, volume_participation_limit: 1,
        minimum_turnover: poolMinimumTurnoverWan.value * 10000,
        max_opening_gap_pct: poolMaxOpeningGapPct.value,
        market_signal_rate_threshold: poolMarketSignalRateThresholdPct.value / 100,
        market_signal_threshold: ['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy.value) ? poolMarketSignalThreshold.value : 0,
        volume_ratio_threshold: poolVolumeRatioEnabled.value ? 1.2 : 0,
        benchmark_5d_drop_threshold: poolBenchmarkDropFilterEnabled.value ? poolBenchmarkDropThresholdPct.value / 100 : 0,
        fundamental_score_enabled: poolFundamentalScoreEnabled.value,
        fundamental_score_threshold: poolFundamentalScoreThreshold.value,
        factor_weights: Object.fromEntries(Object.entries(poolFactorWeights.value).filter(([, weight]) => Number(weight) > 0).map(([factor, weight]) => [factor, Number(weight)])),
        require_above_ma200: poolRequireAboveMa200.value,
        require_ma200_rising: poolRequireMa200Rising.value,
        base_slippage_bps: poolSlippageBps.value, impact_bps: 0,
      }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(apiErrorMessage(data.detail, '资金池回测创建失败'))
    await pollCapitalPoolTask(data.job_id)
  } catch (requestError) {
    poolTaskState.value = { status: 'failed', error: requestError.message }
    poolLoading.value = false
  }
}

async function pollCapitalPoolTask(jobId) {
  if (poolTimer) clearInterval(poolTimer)
  const refresh = async () => {
    const response = await fetch(`/api/jobs/${jobId}`)
    poolTaskState.value = await response.json()
    if (['completed', 'failed'].includes(poolTaskState.value.status)) {
      clearInterval(poolTimer); poolTimer = null; poolLoading.value = false
      const firstTrade = poolTaskState.value.result?.trade_events?.[0]
      if (poolTaskState.value.status === 'completed' && firstTrade) await loadPoolTradeChart(firstTrade.symbol, firstTrade)
    }
  }
  await refresh()
  if (!['completed', 'failed'].includes(poolTaskState.value?.status)) poolTimer = setInterval(refresh, 2000)
}

async function generateCapitalPoolPlan() {
  if (poolPlanLoading.value) return
  poolPlanLoading.value = true
  poolPlanState.value = null
  try {
    const response = await fetch('/api/jobs/capital-pool-plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strategy: poolStrategy.value, start_date: backtestStartDate.value, end_date: backtestEndDate.value,
        adjustment_mode: poolAdjustmentMode.value,
        max_positions: poolMaxPositions.value,
        market_signal_threshold: ['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy.value) ? poolMarketSignalThreshold.value : 0,
        max_opening_gap_pct: poolMaxOpeningGapPct.value,
      }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(apiErrorMessage(data.detail, '交易计划创建失败'))
    if (poolPlanTimer) clearInterval(poolPlanTimer)
    const refresh = async () => {
      poolPlanState.value = await (await fetch(`/api/jobs/${data.job_id}`)).json()
      if (['completed', 'failed'].includes(poolPlanState.value.status)) {
        clearInterval(poolPlanTimer); poolPlanTimer = null; poolPlanLoading.value = false
      }
    }
    await refresh()
    if (!['completed', 'failed'].includes(poolPlanState.value?.status)) poolPlanTimer = setInterval(refresh, 2000)
  } catch (requestError) {
    poolPlanState.value = { status: 'failed', error: requestError.message }
    poolPlanLoading.value = false
  }
}

async function loadPoolTradeChart(symbol = poolTradeSymbol.value, focusTrade = null) {
  if (!symbol) return
  poolTradeSymbol.value = symbol
  poolFocusedTrade.value = focusTrade?.symbol === symbol ? focusTrade : null
  poolTradeChartLoading.value = true
  poolTradeChartError.value = ''
  try {
    const params = new URLSearchParams({
      symbol, market: 'A股', start_date: backtestStartDate.value, end_date: backtestEndDate.value,
      indicators: 'MA', adjustment_mode: '动态前复权',
    })
    const response = await fetch(`/api/market/candles?${params}`)
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '成交K线加载失败')
    poolTradeCandles.value = data
  } catch (requestError) {
    poolTradeCandles.value = null
    poolTradeChartError.value = requestError.message
  } finally {
    poolTradeChartLoading.value = false
  }
}

async function selectPoolTrade(event) {
  if (!event?.symbol) return
  if (event.symbol !== poolTradeSymbol.value) await loadPoolTradeChart(event.symbol, event)
  else poolFocusedTrade.value = event
}

async function loadRecords() {
  recordsLoading.value = true
  try { records.value = await (await fetch('/api/backtests/records')).json() } finally { recordsLoading.value = false }
}

function recordAverageReturn(record) {
  const values = (record.summary?.strategies || []).map((item) => Number(item.average_return_pct)).filter(Number.isFinite)
  return values.length ? Number((values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(2)) : null
}

async function deleteRecord(record) {
  if (!window.confirm(`确认删除回测记录“${record.name}”？删除后将从列表隐藏。`)) return
  const response = await fetch(`/api/backtests/records/${record.id}`, { method: 'DELETE' })
  const data = await response.json()
  if (!response.ok) { comparisonError.value = data.detail || '删除失败'; return }
  compareRunIds.value = compareRunIds.value.filter((id) => id !== record.id)
  if (selectedRecordId.value === record.id) { selectedRecordId.value = ''; recordDetail.value = null; selectedRecordSummary.value = null }
  comparisonResult.value = null
  await loadRecords()
}

async function compareRecords() {
  if (compareRunIds.value.length < 2) { comparisonError.value = '请至少勾选两条回测记录'; return }
  comparisonLoading.value = true; comparisonError.value = ''; comparisonResult.value = null
  try {
    const response = await fetch('/api/backtests/compare', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ run_ids: compareRunIds.value }) })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || 'PK 对比失败')
    comparisonResult.value = data
  } catch (requestError) { comparisonError.value = requestError.message } finally { comparisonLoading.value = false }
}

async function openRecord(id) {
  selectedRecordId.value = id
  recordQuery.value = ''; recordStrategy.value = ''; recordOffset.value = 0
  const summaryResponse = await fetch(`/api/backtests/records/${id}/summary`)
  selectedRecordSummary.value = summaryResponse.ok ? await summaryResponse.json() : null
  await loadRecordResults()
}

async function loadRecordResults() {
  if (!selectedRecordId.value) return
  recordsLoading.value = true
  const params = new URLSearchParams({ limit: String(recordLimit), offset: String(recordOffset.value), sort_by: recordSortBy.value, sort_order: recordSortOrder.value })
  if (recordQuery.value.trim()) params.set('query', recordQuery.value.trim())
  if (recordStrategy.value) params.set('strategy', recordStrategy.value)
  try {
    const response = await fetch(`/api/backtests/records/${selectedRecordId.value}/results?${params}`)
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '结果查询失败')
    recordDetail.value = data
  } finally { recordsLoading.value = false }
}

async function searchRecordResults() {
  recordOffset.value = 0
  await loadRecordResults()
}

async function changeRecordPage(direction) {
  recordOffset.value = Math.max(0, recordOffset.value + direction * recordLimit)
  await loadRecordResults()
}

async function openRecordResult(item) {
  const data = await (await fetch(`/api/backtests/records/${selectedRecordId.value}/results/${item.result_id}`)).json()
  const record = records.value.find((value) => value.id === selectedRecordId.value)
  if (data.symbol === 'CAPITAL_POOL') {
    poolTradePage.value = 1
    if (record) {
      backtestStartDate.value = record.start_date
      backtestEndDate.value = record.end_date
      poolStrategy.value = record.parameters?.strategy || String(data.strategy || '').replace(/^资金池·/, '') || poolStrategy.value
      poolAdjustmentMode.value = record.parameters?.adjustment_mode || '动态前复权'
      poolInitialCapital.value = Number(record.parameters?.initial_capital ?? data.initial_capital ?? poolInitialCapital.value)
      poolExposureLimit.value = Number(record.parameters?.exposure_limit ?? 1) * 100
      poolSinglePositionLimit.value = Number(record.parameters?.single_position_limit ?? 0.04) * 100
      poolMaxPositions.value = Number(record.parameters?.max_positions ?? 25)
      poolSlippageBps.value = Number(record.parameters?.base_slippage_bps ?? 5)
      if (record.parameters?.factor_weights) poolFactorWeights.value = { ...record.parameters.factor_weights }
      poolCandidateRanking.value = record.parameters?.candidate_ranking || 'rsi_rebound_score'
    }
    poolTaskState.value = { status: 'completed', message: `历史回测记录 · ${record?.name || data.strategy}`, progress_current: 1, progress_total: 1, result: data }
    activeTab.value = 'capital'
    const firstTrade = data.trade_events?.[0]
    if (firstTrade) await loadPoolTradeChart(firstTrade.symbol, firstTrade)
    return
  }
  backtestResult.value = data; symbol.value = data.symbol
  if (record) {
    backtestStartDate.value = record.start_date; backtestEndDate.value = record.end_date
    fundamentalScoreEnabled.value = Boolean(record.parameters?.fundamental_score_enabled)
    fundamentalScoreThreshold.value = Number(record.parameters?.fundamental_score_threshold ?? 60)
    marketSignalThreshold.value = Number(record.parameters?.market_signal_threshold ?? 0)
    slippageBps.value = Number(record.parameters?.slippage_bps ?? data.slippage_bps ?? 0)
    adjustmentMode.value = record.parameters?.adjustment_mode || data.adjustment || '前复权'
  }
  activeTab.value = 'backtest'; await loadBacktestCandles()
}

async function openLatestMarketRun() {
  const runId = taskState.value?.result?.run_id
  if (!runId) return
  activeTab.value = 'records'
  await loadRecords()
  await openRecord(runId)
}

async function runBatchBacktest() {
  if (batchLoading.value) return
  const symbols = [...new Set(batchSymbols.value.split(/[\s,，]+/).map((value) => value.trim()).filter(Boolean))]
  if (!symbols.length) {
    batchError.value = '请至少输入一个股票代码'
    return
  }
  batchLoading.value = true
  batchResult.value = null
  batchError.value = ''
  try {
    const strategies = ['买入持有','均线交叉','RSI反转','RSI反转+止盈止损','RSI17反转+止盈止损','RSI反转+暴跌强化风控','多周期趋势跟随','KDJ急跌首阳T+1','建仓波J<0动态止盈','MA20/60首次回踩','MA20/55金叉后J<13且涨幅<15%','MA120回踩5日不破','MA200回踩5日不破']
    const response = await fetch('/api/backtests/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: '多标的多策略对比', symbols, strategies, market: market.value, initial_capital: initialCapital.value, fee_rate: feeRate.value, slippage_bps: slippageBps.value, start_date: backtestStartDate.value, end_date: backtestEndDate.value, adjustment_mode: adjustmentMode.value, fundamental_score_enabled: fundamentalScoreEnabled.value, fundamental_score_threshold: fundamentalScoreThreshold.value }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '批量回测失败')
    batchResult.value = { requested_symbols: symbols.length, succeeded_symbols: symbols.length - data.summary.failed_symbols, failed_symbols: data.summary.failed_symbols, period: `${backtestStartDate.value} ~ ${backtestEndDate.value}`, summaries: data.summary.strategies, results: data.results, errors: data.errors }
  } catch (requestError) {
    batchError.value = requestError.message
  } finally {
    batchLoading.value = false
  }
}


function formatDate(timestamp) {
  return new Date(Number(timestamp) * 1000).toLocaleDateString('zh-CN')
}

function formatMoney(value, digits = 2) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—'
}

function formatDatabaseUpdate(value) {
  if (!value) return '尚无同步记录'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

function periodLabel(value) {
  return { '3mo': '3 个月', '6mo': '6 个月', '1y': '1 年', '2y': '2 年', '5y': '5 年' }[value] || value
}

async function loadNews() {
  if (!newsQuery.value.trim() || newsLoading.value) return
  newsLoading.value = true
  newsItems.value = []
  newsAnalysis.value = ''
  newsError.value = ''
  try {
    await consumeSSE('/api/news/stream', { query: newsQuery.value, lookback: newsLookback.value, limit: 16, focus: newsFocus.value }, {
      news: (data) => { newsItems.value = data; lastNewsUpdate.value = new Date() },
      token: (data) => { newsAnalysis.value += data.content },
      error: (data) => { newsError.value = data.message },
    })
  } catch (requestError) {
    newsError.value = requestError.message
  } finally {
    newsLoading.value = false
  }
}

function formatNewsTime(value) {
  if (!value) return '时间未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '时间未知'
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="shell">
    <header>
      <a class="brand" href="#">STOCK<span>/</span>AGENT</a>
      <nav class="tabs">
        <button :class="{ active: activeTab === 'analysis' }" @click="activeTab = 'analysis'">股票分析</button>
        <button :class="{ active: activeTab === 'market' }" @click="activeTab = 'market'">K线指标</button>
        <button :class="{ active: activeTab === 'backtest' }" @click="activeTab = 'backtest'">量化回测</button>
        <button :class="{ active: activeTab === 'factors' }" @click="activeTab = 'factors'">因子研究</button>
        <button :class="{ active: activeTab === 'capital' }" @click="activeTab = 'capital'">资金池回测</button>
        <button :class="{ active: activeTab === 'strategy' }" @click="activeTab = 'strategy'">AI策略</button>
        <button :class="{ active: activeTab === 'radar' }" @click="activeTab = 'radar'">策略雷达</button>
        <button :class="{ active: activeTab === 'records' }" @click="activeTab = 'records'">回测记录</button>
        <button :class="{ active: activeTab === 'news' }" @click="activeTab = 'news'">新闻推送</button>
      </nav>
      <div class="status" :class="health.status"><i></i>{{ health.status === 'ok' ? (health.mode === 'agno' ? health.model : 'LOCAL DEMO') : 'OFFLINE' }}</div>
    </header>

    <main>
      <template v-if="activeTab === 'analysis'">
      <section class="hero">
        <p>RESEARCH BEFORE REACTION</p>
        <h1>把股票代码，变成一份<br><em>可验证的研究框架。</em></h1>
        <small>覆盖 A 股、港股与美股 · 行情可能延迟 · 不构成投资建议</small>
      </section>

      <section class="workspace">
        <form class="query-card" @submit.prevent="analyze">
          <div class="section-title"><span>01</span><div><small>QUERY</small><h2>研究标的</h2></div></div>
          <label>股票代码<input v-model="symbol" placeholder="600519 / 00700 / AAPL" /></label>
          <div class="two-col">
            <label>市场<select v-model="market"><option>自动</option><option>A股</option><option>港股</option><option>美股</option></select></label>
            <label>投资期限<select v-model="horizon"><option>短线</option><option>波段</option><option>中长线</option></select></label>
          </div>
          <label>行情范围<select v-model="analysisPeriod"><option value="3mo">3 个月</option><option value="6mo">6 个月</option><option value="1y">1 年</option><option value="2y">2 年</option><option value="5y">5 年</option></select></label>
          <label>关注重点<textarea v-model="focus" maxlength="500" placeholder="例如：趋势、回撤风险、关键价位"></textarea></label>
          <button :disabled="loading || !symbol.trim()"><span v-if="loading" class="loader"></span><template v-else>获取行情并分析 ↗</template></button>
          <button v-if="loading" type="button" class="cancel" @click="controller?.abort()">停止</button>
        </form>

        <div class="report-card">
          <div class="section-title"><span>02</span><div><small>AGENT REPORT</small><h2>研究报告</h2></div></div>
          <div v-if="analysis" class="markdown" v-html="renderedAnalysis"></div>
          <div v-else-if="loading" class="thinking"><i></i><p>正在读取行情并构建三种情景……</p></div>
          <div v-else class="empty"><b>↗</b><p>输入股票代码后生成流式研究报告</p></div>
          <p v-if="error" class="error">{{ error }}</p>
        </div>
      </section>

      <section v-if="snapshot" class="dashboard">
        <div class="dashboard-head">
          <div><small>03 / MARKET SNAPSHOT</small><h2>{{ snapshot.name }} <span>{{ snapshot.symbol }}</span></h2></div>
          <div class="price" :class="{ up: positive, down: !positive }"><b>{{ snapshot.price }}</b><span>{{ positive ? '+' : '' }}{{ snapshot.change_pct }}%</span><small>{{ snapshot.currency }}</small></div>
        </div>
        <div class="chart candle-chart" @mouseleave="chartHover = null">
          <svg viewBox="0 0 1000 240" preserveAspectRatio="none">
            <g v-for="(candle, index) in candleBars" :key="`${candle.row.timestamp}-${index}`" class="candle" :class="{ up: candle.up, down: !candle.up }" @mouseenter="chartHover = candle">
              <line :x1="candle.x" :x2="candle.x" :y1="candle.yHigh" :y2="candle.yLow" />
              <rect :x="candle.x - candle.width / 2" :y="candle.bodyY" :width="candle.width" :height="candle.bodyHeight" />
            </g>
          </svg>
          <div v-if="chartHover" class="candle-tooltip" :style="{ left: `${Math.min(88, Math.max(12, chartHover.x / 10))}%` }">
            <b>{{ formatDate(chartHover.row.timestamp) }}</b>
            <span>开 {{ chartHover.row.open }}</span><span>高 {{ chartHover.row.high }}</span><span>低 {{ chartHover.row.low }}</span><span>收 {{ chartHover.row.close }}</span>
          </div>
          <span>范围：{{ snapshot.data_note.split('日线')[0] }} · 鼠标移入蜡烛查看价格</span>
        </div>
        <div class="metrics">
          <article><small>区间收益</small><b>{{ format(snapshot.period_return_pct, '%') }}</b></article>
          <article><small>MA 5 / 20 / 60</small><b>{{ snapshot.ma5 }} / {{ snapshot.ma20 }} / {{ snapshot.ma60 }}</b></article>
          <article><small>RSI (14)</small><b>{{ format(snapshot.rsi14) }}</b></article>
          <article><small>年化波动率</small><b>{{ format(snapshot.annualized_volatility_pct, '%') }}</b></article>
          <article><small>最大回撤</small><b>{{ format(snapshot.max_drawdown_pct, '%') }}</b></article>
          <article><small>区间高 / 低</small><b>{{ snapshot.period_high }} / {{ snapshot.period_low }}</b></article>
        </div>
        <p class="source">数据源：{{ snapshot.data_source }}。{{ snapshot.data_note }}</p>
      </section>
      </template>

      <template v-else-if="activeTab === 'market'">
        <section class="hero compact-hero"><p>MARKET DATA WORKBENCH</p><h1>查询 K 线，也查询<br><em>指标背后的每一个数值。</em></h1><small>任意日期范围 · 日线前复权 · 本地数据库优先</small></section>
        <section class="workbench-panel">
          <form class="query-card market-form" @submit.prevent="loadMarketCandles">
            <div class="section-title"><span>01</span><div><small>QUERY</small><h2>K线查询</h2></div></div>
            <label>股票代码<input v-model="symbol" placeholder="600519" /></label>
            <div class="two-col"><label>开始日期<input v-model="backtestStartDate" type="date" /></label><label>结束日期<input v-model="backtestEndDate" type="date" /></label></div>
            <label>指标</label>
            <div class="indicator-picker"><label v-for="item in ['MA','MACD','RSI','KDJ','ATR','九转','BOLL']" :key="item"><input v-model="selectedIndicators" type="checkbox" :value="item" />{{ item }}</label></div>
            <button :disabled="marketLoading"><span v-if="marketLoading" class="loader"></span><template v-else>查询行情 ↗</template></button>
            <p v-if="marketError" class="error">{{ marketError }}</p>
          </form>
          <div class="workbench-output">
            <template v-if="marketSnapshot">
              <div class="dashboard-head"><div><small>02 / CANDLES</small><h2>{{ marketSnapshot.name }} <span>{{ marketSnapshot.symbol }}</span></h2></div><small>{{ marketSnapshot.points.length }} 个交易日</small></div>
              <div class="chart candle-chart">
                <svg viewBox="0 0 1000 240" preserveAspectRatio="none">
                  <g v-for="(candle,index) in marketCandleChart.bars" :key="index" class="candle" :class="{ up:candle.up, down:!candle.up }"><line :x1="candle.x" :x2="candle.x" :y1="candle.yHigh" :y2="candle.yLow"/><rect :x="candle.x-candle.width/2" :y="candle.bodyY" :width="candle.width" :height="candle.bodyHeight"/></g>
                  <polyline v-if="marketCandleChart.lines.ma20" class="ma-line ma20" :points="marketCandleChart.lines.ma20"/><polyline v-if="marketCandleChart.lines.ma60" class="ma-line ma60" :points="marketCandleChart.lines.ma60"/><polyline v-if="marketCandleChart.lines.ma120" class="ma-line ma120" :points="marketCandleChart.lines.ma120"/><polyline v-if="marketCandleChart.lines.ma200" class="ma-line ma200" :points="marketCandleChart.lines.ma200"/>
                </svg><span>MA20 / MA60 / MA120 / MA200</span>
              </div>
              <div class="metrics indicator-metrics" v-if="marketSnapshot.points.length"><article v-for="(value,key) in marketSnapshot.points.at(-1)" :key="key" v-show="!['timestamp','open','high','low','close','volume'].includes(key)"><small>{{ key }}</small><b>{{ format(value) }}</b></article></div>
              <p class="source">{{ marketSnapshot.data_source }} · {{ marketSnapshot.adjustment }}。{{ marketSnapshot.indicator_note }}</p>
            </template>
            <div v-else-if="marketLoading" class="thinking"><i></i><p>正在读取数据库并补齐行情……</p></div><div v-else class="empty"><b>⌁</b><p>选择日期和指标后查询</p></div>
          </div>
        </section>
      </template>

      <template v-else-if="activeTab === 'backtest'">
        <section class="hero compact-hero">
          <p>BACKTEST WORKBENCH</p><h1>既能验证一只股票，<br><em>也能一次回测全部 A 股。</em></h1><small>个股即时回测 · 全市场异步任务 · K线 B/S · 资产曲线 · 结果查询</small>
        </section>
        <section class="database-status-card">
          <div class="database-status-title"><span>DB</span><div><small>LOCAL MARKET DATABASE</small><h2>本地行情数据库</h2></div></div>
          <template v-if="databaseStatus">
            <article class="latest-date"><small>全市场最新可用日</small><b>{{ databaseStatus.latest_trade_date || '暂无数据' }}</b><span>局部最新：{{ databaseStatus.raw_latest_trade_date || '暂无数据' }}（{{ databaseStatus.raw_latest_symbols || 0 }}只）</span></article>
            <article><small>最新日覆盖</small><b>{{ databaseStatus.symbols_on_latest_date }} / {{ databaseStatus.universe_symbols }}</b><span>{{ databaseStatus.coverage_pct }}% · 停牌股票可能没有当日K线</span></article>
            <article><small>完整复权覆盖</small><b>{{ databaseStatus.full_adjustment_symbols }} / {{ databaseStatus.universe_symbols }}</b><span>{{ databaseStatus.full_adjustment_coverage_pct }}% · 动态/后复权需要</span></article>
            <article><small>最近写入时间</small><b class="database-update-time">{{ formatDatabaseUpdate(databaseStatus.last_updated_at) }}</b><span>数据库实际落盘时间</span></article>
          </template>
          <div v-else class="database-status-empty">{{ databaseStatusLoading ? '正在读取数据库状态……' : '数据库状态不可用' }}</div>
          <div class="database-status-actions"><button :disabled="databaseStatusLoading" title="只刷新本地统计" @click="loadDatabaseStatus">{{ databaseStatusLoading ? '刷新中' : '刷新' }}</button><button :disabled="databaseFreshnessLoading" title="联网探测AKShare最新交易日并检查本地覆盖" @click="checkDatabaseFreshness">{{ databaseFreshnessLoading ? '检测中' : '检测最新' }}</button></div>
        </section>
        <section v-if="databaseFreshness || databaseFreshnessError" class="database-freshness-result">
          <p v-if="databaseFreshnessError" class="error">{{ databaseFreshnessError }}</p>
          <template v-if="databaseFreshness">
            <article v-if="databaseFreshness.front"><span :class="databaseFreshness.front.front_current ? 'fresh-ok' : 'fresh-missing'">{{ databaseFreshness.front.front_current ? '已是最新' : '需要同步' }}</span><div><small>仅前复权行情</small><b>{{ databaseFreshness.front.source_latest_trade_date }}</b><em>{{ databaseFreshness.front.front_symbols }} / {{ databaseFreshness.front.universe_symbols }}（{{ databaseFreshness.front.front_coverage_pct }}%）</em></div></article>
            <article v-if="databaseFreshness.full"><span :class="databaseFreshness.full.full_current ? 'fresh-ok' : 'fresh-missing'">{{ databaseFreshness.full.full_current ? '已是最新' : '需要同步' }}</span><div><small>完整行情数据</small><b>{{ databaseFreshness.full.source_latest_trade_date }}</b><em>{{ databaseFreshness.full.full_symbols }} / {{ databaseFreshness.full.universe_symbols }}（{{ databaseFreshness.full.full_coverage_pct }}%）</em></div></article>
            <p v-if="Object.keys(databaseFreshness.errors || {}).length" class="error">部分行情探测失败：{{ JSON.stringify(databaseFreshness.errors) }}</p>
          </template>
        </section>
        <section class="full-market-backtest">
          <div class="market-task-controls">
            <div class="section-title"><span>01</span><div><small>SCOPE</small><h2>选择回测范围</h2></div></div>
            <label>回测模式<select v-model="backtestScope"><option value="single">个股回测</option><option value="market">全市场回测</option></select></label>
            <template v-if="backtestScope === 'market'">
              <div class="section-title task-strategy-title"><span>02</span><div><small>DATE RANGE &amp; DATA</small><h2>选择回测日期并同步数据</h2></div></div>
              <div class="two-col"><label>回测开始日期<input v-model="backtestStartDate" type="date" :max="backtestEndDate || todayDate" /></label><label>回测结束日期<input v-model="backtestEndDate" type="date" :min="backtestStartDate" :max="todayDate" /></label></div>
              <button class="secondary-button wide-button" type="button" @click="resetBacktestDates">恢复默认：2020-01-01 至今天</button>
              <label>同步模式<select v-model="syncMode"><option value="incremental">增量同步（日常推荐）</option><option value="full">全量同步（首次建库/修复）</option></select></label>
              <label>价格数据<select v-model="syncPriceScope"><option value="front_only">仅前复权（稳定、推荐）</option><option value="all">完整复权（前/不/后复权）</option></select></label>
              <label>同步并发<select v-model.number="syncConcurrency"><option :value="1">1（最稳）</option><option :value="2">2（稳定）</option><option :value="3">3（较快）</option><option :value="4">4（推荐）</option></select></label>
              <button class="action-button wide-button" :disabled="taskLoading" @click="startTask('sync')">{{ syncMode === 'incremental' ? '同步所选日期范围行情' : '重拉所选日期范围行情' }} ↗</button>
              <button class="secondary-button wide-button" :disabled="taskLoading" @click="startTask('fundamental')">{{ syncMode === 'incremental' ? '同步最新基本面数据' : '全量同步历史财报' }} ↗</button>
              <p class="form-note">行情统一通过 AKShare 获取。仅前复权每只股票只请求一套行情，适合前复权全市场回测；资金池动态前复权、后复权回测仍需选择“完整复权”。同步支持断点续传；上游繁忙时请把并发降为1。</p>
            </template>
            <template v-else>
              <label>股票代码<input v-model="symbol" placeholder="600519 / 00700 / AAPL" /></label>
              <label>市场<select v-model="market"><option>自动</option><option>A股</option><option>港股</option><option>美股</option></select></label>
              <div class="two-col"><label>回测开始日期<input v-model="backtestStartDate" type="date" :max="backtestEndDate || todayDate" /></label><label>回测结束日期<input v-model="backtestEndDate" type="date" :min="backtestStartDate" :max="todayDate" /></label></div>
              <button class="secondary-button wide-button" type="button" @click="resetBacktestDates">恢复默认：2020-01-01 至今天</button>
            </template>
            <div class="section-title task-strategy-title"><span>03</span><div><small>STRATEGY</small><h2>{{ backtestScope === 'market' ? '全市场回测设置' : '个股回测设置' }}</h2></div></div>
            <label>策略<select v-model="backtestStrategy"><option>买入持有</option><option>均线交叉</option><option>RSI反转</option><option>RSI反转+止盈止损</option><option>多周期趋势跟随</option><option>KDJ急跌首阳T+1</option><option>建仓波J&lt;0动态止盈</option><option>MA20/60首次回踩</option><option>MA20/55金叉后J&lt;13且涨幅&lt;15%</option><option>MA120回踩5日不破</option><option>MA200回踩5日不破</option><option v-if="backtestScope === 'single'">AI自定义策略</option></select></label>
            <label>价格复权<select v-model="adjustmentMode"><option>前复权</option><option>动态前复权</option><option>后复权</option><option>不复权</option></select></label>
            <label v-if="backtestScope === 'market' && ['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(backtestStrategy)">全市场信号开仓门槛（只统计对应RSI周期）<input v-model.number="marketSignalThreshold" type="number" min="0" max="6000" step="1" /></label>
            <p v-if="backtestScope === 'market' && ['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(backtestStrategy)" class="form-note">每天独立扫描所有股票的对应 RSI 原始入场条件（RSI17策略使用RSI17）；达到该数量，个股才允许在下一交易日开盘买入。填写0关闭。</p>
            <label v-if="backtestScope === 'market'" class="toggle-control"><input v-model="marketVolumeRatioEnabled" type="checkbox" /><span>启用量能比过滤（≥1.2）</span></label>
            <label v-if="backtestScope === 'market' && ['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(backtestStrategy)" class="toggle-control"><input v-model="benchmarkDropFilterEnabled" type="checkbox" /><span>启用沪深300压力过滤</span></label>
            <label v-if="backtestScope === 'market' && benchmarkDropFilterEnabled">沪深300近5日最低跌幅（%）<input v-model.number="benchmarkDropThresholdPct" type="number" min="0.1" max="50" step="0.5" /></label>
            <p v-if="backtestScope === 'market' && marketVolumeRatioEnabled" class="form-note">信号日最近5个交易日平均成交量 ÷ 此前30个交易日平均成交量必须达到1.2；仅使用当时已知数据。</p>
            <p class="form-note">动态前复权使用 AKShare 后复权累计序列并按回测末日归一化；后复权和动态前复权需要先完成全量同步。</p>
            <label v-if="backtestScope === 'single' && backtestStrategy === 'AI自定义策略'">策略 DSL<textarea v-model="customStrategyCode"></textarea></label>
            <div class="two-col"><label>初始资金<input v-model.number="initialCapital" type="number" min="1000" step="1000" /></label><label>单边手续费<input v-model.number="feeRate" type="number" min="0" max="0.02" step="0.0001" /></label></div>
            <label>单边滑点（bps）<input v-model.number="slippageBps" type="number" min="0" max="100" step="1" /></label>
            <p class="form-note">默认5bps：买入成交价上浮0.05%，卖出成交价下调0.05%；沪深300基准曲线不扣策略滑点。</p>
            <div class="fundamental-filter-control">
              <label class="toggle-control"><input v-model="fundamentalScoreEnabled" type="checkbox" /><span>启用基本面评分</span></label>
              <label v-if="fundamentalScoreEnabled">最低评分<input v-model.number="fundamentalScoreThreshold" type="number" min="0" max="100" step="5" /></label>
              <p>独立于策略的入场过滤层；关闭后保持原策略。按财报公告/更新时间取当时可见评分，默认要求达到60分。</p>
            </div>
            <button v-if="backtestScope === 'market'" class="action-button wide-button" :disabled="taskLoading" @click="startTask('backtest')">一次性回测全市场 ↗</button>
            <button v-else class="action-button wide-button" :disabled="backtestLoading || !symbol.trim()" @click="runBacktest">运行个股回测 ↗</button>
          </div>
          <div class="market-task-output">
            <div class="section-title"><span>04</span><div><small>PROGRESS</small><h2>{{ backtestScope === 'market' ? '同步 / 全市场回测任务' : '个股回测结果' }}</h2></div></div>
            <template v-if="backtestScope === 'market'"><template v-if="taskState"><div class="job-card"><b :class="taskState.status">{{ taskState.status }}</b><span>{{ taskState.message }}</span><progress :max="taskState.progress_total || 1" :value="taskState.progress_current || 0"></progress><small>{{ taskState.progress_current }} / {{ taskState.progress_total }}</small><p v-if="taskState.error" class="error">{{ taskState.error }}</p></div><div v-if="taskState.result?.strategies?.length" class="market-summary"><p class="form-note">区间股票 {{ taskState.result.universe_symbols ?? taskState.result.symbols }} 只 · 实际回测 {{ taskState.result.succeeded }} 只 · 复权字段缺口排除 {{ taskState.result.excluded_adjustment || 0 }} 只 · 其他失败 {{ taskState.result.failed }} 只</p><div class="batch-table-wrap"><table class="batch-table"><thead><tr><th>策略</th><th>样本</th><th>平均收益</th><th>平均年化</th><th>组合收益</th><th>组合回撤</th><th>组合夏普</th><th>个股夏普均值</th><th>平均胜率</th><th>盈利比例</th><th>跑赢比例</th></tr></thead><tbody><tr v-for="item in taskState.result.strategies" :key="item.strategy"><td>{{ item.strategy }}</td><td>{{ item.samples }}</td><td :class="item.average_return_pct>=0?'positive-text':'negative-text'">{{ item.average_return_pct }}%</td><td :class="item.average_annualized_return_pct>=0?'positive-text':'negative-text'">{{ format(item.average_annualized_return_pct,'%') }}</td><td :class="item.portfolio_return_pct>=0?'positive-text':'negative-text'">{{ format(item.portfolio_return_pct,'%') }}</td><td>{{ format(item.portfolio_max_drawdown_pct,'%') }}</td><td>{{ format(item.portfolio_sharpe) }}</td><td>{{ format(item.average_sharpe) }}</td><td>{{ format(item.average_win_rate_pct,'%') }}</td><td>{{ item.profitable_rate_pct }}%</td><td>{{ item.outperform_rate_pct }}%</td></tr></tbody></table></div><p class="form-note">组合指标：所有有效股票初始等权，未上市区间按现金处理，按组合每日净值计算夏普和最大回撤。</p><button class="secondary-button wide-button" @click="openLatestMarketRun">查询个股结果与 B/S 图</button></div></template><div v-else class="empty"><b>▦</b><p>首次使用请全量同步，之后使用增量同步</p></div></template>
            <template v-else><div v-if="backtestLoading" class="thinking"><i></i><p>正在运行并保存个股回测……</p></div><div v-else-if="backtestResult" class="single-result-summary"><b>{{ backtestResult.name }} · {{ backtestResult.total_return_pct }}%</b><p>完整 K 线 B/S、资产曲线和成交明细显示在下方。</p></div><div v-else class="empty"><b>⌁</b><p>输入股票代码并运行回测</p></div><p v-if="backtestError" class="error">{{ backtestError }}</p></template>
            <div v-if="backtestScope === 'market' && marketCurveItems.length" class="summary-curves"><div v-for="item in marketCurveItems" :key="`market-${item.strategy}`" class="summary-curve"><b>{{ item.strategy }}：全市场等权组合 vs 沪深300</b><div class="backtest-chart chart"><svg viewBox="0 0 1000 240" preserveAspectRatio="none"><polyline class="equity-line" :points="item.points.equity" /><polyline class="benchmark-line" :points="item.points.benchmark" /></svg><span>青色：策略组合 · 灰色：沪深300 · 初始净值100</span></div></div></div>
          </div>
        </section>

        <section v-if="backtestResult" class="backtest-layout record-drilldown">
          <form v-if="false" class="query-card backtest-form" @submit.prevent="runBacktest">
            <div class="section-title"><span>01</span><div><small>STRATEGY</small><h2>回测设置</h2></div></div>
            <label>股票代码<input v-model="symbol" placeholder="600519 / 00700 / AAPL" /></label>
            <label>市场<select v-model="market"><option>自动</option><option>A股</option><option>港股</option><option>美股</option></select></label>
            <div class="two-col"><label>开始日期<input v-model="backtestStartDate" type="date" /></label><label>结束日期<input v-model="backtestEndDate" type="date" /></label></div>
            <label>策略<select v-model="backtestStrategy"><option>买入持有</option><option>均线交叉</option><option>RSI反转</option><option>RSI反转+止盈止损</option><option>多周期趋势跟随</option><option>KDJ急跌首阳T+1</option><option>建仓波J&lt;0动态止盈</option><option>MA20/60首次回踩</option><option>MA20/55金叉后J&lt;13且涨幅&lt;15%</option><option>MA120回踩5日不破</option><option>MA200回踩5日不破</option><option>AI自定义策略</option></select></label>
            <label v-if="backtestStrategy === 'AI自定义策略'">策略 DSL<textarea v-model="customStrategyCode" placeholder="请先在 AI 策略页面生成，或直接粘贴 DSL"></textarea></label>
            <label>初始资金<input v-model.number="initialCapital" type="number" min="1000" step="1000" /></label>
            <label>单边手续费<input v-model.number="feeRate" type="number" min="0" max="0.02" step="0.0001" /></label>
            <button :disabled="backtestLoading || !symbol.trim()"><span v-if="backtestLoading" class="loader"></span><template v-else>运行回测 ↗</template></button>
          </form>
          <div class="backtest-result">
            <div v-if="backtestResult">
              <div class="dashboard-head"><div><small>02 / RESULT</small><h2>{{ backtestResult.name }} <span>{{ backtestResult.symbol }} · {{ backtestResult.strategy }}</span></h2></div><div class="price" :class="{ up: backtestResult.total_return_pct >= 0, down: backtestResult.total_return_pct < 0 }"><b>{{ backtestResult.total_return_pct }}%</b><small>策略收益</small></div></div>
              <div class="backtest-chart chart" @mouseleave="backtestHover = null">
                <svg viewBox="0 0 1000 240" preserveAspectRatio="none">
                  <polyline class="equity-line" :points="backtestPoints.equity" /><polyline class="benchmark-line" :points="backtestPoints.benchmark" />
                  <g v-for="event in backtestPoints.markers" :key="`${event.timestamp}-${event.side}`" class="trade-marker" :class="event.side === 'B' ? 'buy' : 'sell'" @mouseenter="backtestHover = event">
                    <circle :cx="event.x" :cy="event.y" r="6" /><text :x="event.x" :y="event.y + 3" text-anchor="middle">{{ event.side }}</text>
                  </g>
                </svg>
                <div v-if="backtestHover" class="trade-tooltip" :style="{ left: `${Math.min(88, Math.max(12, backtestHover.x / 10))}%` }"><b>{{ backtestHover.side === 'B' ? '买入' : '卖出' }} · {{ formatDate(backtestHover.timestamp) }}</b><span>开盘成交价 {{ backtestHover.price }}</span><span>净值 {{ backtestHover.equity.toLocaleString() }}</span><span v-if="backtestHover.signal_timestamp">信号日 {{ formatDate(backtestHover.signal_timestamp) }} · 收盘 {{ backtestHover.signal_price }}</span><small>{{ backtestHover.reason }}</small></div>
                <span>青色：策略净值 · 灰色：沪深300 · 红色 B：买入 · 绿色 S：卖出</span>
              </div>
              <div class="chart candle-chart signal-candle-chart">
                <svg viewBox="0 0 1000 240" preserveAspectRatio="none">
                  <g v-for="(candle,index) in backtestCandleChart.bars" :key="index" class="candle" :class="{up:candle.up,down:!candle.up}"><line :x1="candle.x" :x2="candle.x" :y1="candle.yHigh" :y2="candle.yLow"/><rect :x="candle.x-candle.width/2" :y="candle.bodyY" :width="candle.width" :height="candle.bodyHeight"/></g>
                  <g v-for="event in backtestCandleChart.markers" :key="`${event.timestamp}-${event.side}`" class="trade-marker" :class="event.side==='B'?'buy':'sell'"><circle :cx="event.x" :cy="event.y" r="8"/><text :x="event.x" :y="event.y+3" text-anchor="middle">{{ event.side }}</text></g>
                </svg><span>{{ backtestResult.adjustment }} K 线 · 红色 B 买入 · 绿色 S 卖出</span>
              </div>
              <div class="metrics backtest-metrics"><article><small>最终资产</small><b>{{ backtestResult.final_equity.toLocaleString() }}</b></article><article><small>年化收益</small><b>{{ format(backtestResult.annualized_return_pct, '%') }}</b></article><article><small>沪深300收益</small><b>{{ backtestResult.benchmark_return_pct }}%</b></article><article><small>沪深300超额</small><b>{{ format(backtestResult.excess_return_pct, '%') }}</b></article><article><small>持仓日均复利</small><b>{{ format(backtestResult.holding_daily_return_pct, '%') }}</b></article><article><small>持仓交易日</small><b>{{ backtestResult.holding_days }}</b></article><article><small>最大回撤</small><b>{{ backtestResult.max_drawdown_pct }}%</b></article><article><small>夏普</small><b>{{ backtestResult.sharpe }}</b></article><article><small>委托次数</small><b>{{ backtestResult.trades }}</b></article><article><small>已平仓胜率</small><b>{{ format(backtestResult.win_rate_pct, '%') }}</b></article></div>
              <p v-if="backtestResult.fundamental_filter?.enabled" class="fundamental-filter-summary">基本面评分已启用：最低 {{ backtestResult.fundamental_filter.threshold }} 分；载入 {{ backtestResult.fundamental_filter.reports }} 期财报，过滤 {{ backtestResult.fundamental_filter.filtered_entries }} 次低分入场，{{ backtestResult.fundamental_filter.missing_entries }} 次因当时无已公布财报未入场。</p>
              <div v-if="backtestResult.trade_events.length" class="trade-log"><div class="trade-log-head"><span>方向</span><span>成交日/开盘价</span><span>信号日/收盘价</span><span>成交后净值</span><span>信号原因</span></div><div v-for="event in backtestResult.trade_events" :key="`${event.timestamp}-${event.side}-row`"><b :class="event.side === 'B' ? 'buy-text' : 'sell-text'">{{ event.side }}</b><span>{{ formatDate(event.timestamp) }}<br>{{ event.price }}</span><span>{{ formatDate(event.signal_timestamp) }}<br>{{ event.signal_price }}</span><span>{{ event.equity.toLocaleString() }}</span><small :title="event.reason">{{ event.reason }}</small></div></div>
              <p class="source">{{ backtestResult.warning }} 数据源：{{ backtestResult.data_source }}；价格口径：{{ backtestResult.adjustment }}。{{ backtestResult.adjustment_note || '' }} {{ backtestResult.indicator_note }}</p>
            </div>
            <div v-else-if="backtestLoading" class="thinking"><i></i><p>正在获取历史数据并模拟策略……</p></div>
            <div v-else class="empty"><b>⌁</b><p>选择策略后运行一次回测</p></div>
            <p v-if="backtestError" class="error">{{ backtestError }}</p>
          </div>
        </section>

        <section v-if="false" class="batch-panel">
          <div class="section-title"><span>03</span><div><small>BATCH BENCHMARK</small><h2>批量策略对比</h2></div></div>
          <div class="batch-grid">
            <form class="batch-form" @submit.prevent="runBatchBacktest">
              <label>股票代码（换行、空格或逗号分隔）<textarea v-model="batchSymbols" placeholder="600519&#10;000858&#10;601318"></textarea></label>
              <div class="two-col"><label>开始日期<input v-model="backtestStartDate" type="date" /></label><label>结束日期<input v-model="backtestEndDate" type="date" /></label></div>
              <label>市场<select v-model="market"><option>自动</option><option>A股</option><option>港股</option><option>美股</option></select></label>
              <button :disabled="batchLoading"><span v-if="batchLoading" class="loader"></span><template v-else>运行全部策略 ↗</template></button>
              <p class="form-note">对每个标的运行全部内置策略并保存记录，支持精确日期范围，最多 30 个标的。全市场回测请使用“策略雷达”。</p>
              <p v-if="batchError" class="error">{{ batchError }}</p>
            </form>

            <div class="batch-output">
              <template v-if="batchResult">
                <div class="batch-status"><span>成功 {{ batchResult.succeeded_symbols }} / {{ batchResult.requested_symbols }}</span><span>周期 {{ periodLabel(batchResult.period) }}</span><span v-if="batchResult.failed_symbols" class="negative-text">失败 {{ batchResult.failed_symbols }}</span></div>
                <div class="batch-table-wrap">
                  <table class="batch-table summary-table">
                    <thead><tr><th>策略</th><th>平均收益</th><th>平均年化</th><th>持仓日均复利</th><th>平均持仓日</th><th>沪深300收益</th><th>平均超额</th><th>平均回撤</th><th>夏普均值</th><th>平均委托</th><th>平均胜率</th><th>盈利标的</th><th>跑赢标的</th></tr></thead>
                    <tbody><tr v-for="row in batchResult.summaries" :key="row.strategy"><td>{{ row.strategy }}</td><td :class="row.average_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.average_return_pct }}%</td><td :class="row.average_annualized_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ format(row.average_annualized_return_pct, '%') }}</td><td :class="row.average_holding_daily_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ format(row.average_holding_daily_return_pct, '%') }}</td><td>{{ row.average_holding_days }}</td><td>{{ row.average_benchmark_return_pct }}%</td><td :class="row.average_excess_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.average_excess_return_pct }}%</td><td>{{ row.average_max_drawdown_pct }}%</td><td>{{ row.average_sharpe }}</td><td>{{ row.average_trades }}</td><td>{{ format(row.average_win_rate_pct, '%') }}</td><td>{{ row.profitable_rate_pct }}%</td><td>{{ row.outperform_rate_pct }}%</td></tr></tbody>
                  </table>
                </div>
                <div v-for="item in batchCurveItems" :key="`batch-${item.strategy}`" class="summary-curve"><b>{{ item.strategy }}：组合收益 vs 沪深300</b><div class="backtest-chart chart"><svg viewBox="0 0 1000 240" preserveAspectRatio="none"><polyline class="equity-line" :points="item.points.equity" /><polyline class="benchmark-line" :points="item.points.benchmark" /></svg><span>青色：策略组合 · 灰色：沪深300 · 初始净值100</span></div></div>
                <details class="batch-details"><summary>查看全部 {{ batchResult.results.length }} 条标的明细</summary><div class="batch-table-wrap"><table class="batch-table"><thead><tr><th>标的</th><th>策略</th><th>策略收益</th><th>持仓日均复利</th><th>持仓日</th><th>沪深300收益</th><th>超额收益</th><th>最大回撤</th><th>夏普</th><th>委托</th><th>胜率</th></tr></thead><tbody><tr v-for="(row, index) in batchResult.results" :key="`${row.symbol}-${row.strategy}-${index}`"><td>{{ row.name }}<small>{{ row.symbol }}</small></td><td>{{ row.strategy }}</td><td :class="row.total_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.total_return_pct }}%</td><td :class="row.holding_daily_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ format(row.holding_daily_return_pct, '%') }}</td><td>{{ row.holding_days }}</td><td>{{ row.benchmark_return_pct }}%</td><td :class="row.excess_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.excess_return_pct }}%</td><td>{{ row.max_drawdown_pct }}%</td><td>{{ row.sharpe }}</td><td>{{ row.trades }}</td><td>{{ format(row.win_rate_pct, '%') }}</td></tr></tbody></table></div></details>
                <div v-if="batchResult.errors.length" class="batch-errors"><p v-for="item in batchResult.errors" :key="item.symbol">{{ item.symbol }}：{{ item.message }}</p></div>
              </template>
              <div v-else-if="batchLoading" class="thinking batch-thinking"><i></i><p>正在拉取多只股票行情并运行全部策略……</p></div>
              <div v-else class="empty batch-empty"><b>▦</b><p>运行后生成策略平均收益对比表</p></div>
            </div>
          </div>
        </section>

      </template>

      <template v-else-if="activeTab === 'factors'">
        <section class="hero compact-hero"><p>FACTOR RESEARCH LAB</p><h1>先证明因子有效，<br><em>再把它放进策略。</em></h1><small>月末横截面 · Rank IC · 五分组多空收益 · 因子相关性</small></section>
        <section class="factor-lab">
          <aside class="factor-controls">
            <div class="section-title"><span>01</span><div><small>UNIVERSE</small><h2>研究参数</h2></div></div>
            <div class="two-col"><label>开始日期<input v-model="factorStartDate" type="date" :max="factorEndDate" /></label><label>结束日期<input v-model="factorEndDate" type="date" :min="factorStartDate" :max="todayDate" /></label></div>
            <div class="two-col"><label>未来收益周期<select v-model.number="factorForwardDays"><option :value="5">5个交易日</option><option :value="10">10个交易日</option><option :value="20">20个交易日</option><option :value="40">40个交易日</option><option :value="60">60个交易日</option></select></label><label>股票池上限<input v-model.number="factorUniverseLimit" type="number" min="100" max="6000" step="100" /></label></div>
            <div class="section-title factor-picker-title"><span>02</span><div><small>FACTORS</small><h2>选择对比因子</h2></div></div>
            <div class="factor-picker">
              <label v-for="factor in factorCatalog" :key="factor.id" :class="{ selected: selectedFactors.includes(factor.id) }">
                <input v-model="selectedFactors" type="checkbox" :value="factor.id" :disabled="selectedFactors.length >= 9 && !selectedFactors.includes(factor.id)" />
                <span><b>{{ factor.name }}</b><small>{{ factor.category }} · {{ factor.direction }}</small></span>
              </label>
            </div>
            <button class="action-button wide-button" :disabled="factorLoading || !selectedFactors.length" @click="runFactorAnalysis"><span v-if="factorLoading" class="loader"></span><template v-else>运行多因子检验 ↗</template></button>
            <p class="form-note">默认从本地A股代码序列均匀抽样1200只，避免只取某个交易所或代码段；提高到6000可覆盖库内全市场，但会增加计算时间。复合因子使用所选因子横截面缩尾、标准化后的等权平均。</p>
            <p v-if="factorError" class="error">{{ factorError }}</p>
          </aside>
          <div class="factor-output">
            <div class="section-title"><span>03</span><div><small>RESEARCH OUTPUT</small><h2>因子有效性与相关性</h2></div></div>
            <div v-if="factorLoading" class="thinking factor-thinking"><i></i><p>正在构造月末横截面并计算未来收益，股票池较大时需要等待……</p></div>
            <template v-else-if="factorResult">
              <div class="factor-summary-strip"><span>股票 {{ factorResult.universe.instruments }} 只</span><span>有效截面 {{ factorResult.universe.periods }} 期</span><span>最后样本日 {{ factorResult.universe.latest_sample_date }}</span><span>持有 {{ factorResult.parameters.forward_days }} 日</span></div>
              <button class="secondary-button factor-to-pool" @click="useResearchFactorsInPool">将当前因子等权送入资金池回测 →</button>
              <div class="factor-metric-grid">
                <article v-for="row in factorResult.summaries" :key="row.factor"><small>{{ factorResult.labels[row.factor] }}</small><b :class="row.mean_ic >= 0 ? 'positive-text' : 'negative-text'">IC {{ row.mean_ic }}</b><span>IR {{ row.ic_ir }} · 正IC {{ row.positive_ic_rate_pct }}%</span><em>多空累计 {{ row.long_short_return_pct }}%</em></article>
              </div>
              <section class="factor-chart-card"><div><small>FACTOR SCORECARD</small><h3>Mean IC 与 IC IR</h3></div><FactorChart :option="factorMetricOption" height="350px" /></section>
              <section class="factor-chart-card"><div><small>LONG / SHORT</small><h3>五分组多空累计收益</h3></div><FactorChart :option="factorCurveOption" height="390px" /></section>
              <div class="factor-chart-pair">
                <section class="factor-chart-card"><div><small>IC STABILITY</small><h3>月度 Rank IC</h3></div><FactorChart :option="factorIcOption" height="460px" /></section>
                <section class="factor-chart-card"><div><small>REDUNDANCY</small><h3>因子秩相关热力图</h3></div><FactorChart :option="factorCorrelationOption" height="520px" /></section>
              </div>
              <details class="factor-methodology"><summary>查看计算口径与偏差说明</summary><p>{{ factorResult.methodology.factor_timing }}</p><p>{{ factorResult.methodology.return_timing }}</p><p>{{ factorResult.methodology.preprocessing }}</p><p>{{ factorResult.methodology.long_short }}</p><p class="negative-text">{{ factorResult.methodology.bias_warning }}</p></details>
            </template>
            <div v-else class="empty factor-empty"><b>∑</b><p>选择因子后运行横截面检验</p></div>
          </div>
        </section>
      </template>

      <template v-else-if="activeTab === 'capital'">
        <section class="hero compact-hero"><p>CAPITAL POOL BACKTEST</p><h1>不再假设每只股票都有一份本金，<br><em>用一个真实共享账户回测。</em></h1><small>动态前复权 · 多策略信号 · 自定义开仓条件 · 共享现金</small></section>
        <section class="full-market-backtest">
          <div class="market-task-controls">
            <div class="section-title"><span>01</span><div><small>CAPITAL</small><h2>资金池参数</h2></div></div>
            <div class="two-col"><label>开始日期<input v-model="backtestStartDate" type="date" :max="backtestEndDate || todayDate" /></label><label>结束日期<input v-model="backtestEndDate" type="date" :min="backtestStartDate" :max="todayDate" /></label></div>
            <label>资金池策略<select v-model="poolStrategy"><option>均线交叉</option><option>RSI反转</option><option>RSI反转+止盈止损</option><option>RSI17反转+止盈止损</option><option>RSI反转+暴跌强化风控</option><option>红利质量动量</option><option>多因子月度轮动</option><option>自适应趋势轮动</option><option>纯A股ETF-V25</option><option>纯A股ETF-14基线</option><option>多周期趋势跟随</option><option>KDJ急跌首阳T+1</option><option>建仓波J&lt;0动态止盈</option><option>MA20/60首次回踩</option><option>MA20/55金叉后J&lt;13且涨幅&lt;15%</option><option>MA120回踩5日不破</option><option>MA200回踩5日不破</option></select></label>
            <label>资金池行情口径<select v-model="poolAdjustmentMode"><option>动态前复权</option><option>前复权</option><option>后复权</option><option>不复权</option></select></label>
            <label v-if="!['红利质量动量','多因子月度轮动'].includes(poolStrategy)">候选排名方式<select v-model="poolCandidateRanking"><option value="rsi_rebound_score">RSI止跌评分（当前逻辑）</option><option value="ten_day_decline_rank_15_39">近10日跌幅榜第15～39名</option><option value="multifactor_score">多因子综合评分</option></select></label>
            <p v-if="!['红利质量动量','多因子月度轮动'].includes(poolStrategy)" class="form-note">选择“多因子综合评分”时，原策略仍决定何时产生买卖信号，多因子只负责在同日候选中排序；这是与RSI、回踩和趋势策略结合的推荐方式。</p>
            <div v-if="poolStrategy === '红利质量动量'">
              <button class="secondary-button wide-button" :disabled="taskLoading" @click="startTask('dividend')">同步全市场历史分红</button>
              <p class="form-note">策略按月调仓，持有综合评分前10只；评分为红利55%+质量20%+估值10%+50日动量15%，单票止损15%。运行前需完成历史财报和历史分红同步。</p>
            </div>
            <div v-if="poolStrategy === '多因子月度轮动' || poolCandidateRanking === 'multifactor_score'" class="pool-factor-panel">
              <div class="pool-factor-head"><b>多因子权重</b><span>合计 {{ poolFactorWeightTotal.toFixed(2) }}</span></div>
              <div class="pool-factor-grid">
                <label v-for="factor in factorCatalog" :key="`pool-${factor.id}`"><span>{{ factor.name }}</span><input v-model.number="poolFactorWeights[factor.id]" type="number" min="0" max="100" step="5" /></label>
              </div>
              <p class="form-note">权重无需恰好等于100，后端会自动归一化。填写0表示不使用；带基本面、ROE或成长因子时必须先完成历史财报同步。月度轮动模式由因子同时决定选股和换仓；与旧策略结合时，因子只排序旧策略当天产生的候选。</p>
            </div>
            <label>初始入市资金<input v-model.number="poolInitialCapital" type="number" min="10000" max="1000000000" step="100000" /></label>
            <template v-if="!['自适应趋势轮动','纯A股ETF-V25','纯A股ETF-14基线'].includes(poolStrategy)"><div class="two-col"><label>总仓位上限（%）<input v-model.number="poolExposureLimit" type="number" min="1" max="100" step="1" /></label><label>单票仓位上限（%）<input v-model.number="poolSinglePositionLimit" type="number" min="0.1" max="100" step="0.1" /></label></div>
            <div class="two-col"><label>最大持仓数<input v-model.number="poolMaxPositions" type="number" min="1" max="100" /></label><label>单边基础滑点（bps）<input v-model.number="poolSlippageBps" type="number" min="0" max="100" step="1" /></label></div></template>
            <div v-if="!['自适应趋势轮动','纯A股ETF-V25','纯A股ETF-14基线'].includes(poolStrategy)" class="section-title compact-section-title"><span>02</span><div><small>ENTRY FILTER</small><h2>开仓条件</h2></div></div>
            <label v-if="['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy)">单日全市场信号开仓门槛（只统计对应RSI周期）<input v-model.number="poolMarketSignalThreshold" type="number" min="0" max="6000" step="1" /></label>
            <p v-if="['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy)" class="form-note">每天独立统计全市场对应 RSI 周期的原始入场信号（RSI17策略统计RSI17）；只有信号数量达到你填写的门槛，资金池才接受该日信号。填写0关闭。</p>
            <label v-if="['RSI反转+止盈止损', 'RSI17反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy)">或按全市场信号比例（%）<input v-model.number="poolMarketSignalRateThresholdPct" type="number" min="0" max="100" step="0.1" /></label>
            <label v-if="['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy)" class="toggle-control"><input v-model="poolBenchmarkDropFilterEnabled" type="checkbox" /><span>启用沪深300市场压力过滤（近5日跌幅达到设定阈值）</span></label>
            <label v-if="poolBenchmarkDropFilterEnabled && ['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy)">沪深300近5日最低跌幅（%）<input v-model.number="poolBenchmarkDropThresholdPct" type="number" min="0.1" max="50" step="0.5" /></label>
            <p v-if="['RSI反转+止盈止损', 'RSI反转+暴跌强化风控'].includes(poolStrategy) && poolBenchmarkDropFilterEnabled" class="form-note">仅当信号日前5个交易日沪深300跌幅达到4%或以上时，才允许 RSI 买入；这是市场压力过滤，不是个股成交流动性指标。</p>
            <label v-if="!['自适应趋势轮动','纯A股ETF-V25','纯A股ETF-14基线'].includes(poolStrategy)" class="toggle-control"><input v-model="poolVolumeRatioEnabled" type="checkbox" /><span>启用量能比过滤（≥1.2）</span></label>
            <p v-if="poolStrategy !== '自适应趋势轮动' && poolVolumeRatioEnabled" class="form-note">信号日最近5个交易日平均成交量 ÷ 此前30个交易日平均成交量必须达到1.2；仅使用当时已知数据。</p>
            <label v-if="poolStrategy !== '自适应趋势轮动'" class="toggle-control"><input v-model="poolFundamentalScoreEnabled" type="checkbox" /><span>启用基本面评分筛选</span></label>
            <label v-if="poolStrategy !== '自适应趋势轮动' && poolFundamentalScoreEnabled">基本面最低分<input v-model.number="poolFundamentalScoreThreshold" type="number" min="0" max="100" step="1" /></label>
            <p v-if="poolStrategy !== '自适应趋势轮动' && poolFundamentalScoreEnabled" class="form-note">只使用信号日当时已经公开的财报；评分不足或当时没有已公开财报均不允许开仓。全市场缓存覆盖不足80%时会拒绝回测并提示先同步。</p>
            <div v-if="poolStrategy !== '自适应趋势轮动'" class="two-col"><label>次日高开上限（%）<input v-model.number="poolMaxOpeningGapPct" type="number" min="0" max="20" step="0.5" /></label><label>近20日最低日均成交额（万元）<input v-model.number="poolMinimumTurnoverWan" type="number" min="0" max="1000000" step="1000" /></label></div>
            <div v-if="poolStrategy !== '自适应趋势轮动'" class="indicator-picker"><label><input v-model="poolRequireAboveMa200" type="checkbox" />信号日收盘价高于MA200</label><label><input v-model="poolRequireMa200Rising" type="checkbox" />信号日MA200高于5日前</label></div>
            <button class="action-button wide-button" :disabled="poolLoading || ((poolStrategy === '多因子月度轮动' || poolCandidateRanking === 'multifactor_score') && poolFactorWeightTotal <= 0)" @click="startCapitalPoolBacktest">运行资金池回测 ↗</button>
            <p v-if="poolStrategy !== '自适应趋势轮动'" class="form-note">先由所选策略生成买卖信号，再按上述条件过滤开仓候选，最后由共享资金池分配仓位。默认单边滑点5bps：买入价上浮0.05%，卖出价下调0.05%；成交量冲击保持关闭。MA200条件只使用信号日及此前数据；次日高开条件在开盘成交前判断，不使用未来数据。</p>
            <p v-else-if="poolStrategy === '自适应趋势轮动'" class="form-note">固定参数：ETF 月度调仓，MA200 趋势过滤，12-1/3/6/12 月动量综合评分，近60日低波动加分；最多持有5个资产，单资产上限25%。</p>
            <p v-else-if="poolStrategy === '纯A股ETF-V25'" class="form-note">复现参数固定：35只纯A股行业ETF，周五收盘调仓，多周期风险调整动量、短期反转、动量加速度、Leg A + Leg G、黄金防御门；最多持有8只，单只上限25%。</p>
            <p v-else class="form-note">复现参数固定：14只纯A股ETF，20日动量排名，MA200趋势过滤，每5个交易日调仓，持有排名前3只。</p>
            <button class="secondary-button wide-button" :disabled="poolPlanLoading" @click="generateCapitalPoolPlan">生成明日交易计划 ↗</button>
            <p class="form-note">按最新交易日收盘信号生成下一交易日开盘清单：只把“建议买入”列为开仓候选，卖出只针对已追踪持仓；计划不会把卖出误计入开仓数量。</p>
          </div>
          <div class="market-task-output">
            <div class="section-title"><span>03</span><div><small>PORTFOLIO RESULT</small><h2>资金池收益与沪深300</h2></div></div>
            <section v-if="poolPlanState" class="pool-plan-card">
              <div class="pool-report-head"><div><small>NEXT SESSION PLAN</small><b>明日资金池交易计划</b></div><span>{{ poolPlanState.result?.trade_date || '扫描中' }}</span></div>
              <div class="job-card"><b :class="poolPlanState.status">{{ poolPlanState.status }}</b><span>{{ poolPlanState.message }}</span><progress :max="poolPlanState.progress_total || 1" :value="poolPlanState.progress_current || 0"></progress><small>{{ poolPlanState.progress_current || 0 }} / {{ poolPlanState.progress_total || 0 }}</small><p v-if="poolPlanState.error" class="error">{{ poolPlanState.error }}</p></div>
              <template v-if="poolPlanState.result">
                <div class="pool-plan-grid"><article><small>建议卖出</small><b class="sell-text">{{ poolPlanState.result.sell_candidates?.length || 0 }} 只</b><span>仅统计已追踪持仓</span></article><article><small>建议买入</small><b class="buy-text">{{ poolPlanState.result.buy_candidates?.length || 0 }} 只</b><span>已按持仓上限筛选</span></article><article><small>持仓数 / 空位</small><b>{{ poolPlanState.result.holdings?.length || 0 }} / {{ poolPlanState.result.capacity ?? '—' }}</b><span>最大持仓 {{ poolMaxPositions }} 只</span></article></div>
                <div v-if="poolPlanState.result.sell_candidates?.length" class="batch-table-wrap"><h3 class="sell-text">明日卖出</h3><table class="batch-table"><thead><tr><th>股票</th><th>持仓日期</th><th>持仓价</th><th>信号价</th><th>原因</th></tr></thead><tbody><tr v-for="item in poolPlanState.result.sell_candidates" :key="`plan-s-${item.symbol}`"><td>{{ item.name }} · {{ item.symbol }}</td><td>{{ item.entry_date }}</td><td>{{ item.entry_price }}</td><td>{{ item.signal_price }}</td><td>{{ item.reason }}</td></tr></tbody></table></div>
                <div v-if="poolPlanState.result.buy_candidates?.length" class="batch-table-wrap"><h3 class="buy-text">明日开仓候选</h3><table class="batch-table"><thead><tr><th>排名</th><th>股票</th><th>信号价</th><th>门槛信号数</th><th>原因</th></tr></thead><tbody><tr v-for="(item,index) in poolPlanState.result.buy_candidates" :key="`plan-b-${item.symbol}`"><td>{{ index + 1 }}</td><td>{{ item.name }} · {{ item.symbol }}</td><td>{{ item.signal_price }}</td><td>{{ item.signal_count ?? '关闭' }}</td><td>{{ item.reason }}</td></tr></tbody></table></div>
                <p v-if="!poolPlanState.result.sell_candidates?.length && !poolPlanState.result.buy_candidates?.length" class="empty"><b>◎</b><span>最新交易日没有符合资金池规则的开仓或卖出计划</span></p>
                <p class="form-note">{{ poolPlanState.result.next_trade_day_note }}</p>
              </template>
            </section>
            <template v-if="poolTaskState">
              <div class="job-card"><b :class="poolTaskState.status">{{ poolTaskState.status }}</b><span>{{ poolTaskState.message }}</span><progress :max="poolTaskState.progress_total || 1" :value="poolTaskState.progress_current || 0"></progress><small>{{ poolTaskState.progress_current || 0 }} / {{ poolTaskState.progress_total || 0 }}</small><p v-if="poolTaskState.error" class="error">{{ poolTaskState.error }}</p></div>
              <template v-if="poolTaskState.result?.curve?.length">
                <div class="pool-report-head"><div><small>BACKTEST REPORT</small><b>{{ poolTaskState.result.strategy }}</b></div><span>{{ backtestStartDate }} → {{ backtestEndDate }}</span></div>
                <div class="pool-kpi-grid">
                  <article class="primary"><small>组合累计收益</small><b :class="poolTaskState.result.total_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ poolTaskState.result.total_return_pct }}%</b><span>年化 {{ format(poolTaskState.result.annualized_return_pct, '%') }}</span></article>
                  <article><small>同期沪深300</small><b>{{ poolTaskState.result.benchmark_return_pct }}%</b><span>基准收益</span></article>
                  <article><small>累计超额收益</small><b :class="poolTaskState.result.excess_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ poolTaskState.result.excess_return_pct }}%</b><span>策略－基准</span></article>
                  <article><small>期末总资产</small><b>¥{{ formatMoney(poolTaskState.result.final_equity, 0) }}</b><span>初始 ¥{{ formatMoney(poolTaskState.result.initial_capital, 0) }}</span></article>
                </div>
                <div class="pool-stat-grid">
                  <article><small>最大回撤</small><b class="negative-text">{{ poolTaskState.result.max_drawdown_pct }}%</b></article><article><small>夏普比率</small><b>{{ poolTaskState.result.sharpe }}</b></article><article><small>平均仓位</small><b>{{ poolTaskState.result.average_exposure_pct }}%</b></article><article><small>平仓胜率</small><b>{{ format(poolTaskState.result.win_rate_pct, '%') }}</b></article><article><small>候选 / 采用</small><b>{{ poolTaskState.result.candidate_trades }} / {{ poolTaskState.result.selected_trades }}</b></article><article><small>持仓日均复利</small><b>{{ format(poolTaskState.result.holding_daily_return_pct, '%') }}</b></article>
                </div>
                <section class="pool-chart-card"><div><small>PERFORMANCE & EXPOSURE</small><h3>组合净值、沪深300与仓位</h3></div><FactorChart :option="poolPerformanceOption" height="430px" /></section>
                <section class="pool-chart-card"><div><small>UNDERWATER</small><h3>策略与基准回撤</h3></div><FactorChart :option="poolRiskOption" height="290px" /></section>
                <section class="pool-chart-card pool-trade-chart-card">
                  <div class="pool-trade-chart-head"><div><small>TRADE REVIEW</small><h3>单票成交K线复盘</h3></div><label>查看股票<select v-model="poolTradeSymbol" @change="loadPoolTradeChart()"><option v-for="item in poolTradeSymbols" :key="item.symbol" :value="item.symbol">{{ item.name }} · {{ item.symbol }}</option></select></label></div>
                  <p class="form-note">点击下方任意成交记录可切换股票；红色B为买入，绿色S为卖出，标记价格使用实际成交价。</p>
                  <p v-if="poolFocusedTrade" class="pool-focused-trade"><b>{{ poolFocusedTrade.side === 'B' ? '买入' : '卖出' }} · {{ formatDate(poolFocusedTrade.timestamp) }}</b><span>成交价 {{ formatMoney(poolFocusedTrade.price, 4) }} · {{ Number(poolFocusedTrade.shares).toLocaleString() }}股</span><em>{{ poolFocusedTrade.reason }}</em></p>
                  <div v-if="poolTradeChartLoading" class="pool-chart-loading">正在加载动态前复权K线……</div>
                  <FactorChart v-else-if="poolTradeCandles?.points?.length" :option="poolTradeCandleOption" height="470px" />
                  <p v-if="poolTradeChartError" class="error">{{ poolTradeChartError }}</p>
                </section>
                <details class="pool-trade-details" open>
                  <summary><span>成交明细</span><small>{{ poolTradeSummary.orders }}笔委托 · 买入{{ poolTradeSummary.buys }} · 卖出{{ poolTradeSummary.sells }} · 已实现盈亏 <b :class="poolTradeSummary.realized >= 0 ? 'positive-text' : 'negative-text'">¥{{ formatMoney(poolTradeSummary.realized) }}</b></small></summary>
                  <div class="pool-table-wrap"><table class="pool-trade-table"><thead><tr><th>#</th><th>日期 / 方向</th><th>证券</th><th>成交价 / 数量</th><th>成交金额</th><th>实现盈亏</th><th>信号与成交原因</th></tr></thead><tbody><tr v-for="(event,index) in poolVisibleTradeEvents" :key="`${event.timestamp}-${event.symbol}-${event.side}-${index}`" :class="{ selected: Number(event.timestamp) === Number(poolFocusedTrade?.timestamp) && event.symbol === poolFocusedTrade?.symbol && event.side === poolFocusedTrade?.side }" @click="selectPoolTrade(event)"><td>{{ (poolTradePage - 1) * poolTradePageSize + index + 1 }}</td><td class="date-side-cell"><span>{{ formatDate(event.timestamp) }}</span><span class="side-badge" :class="event.side === 'B' ? 'buy' : 'sell'">{{ event.side === 'B' ? '买入' : '卖出' }}</span></td><td class="security-cell"><b>{{ event.name }}</b><small>{{ event.symbol }}</small></td><td class="price-size-cell"><b>{{ formatMoney(event.price, 4) }}</b><small>{{ Number(event.shares).toLocaleString() }} 股</small></td><td>¥{{ formatMoney(Number(event.price) * Number(event.shares)) }}</td><td :class="event.pnl == null ? '' : Number(event.pnl) >= 0 ? 'positive-text' : 'negative-text'">{{ event.pnl == null ? '—' : `¥${formatMoney(event.pnl)}` }}</td><td class="reason-cell">{{ event.reason }}</td></tr></tbody><tfoot><tr><td colspan="4">全量合计</td><td>¥{{ formatMoney(poolTradeSummary.turnover) }}</td><td :class="poolTradeSummary.realized >= 0 ? 'positive-text' : 'negative-text'">¥{{ formatMoney(poolTradeSummary.realized) }}</td><td>动态前复权 · 已计手续费与滑点</td></tr></tfoot></table></div>
                  <div class="pool-table-pagination"><span>第 {{ poolTradePage }} / {{ poolTradePageCount }} 页 · 每页 {{ poolTradePageSize }} 条</span><div><button :disabled="poolTradePage <= 1" @click="poolTradePage--">上一页</button><button :disabled="poolTradePage >= poolTradePageCount" @click="poolTradePage++">下一页</button></div></div>
                </details>
              </template>
            </template>
            <div v-else class="empty"><b>◫</b><p>设置资金规模与仓位约束后运行</p></div>
          </div>
        </section>
      </template>

      <template v-else-if="activeTab === 'strategy'">
        <section class="hero compact-hero"><p>AI STRATEGY STUDIO</p><h1>从自然语言到策略代码，<br><em>也从代码回到可读规则。</em></h1><small>受限 DSL · 无任意 Python 执行 · 信号统一下一交易日开盘成交</small></section>
        <section class="strategy-studio">
          <div class="studio-column"><div class="section-title"><span>01</span><div><small>TEXT → CODE</small><h2>策略文字描述</h2></div></div><textarea v-model="strategyDescription" class="code-editor natural-editor"></textarea><button class="action-button" :disabled="strategyLoading" @click="generateStrategy">AI 生成策略代码 ↗</button></div>
          <div class="studio-column"><div class="section-title"><span>02</span><div><small>SAFE DSL</small><h2>策略代码</h2></div></div><textarea v-model="strategyCode" class="code-editor" spellcheck="false" placeholder="NAME=...&#10;ENTRY=...&#10;EXIT=..."></textarea><div class="studio-actions"><button class="secondary-button" :disabled="strategyLoading || !strategyCode" @click="explainStrategy">AI 反推文字</button><button class="action-button" :disabled="!strategyCode" @click="useGeneratedStrategy">送入回测</button></div></div>
          <div class="studio-explanation"><div class="section-title"><span>03</span><div><small>EXPLANATION</small><h2>策略说明</h2></div></div><div v-if="strategyExplanation" class="markdown" v-html="DOMPurify.sanitize(marked.parse(strategyExplanation))"></div><div v-else class="empty studio-empty"><b>⌁</b><p>生成或粘贴策略后查看解释</p></div></div>
        </section>
      </template>

      <template v-else-if="activeTab === 'radar'">
        <section class="hero compact-hero"><p>FULL MARKET RADAR</p><h1>先把全市场放进数据库，<br><em>再扫描今天发生了什么。</em></h1><small>A股全市场 · 2020 年至今 · 异步任务可查询进度</small></section>
        <section class="radar-panel">
          <div class="radar-controls"><div class="section-title"><span>01</span><div><small>PIPELINE</small><h2>全市场任务</h2></div></div><div class="two-col"><label>开始日期<input v-model="backtestStartDate" type="date" /></label><label>结束日期<input v-model="backtestEndDate" type="date" /></label></div><label>扫描/回测策略<select v-model="backtestStrategy"><option>均线交叉</option><option>RSI反转</option><option>RSI反转+止盈止损</option><option>多周期趋势跟随</option><option>KDJ急跌首阳T+1</option><option>建仓波J&lt;0动态止盈</option><option>MA20/60首次回踩</option><option>MA20/55金叉后J&lt;13且涨幅&lt;15%</option><option>MA120回踩5日不破</option><option>MA200回踩5日不破</option></select></label><button class="action-button" :disabled="taskLoading" @click="startTask('sync')">① 同步全市场行情</button><button class="secondary-button" :disabled="taskLoading" @click="startTask('backtest')">② 全市场策略回测</button><button class="secondary-button" :disabled="taskLoading" @click="startTask('scan')">③ 扫描今日开仓信号</button><p class="form-note">首次同步约数千只股票，会受到行情源限速影响；任务在后端持续运行，前端每 2 秒读取进度。</p></div>
          <div class="radar-output"><div class="section-title"><span>02</span><div><small>JOB STATUS</small><h2>任务状态</h2></div></div><template v-if="taskState"><div class="job-card"><b :class="taskState.status">{{ taskState.status }}</b><span>{{ taskState.message }}</span><progress :max="taskState.progress_total || 1" :value="taskState.progress_current || 0"></progress><small>{{ taskState.progress_current }} / {{ taskState.progress_total }}</small><p v-if="taskState.error" class="error">{{ taskState.error }}</p></div><div v-if="scanSignals.length" class="batch-table-wrap"><table class="batch-table"><thead><tr><th>信号日</th><th>股票</th><th>策略</th><th>方向</th><th>价格</th><th>原因</th></tr></thead><tbody><tr v-for="item in scanSignals" :key="item.id"><td>{{ item.signal_date }}</td><td>{{ item.symbol }}</td><td>{{ item.strategy }}</td><td :class="item.side === 'B' ? 'buy-text' : 'sell-text'">{{ item.side === 'B' ? '开仓' : '卖出' }}</td><td>{{ item.price }}</td><td>{{ item.reason }}</td></tr></tbody></table></div><div v-if="trackedPositions.length" class="batch-table-wrap"><h3>当前策略持仓追踪</h3><table class="batch-table"><thead><tr><th>买入日期</th><th>股票</th><th>策略</th><th>买入价</th><th>状态</th><th>说明</th></tr></thead><tbody><tr v-for="item in trackedPositions" :key="item.id"><td>{{ item.entry_date }}</td><td>{{ item.symbol }}</td><td>{{ item.strategy }}</td><td>{{ item.entry_price }}</td><td class="buy-text">持有中</td><td>下一次扫描出现卖出信号时提醒</td></tr></tbody></table></div></template><div v-else class="empty"><b>◎</b><p>按顺序同步、回测或扫描</p></div></div>
        </section>
      </template>

      <template v-else-if="activeTab === 'records'">
        <section class="hero compact-hero"><p>BACKTEST ARCHIVE</p><h1>每一次回测都保存，<br><em>每一个结论都能复查。</em></h1><small>参数、汇总、资产曲线与买卖记录完整保存</small></section>
        <section class="records-panel">
          <div class="section-title"><span>01</span><div><small>HISTORY</small><h2>回测记录</h2></div></div>
          <div v-if="records.length" class="record-toolbar"><span>勾选 2～10 条记录进行收益 PK</span><button class="action-button" :disabled="compareRunIds.length < 2 || comparisonLoading" @click="compareRecords">PK 对比（{{ compareRunIds.length }}）</button></div>
          <div v-if="records.length" class="record-list">
            <article v-for="record in records" :key="record.id" :class="{ selected: selectedRecordId === record.id }">
              <label class="compare-check"><input v-model="compareRunIds" type="checkbox" :value="record.id" :disabled="compareRunIds.length >= 10 && !compareRunIds.includes(record.id)" />PK</label>
              <button class="record-open" @click="openRecord(record.id)"><div><b>{{ record.name }}</b><small>{{ record.start_date }} → {{ record.end_date }}</small></div><span>{{ record.scope }} · {{ record.result_count }} 个结果</span></button>
              <div class="record-return"><small>记录平均收益</small><b :class="recordAverageReturn(record) >= 0 ? 'positive-text' : 'negative-text'">{{ format(recordAverageReturn(record), '%') }}</b></div>
              <em :class="record.status">{{ record.status }}</em>
              <button class="record-delete" title="删除记录" @click="deleteRecord(record)">删除</button>
            </article>
          </div>
          <div v-else-if="recordsLoading" class="thinking"><i></i><p>加载回测记录……</p></div><div v-else class="empty"><b>□</b><p>还没有保存的回测</p></div>
          <p v-if="comparisonError" class="error">{{ comparisonError }}</p>
          <div v-if="comparisonResult?.rows?.length" class="comparison-panel">
            <div class="section-title"><span>PK</span><div><small>RECORD COMPARISON</small><h2>收益与风险对比</h2></div></div>
            <div class="batch-table-wrap"><table class="batch-table comparison-table"><thead><tr><th>排名</th><th>记录</th><th>策略</th><th>日期</th><th>样本</th><th>平均收益</th><th>平均年化</th><th>组合收益</th><th>沪深300</th><th>超额</th><th>组合回撤</th><th>组合夏普</th><th>个股夏普均值</th><th>胜率</th><th>盈利比例</th><th>跑赢比例</th></tr></thead><tbody><tr v-for="row in comparisonResult.rows" :key="`${row.run_id}-${row.strategy}`"><td>{{ row.rank }}</td><td>{{ row.record_name }}</td><td>{{ row.strategy }}</td><td>{{ row.start_date }} → {{ row.end_date }}</td><td>{{ row.samples || row.result_count }}</td><td :class="row.average_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.average_return_pct }}%</td><td :class="row.average_annualized_return_pct >= 0 ? 'positive-text':'negative-text'">{{ format(row.average_annualized_return_pct, '%') }}</td><td :class="row.portfolio_return_pct >= 0 ? 'positive-text':'negative-text'">{{ format(row.portfolio_return_pct, '%') }}</td><td>{{ row.average_benchmark_return_pct }}%</td><td :class="row.average_excess_return_pct >= 0 ? 'positive-text' : 'negative-text'">{{ row.average_excess_return_pct }}%</td><td>{{ format(row.portfolio_max_drawdown_pct, '%') }}</td><td>{{ format(row.portfolio_sharpe) }}</td><td>{{ format(row.average_sharpe) }}</td><td>{{ format(row.average_win_rate_pct, '%') }}</td><td>{{ row.profitable_rate_pct }}%</td><td>{{ row.outperform_rate_pct }}%</td></tr></tbody></table></div>
            <div class="comparison-curve-grid"><div v-for="item in comparisonCurveRows" :key="`pk-${item.run_id}-${item.strategy}`" class="summary-curve"><b>{{ item.record_name }} · {{ item.strategy }}</b><div class="backtest-chart chart"><svg viewBox="0 0 1000 240" preserveAspectRatio="none"><polyline class="equity-line" :points="item.points.equity" /><polyline class="benchmark-line" :points="item.points.benchmark" /></svg><span>青色：策略组合 · 灰色：沪深300</span></div></div></div>
          </div>
          <div v-if="recordCurveItems.length" class="record-summary-curves"><div class="section-title"><span>02</span><div><small>PORTFOLIO CURVE</small><h2>多策略组合净值与沪深300</h2></div></div><FactorChart :option="recordPortfolioOption" height="430px" /></div>
          <div v-if="selectedRecordId" class="record-results">
            <div class="section-title"><span>02</span><div><small>SEARCH RESULTS</small><h2>查询个股回测结果</h2></div></div>
            <form class="record-search" @submit.prevent="searchRecordResults">
              <label>股票代码 / 名称<input v-model="recordQuery" placeholder="600519 / 贵州茅台" /></label>
              <label>策略<select v-model="recordStrategy"><option value="">全部策略</option><option v-for="strategy in selectedRecord?.strategies || []" :key="strategy" :value="strategy">{{ strategy }}</option></select></label>
              <label>排序<select v-model="recordSortBy"><option value="symbol">股票代码</option><option value="return">策略收益</option><option value="drawdown">最大回撤</option><option value="sharpe">夏普</option><option value="win_rate">胜率</option></select></label>
              <label>顺序<select v-model="recordSortOrder"><option value="asc">升序</option><option value="desc">降序</option></select></label>
              <button>查询</button>
            </form>
            <div class="result-count">匹配 {{ recordDetail?.total || 0 }} 条 · 当前第 {{ recordOffset + 1 }}～{{ Math.min(recordOffset + recordLimit, recordDetail?.total || 0) }} 条</div>
            <div v-if="recordDetail?.items?.length" class="batch-table-wrap"><table class="batch-table"><thead><tr><th>股票</th><th>策略</th><th>收益</th><th>年化收益</th><th>回撤</th><th>夏普</th><th>胜率</th><th>成交</th><th>查看</th></tr></thead><tbody><tr v-for="item in recordDetail.items" :key="item.result_id"><td>{{ item.name }}<small>{{ item.symbol }}</small></td><td>{{ item.strategy }}</td><td :class="item.total_return_pct>=0?'positive-text':'negative-text'">{{ item.total_return_pct }}%</td><td :class="item.annualized_return_pct>=0?'positive-text':'negative-text'">{{ format(item.annualized_return_pct,'%') }}</td><td>{{ item.max_drawdown_pct }}%</td><td>{{ item.sharpe }}</td><td>{{ format(item.win_rate_pct,'%') }}</td><td>{{ item.trades }}</td><td><button class="table-action" @click="openRecordResult(item)">{{ item.symbol === 'CAPITAL_POOL' ? '查看专业组合报告' : 'K线 / B·S / 资产' }}</button></td></tr></tbody></table></div>
            <div v-else-if="!recordsLoading" class="empty result-empty"><b>⌕</b><p>没有匹配的个股结果</p></div>
            <div class="pagination"><button class="secondary-button" :disabled="recordOffset === 0" @click="changeRecordPage(-1)">上一页</button><button class="secondary-button" :disabled="recordOffset + recordLimit >= (recordDetail?.total || 0)" @click="changeRecordPage(1)">下一页</button></div>
          </div>
        </section>
      </template>

      <template v-else-if="activeTab === 'news'">
        <section class="hero compact-hero news-hero">
          <p>NEWS INTELLIGENCE</p>
          <h1>把市场噪声，整理成<br><em>可追溯的事件线索。</em></h1>
          <small>聚合 Google News 与 Bing News · 保留原文链接 · 重要信息请以公司公告和交易所披露为准</small>
        </section>

        <section class="news-layout">
          <form class="query-card news-form" @submit.prevent="loadNews">
            <div class="section-title"><span>01</span><div><small>MONITOR</small><h2>新闻监控</h2></div></div>
            <label>公司 / 股票 / 关键词<input v-model="newsQuery" placeholder="贵州茅台 / 600519 / 人工智能" /></label>
            <div class="two-col">
              <label>时间范围<select v-model="newsLookback"><option value="1d">24 小时</option><option value="3d">3 天</option><option value="7d">7 天</option><option value="30d">30 天</option></select></label>
              <label>自动刷新<select v-model.number="autoRefresh"><option :value="0">关闭</option><option :value="5">5 分钟</option><option :value="15">15 分钟</option><option :value="30">30 分钟</option></select></label>
            </div>
            <label>Agent 关注重点<textarea v-model="newsFocus" maxlength="500" placeholder="例如：事件影响、政策风险、业绩线索"></textarea></label>
            <button :disabled="newsLoading || !newsQuery.trim()"><span v-if="newsLoading" class="loader"></span><template v-else>刷新并分析 ↗</template></button>
            <p class="form-note">自动刷新只在“新闻推送”页面打开时运行。</p>
          </form>

          <div class="news-content">
            <section class="news-agent-card">
              <div class="section-title"><span>02</span><div><small>AGENT BRIEF</small><h2>新闻研判</h2></div></div>
              <div v-if="newsAnalysis" class="markdown news-markdown" v-html="renderedNewsAnalysis"></div>
              <div v-else-if="newsLoading" class="thinking news-thinking"><i></i><p>正在检索新闻并梳理事件影响……</p></div>
              <div v-else class="empty news-empty"><b>⌁</b><p>输入关键词，生成带来源链接的流式新闻摘要</p></div>
              <p v-if="newsError" class="error">{{ newsError }}</p>
            </section>

            <section class="news-feed-card">
              <div class="news-feed-head">
                <div><small>03 / SOURCE FEED</small><h2>新闻原文</h2></div>
                <span v-if="lastNewsUpdate">更新于 {{ formatNewsTime(lastNewsUpdate) }}</span>
              </div>
              <div v-if="newsItems.length" class="news-list">
                <article v-for="(item, index) in newsItems" :key="`${item.url}-${index}`">
                  <div class="news-meta"><span>{{ item.source || '来源未知' }}</span><time>{{ formatNewsTime(item.published_at) }}</time></div>
                  <h3><a :href="item.url" target="_blank" rel="noopener noreferrer">{{ item.title }}</a></h3>
                  <p v-if="item.summary">{{ item.summary }}</p>
                  <a class="news-link" :href="item.url" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>
                </article>
              </div>
              <div v-else class="feed-empty">尚未获取新闻原文</div>
              <p class="source news-disclaimer">新闻聚合可能存在延迟、重复或标题偏差，不能替代上市公司公告、财报与交易所披露。</p>
            </section>
          </div>
        </section>
      </template>
    </main>
    <footer><span>STOCK RESEARCH AGENT · 2026</span><span>事实优先 · 情景思维 · 风险先行</span></footer>
  </div>
</template>
