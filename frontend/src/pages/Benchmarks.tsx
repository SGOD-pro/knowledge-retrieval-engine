import { Activity, Zap, ShieldAlert, BrainCircuit, AlertTriangle } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart"
import { XAxis, YAxis, CartesianGrid, ResponsiveContainer, Area, AreaChart } from "recharts"
import { Badge } from "@/components/ui/badge"

const data = {
  p95_latency_ms: 14616.6,
  recall_5: 0.033,
  faithfulness: 0.0,
  llm_activation_rate: 0.883
}

// Mock time-series data for the latency chart
const latencyData = [
  { time: "00:00", latency: 2400 },
  { time: "04:00", latency: 1398 },
  { time: "08:00", latency: 14616.6 },
  { time: "12:00", latency: 3908 },
  { time: "16:00", latency: 4800 },
  { time: "20:00", latency: 3800 },
  { time: "24:00", latency: 4300 },
]

export function Benchmarks() {
  return (
    <div className="p-6 h-full flex flex-col gap-6 max-w-6xl mx-auto w-full">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight text-primary">System Benchmarks</h2>
          <p className="text-muted-foreground">Current performance metrics from benchmark_results.json</p>
        </div>
        <Badge variant="destructive" className="uppercase tracking-widest text-xs py-1">
          <AlertTriangle className="mr-2 h-3 w-3" />
          Failing Targets
        </Badge>
      </div>

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card className="border-destructive/50 bg-destructive/5">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium text-destructive">p95 Latency</CardTitle>
            <Zap className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{data.p95_latency_ms}ms</div>
            <p className="text-xs text-destructive/80 mt-1">
              Target: &lt; 4000ms
            </p>
          </CardContent>
        </Card>
        <Card className="border-destructive/50 bg-destructive/5">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium text-destructive">Recall@5</CardTitle>
            <Activity className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{data.recall_5}</div>
            <p className="text-xs text-destructive/80 mt-1">
              Target: &gt; 0.85
            </p>
          </CardContent>
        </Card>
        <Card className="border-destructive/50 bg-destructive/5">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium text-destructive">Faithfulness</CardTitle>
            <ShieldAlert className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{data.faithfulness}</div>
            <p className="text-xs text-destructive/80 mt-1">
              Target: 1.0 (Zero Hallucination)
            </p>
          </CardContent>
        </Card>
        <Card className="border-destructive/50 bg-destructive/5">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium text-destructive">LLM Activation</CardTitle>
            <BrainCircuit className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{(data.llm_activation_rate * 100).toFixed(1)}%</div>
            <p className="text-xs text-destructive/80 mt-1">
              Target: &lt; 60%
            </p>
          </CardContent>
        </Card>
      </div>

      <Card className="flex-1 min-h-[400px] flex flex-col">
        <CardHeader>
          <CardTitle>Latency over Time</CardTitle>
          <CardDescription>Mock visualization of p95 latency spikes over 24 hours.</CardDescription>
        </CardHeader>
        <CardContent className="flex-1 pb-4">
          <ChartContainer config={{ latency: { label: "Latency (ms)", color: "var(--primary)" } }} className="h-full w-full min-h-[300px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={latencyData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorLatency" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="var(--primary)" stopOpacity={0.3}/>
                    <stop offset="95%" stopColor="var(--primary)" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="var(--border)" />
                <XAxis dataKey="time" axisLine={false} tickLine={false} tickMargin={10} stroke="var(--muted-foreground)" fontSize={12} />
                <YAxis axisLine={false} tickLine={false} tickMargin={10} stroke="var(--muted-foreground)" fontSize={12} />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Area type="monotone" dataKey="latency" stroke="var(--primary)" strokeWidth={2} fillOpacity={1} fill="url(#colorLatency)" />
              </AreaChart>
            </ResponsiveContainer>
          </ChartContainer>
        </CardContent>
      </Card>
    </div>
  )
}
