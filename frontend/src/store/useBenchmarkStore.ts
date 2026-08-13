import { create } from "zustand"
import type { BenchmarkResponse } from "../types/api"
import { api, MOCK_BENCHMARKS } from "../lib/api"

interface BenchmarkState {
  benchmarks: BenchmarkResponse
  activeTimeRange: "24h" | "7d" | "30d"
  isLoading: boolean
  error: string | null
  setActiveTimeRange: (range: "24h" | "7d" | "30d") => void
  fetchBenchmarks: () => Promise<void>
}

export const useBenchmarkStore = create<BenchmarkState>((set) => ({
  benchmarks: MOCK_BENCHMARKS,
  activeTimeRange: "7d",
  isLoading: false,
  error: null,

  setActiveTimeRange: (activeTimeRange) => set({ activeTimeRange }),

  fetchBenchmarks: async () => {
    set({ isLoading: true, error: null })
    try {
      const data = await api.getBenchmarks()
      set({ benchmarks: data, isLoading: false })
    } catch (err: any) {
      set({ error: err.message, isLoading: false })
    }
  }
}))
