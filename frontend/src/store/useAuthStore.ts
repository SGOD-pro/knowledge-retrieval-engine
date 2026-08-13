import { create } from "zustand"
import type { User, LoginRequest } from "../types/api"
import { api } from "../lib/api"

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  isLoading: boolean
  error: string | null
  rememberMe: boolean
  setRememberMe: (val: boolean) => void
  login: (credentials: LoginRequest) => Promise<boolean>
  logout: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  user: {
    id: "usr_54321",
    email: "alexandra.chen@enterprise.com",
    name: "Alexandra Chen",
    role: "Senior Designer",
    avatar: "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=128&auto=format&fit=crop&q=80"
  },
  token: localStorage.getItem("kre_token") || "mock_valid_token",
  isAuthenticated: true,
  isLoading: false,
  error: null,
  rememberMe: true,

  setRememberMe: (val) => set({ rememberMe: val }),

  login: async (credentials) => {
    set({ isLoading: true, error: null })
    try {
      const res = await api.login(credentials)
      if (res.access_token) {
        localStorage.setItem("kre_token", res.access_token)
        set({
          user: res.user,
          token: res.access_token,
          isAuthenticated: true,
          isLoading: false
        })
        return true
      }
      return false
    } catch (err: any) {
      set({ error: err.message || "Failed to login", isLoading: false })
      return false
    }
  },

  logout: () => {
    localStorage.removeItem("kre_token")
    set({ user: null, token: null, isAuthenticated: false })
  }
}))
