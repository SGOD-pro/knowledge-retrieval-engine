import { useEffect } from "react"
import {
  TrendingUp,
  AlertTriangle,
  Clock,
  CheckSquare,
  Scale,
  Brain,
  Bell,
  ArrowUpRight,
  ArrowDownRight,
  Minus
} from "lucide-react"
import { useBenchmarkStore } from "../store/useBenchmarkStore"
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  CartesianGrid
} from "recharts"

export function BenchmarksPage() {
  const { benchmarks, activeTimeRange, setActiveTimeRange, fetchBenchmarks } =
    useBenchmarkStore()

  useEffect(() => {
    fetchBenchmarks()
  }, [fetchBenchmarks])

  const kpis = benchmarks.kpis

  // Custom Chart Tooltip
  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-[#1e2022] text-white p-2.5 rounded-lg shadow-lg border border-border/60 text-xs space-y-1">
          <div className="flex items-center gap-1.5 text-muted-foreground font-medium">
            <span className="h-2 w-2 rounded-full bg-[#c96442]" />
            <span>{label}, 14:00</span>
          </div>
          <div className="font-headline font-bold text-sm text-white">
            {payload[0].value}s
          </div>
        </div>
      )
    }
    return null
  }

  return (
    <div className="p-8 lg:p-12 max-w-7xl mx-auto space-y-8 animate-in fade-in-50 duration-200">
      {/* Top Header & Bell */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-lg bg-[#fdeae4] dark:bg-[#3d231b] text-[#c96442] dark:text-[#ffb59d]">
            <TrendingUp className="h-5 w-5" />
          </div>
          <h1 className="font-headline text-3xl sm:text-4xl font-bold tracking-tight text-foreground">
            System Benchmarks
          </h1>
        </div>

        <button
          type="button"
          className="p-2.5 rounded-full hover:bg-accent text-foreground transition-colors cursor-pointer"
          title="Notifications"
        >
          <Bell className="h-5 w-5" />
        </button>
      </div>

      {/* Evaluation Run Banner */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2">
        <div className="space-y-0.5">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
            Last Evaluation Run
          </span>
          <h2 className="font-headline text-2xl font-bold text-foreground">
            Production Model ({benchmarks.version})
          </h2>
        </div>

        {/* Status Badge */}
        <div className="flex items-center gap-2 px-4 py-2 rounded-xl bg-[#ffdad6] dark:bg-[#4a1818] text-[#ba1a1a] dark:text-[#ffb4ab] border border-[#ba1a1a]/20 shadow-xs">
          <AlertTriangle className="h-4 w-4" />
          <span className="text-xs font-bold tracking-wider uppercase">
            {benchmarks.status}
          </span>
        </div>
      </div>

      {/* 4 KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
        {/* KPI 1: p95 Latency */}
        <div className="p-6 rounded-2xl border border-border/80 bg-card shadow-xs flex flex-col justify-between space-y-4">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-xs font-medium">p95 Latency</span>
            <Clock className="h-4 w-4 text-[#c96442]" />
          </div>
          <div className="font-headline text-4xl font-bold text-foreground">
            {kpis.p95_latency.value}
            <span className="text-2xl font-normal text-muted-foreground ml-1">
              {kpis.p95_latency.unit}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-[#ba1a1a] dark:text-[#ffb4ab]">
            <ArrowUpRight className="h-3.5 w-3.5" />
            <span>{kpis.p95_latency.delta}</span>
          </div>
        </div>

        {/* KPI 2: Recall@5 */}
        <div className="p-6 rounded-2xl border border-border/80 bg-card shadow-xs flex flex-col justify-between space-y-4">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-xs font-medium">Recall@5</span>
            <CheckSquare className="h-4 w-4 text-[#c96442]" />
          </div>
          <div className="font-headline text-4xl font-bold text-foreground">
            {kpis.recall_5.value}
            <span className="text-2xl font-normal text-muted-foreground ml-1">
              {kpis.recall_5.unit}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-[#006768] dark:text-[#6cd7d8]">
            <ArrowUpRight className="h-3.5 w-3.5" />
            <span>{kpis.recall_5.delta}</span>
          </div>
        </div>

        {/* KPI 3: Faithfulness */}
        <div className="p-6 rounded-2xl border border-border/80 bg-card shadow-xs flex flex-col justify-between space-y-4">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-xs font-medium">Faithfulness</span>
            <Scale className="h-4 w-4 text-[#c96442]" />
          </div>
          <div className="font-headline text-4xl font-bold text-foreground">
            {kpis.faithfulness.value}
            <span className="text-2xl font-normal text-muted-foreground ml-1">
              {kpis.faithfulness.unit}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
            <Minus className="h-3.5 w-3.5" />
            <span>{kpis.faithfulness.delta}</span>
          </div>
        </div>

        {/* KPI 4: LLM Activation */}
        <div className="p-6 rounded-2xl border border-border/80 bg-card shadow-xs flex flex-col justify-between space-y-4">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="text-xs font-medium">LLM Activation</span>
            <Brain className="h-4 w-4 text-[#c96442]" />
          </div>
          <div className="font-headline text-4xl font-bold text-foreground">
            {kpis.llm_activation.value}
            <span className="text-2xl font-normal text-muted-foreground ml-1">
              {kpis.llm_activation.unit}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-xs font-semibold text-[#006768] dark:text-[#6cd7d8]">
            <ArrowDownRight className="h-3.5 w-3.5" />
            <span>{kpis.llm_activation.delta}</span>
          </div>
        </div>
      </div>

      {/* Latency Over Time Chart Card */}
      <div className="rounded-2xl border border-border/80 bg-card p-6 sm:p-8 shadow-xs space-y-6">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
          <div className="space-y-1">
            <h3 className="font-headline text-2xl font-bold text-foreground">
              Latency Over Time (p95)
            </h3>
            <p className="text-xs text-muted-foreground font-sans">
              Last 7 days, 1-hour intervals
            </p>
          </div>

          {/* Time Range Pills */}
          <div className="flex items-center p-1 rounded-xl bg-accent/60 border border-border/60">
            {(["24h", "7d", "30d"] as const).map((range) => (
              <button
                key={range}
                type="button"
                onClick={() => setActiveTimeRange(range)}
                className={`px-3 py-1 text-xs font-semibold rounded-lg transition-all cursor-pointer ${
                  activeTimeRange === range
                    ? "bg-[#c96442] text-white shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {range}
              </button>
            ))}
          </div>
        </div>

        {/* Chart Visualization */}
        <div className="h-72 sm:h-80 w-full pt-4">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={benchmarks.latency_chart.data_points}
              margin={{ top: 20, right: 20, left: -20, bottom: 0 }}
            >
              <defs>
                <linearGradient id="latencyGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#c96442" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#c96442" stopOpacity={0.0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                vertical={true}
                horizontal={true}
                stroke="var(--border)"
                opacity={0.4}
              />
              <XAxis
                dataKey="timestamp"
                tickLine={false}
                axisLine={false}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                domain={[0, 2.5]}
                ticks={[0.5, 1.0, 1.5, 2.0, 2.5]}
                tickFormatter={(v) => `${v}s`}
                tick={{ fontSize: 11, fill: "var(--muted-foreground)" }}
              />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine
                y={benchmarks.latency_chart.target_line}
                stroke="#c96442"
                strokeDasharray="4 4"
                label={{
                  value: `Target: ${benchmarks.latency_chart.target_line}s`,
                  position: "insideTopRight",
                  fill: "#c96442",
                  fontSize: 11
                }}
              />
              <Area
                type="monotone"
                dataKey="latency_ms"
                stroke="#c96442"
                strokeWidth={2.5}
                fillOpacity={1}
                fill="url(#latencyGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  )
}
