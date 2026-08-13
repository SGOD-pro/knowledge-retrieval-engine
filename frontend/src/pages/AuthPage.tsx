import React, { useState } from "react"
import { useNavigate } from "react-router-dom"
import { Mail, Lock, ArrowRight, Loader2 } from "lucide-react"
import { useAuthStore } from "../store/useAuthStore"
import { Input } from "../components/ui/input"
import { Button } from "../components/ui/button"
import { Checkbox } from "../components/ui/checkbox"
import { toast } from "sonner"
import { ThemeToggle } from "../components/common/ThemeToggle"

export function AuthPage() {
  const navigate = useNavigate()
  const { login, rememberMe, setRememberMe, isLoading } = useAuthStore()
  const [email, setEmail] = useState("alexandra.chen@enterprise.com")
  const [password, setPassword] = useState("••••••••••••")

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email || !password) {
      toast.error("Please provide your email and password")
      return
    }

    const success = await login({ email, password, remember_me: rememberMe })
    if (success) {
      toast.success("Welcome back to KRE!")
      navigate("/workspaces")
    } else {
      toast.error("Authentication failed. Please check your credentials.")
    }
  }

  const handleOAuthLogin = (provider: "google" | "microsoft") => {
    toast.info(`Connecting to ${provider === "google" ? "Google" : "Microsoft"} SSO...`)
    setTimeout(async () => {
      await login({ email: `user@${provider}.com`, password: "sso" })
      toast.success(`Signed in with ${provider === "google" ? "Google" : "Microsoft"}`)
      navigate("/workspaces")
    }, 600)
  }

  return (
    <div className="min-h-screen w-full bg-background flex flex-col items-center justify-center p-4 relative">
      {/* Theme toggle in top right */}
      <div className="absolute top-6 right-6">
        <ThemeToggle />
      </div>

      {/* Main Auth Card */}
      <div className="w-full max-w-[480px] bg-card rounded-2xl border border-border/80 shadow-sm overflow-hidden relative">
        {/* Top Terracotta Accent Line */}
        <div className="h-1.5 w-full bg-[#c96442]" />

        <div className="p-8 sm:p-10">
          {/* Header */}
          <div className="text-center space-y-1.5 mb-8">
            <div className="text-[#c96442] font-headline font-semibold text-xs tracking-wider">
              KRE
            </div>
            <div className="text-[11px] font-semibold text-muted-foreground tracking-[0.15em] uppercase">
              Knowledge Retrieval Engine
            </div>
            <h1 className="font-headline text-3xl sm:text-4xl font-bold text-[#c96442] pt-4">
              Sign In
            </h1>
            <p className="text-sm text-muted-foreground font-sans">
              Access your analytical workspace
            </p>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-2 text-left">
              <label className="text-xs font-semibold text-foreground/90">
                Email Address
              </label>
              <div className="relative">
                <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="name@organization.com"
                  className="pl-10 h-11 bg-card border-input/80 rounded-lg text-sm text-foreground placeholder:text-muted-foreground/70 focus-visible:ring-[#c96442]"
                  required
                />
              </div>
            </div>

            <div className="space-y-2 text-left">
              <label className="text-xs font-semibold text-foreground/90">
                Password
              </label>
              <div className="relative">
                <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="pl-10 h-11 bg-card border-input/80 rounded-lg text-sm text-foreground placeholder:text-muted-foreground/70 focus-visible:ring-[#c96442]"
                  required
                />
              </div>
            </div>

            <div className="flex items-center justify-between pt-1">
              <div className="flex items-center space-x-2">
                <Checkbox
                  id="remember"
                  checked={rememberMe}
                  onCheckedChange={(c) => setRememberMe(!!c)}
                  className="border-[#c96442] data-[state=checked]:bg-[#c96442]"
                />
                <label
                  htmlFor="remember"
                  className="text-xs font-medium text-muted-foreground cursor-pointer select-none"
                >
                  Remember me
                </label>
              </div>
              <button
                type="button"
                onClick={() => toast.info("Password reset instructions sent to email")}
                className="text-xs font-medium text-[#c96442] hover:underline"
              >
                Forgot password?
              </button>
            </div>

            {/* Sign In Button */}
            <Button
              type="submit"
              disabled={isLoading}
              className="w-full h-11 bg-[#c96442] hover:bg-[#b05730] text-white font-medium rounded-lg text-sm transition-all shadow-xs flex items-center justify-center gap-2 mt-2"
            >
              {isLoading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <>
                  <span>Sign In</span>
                  <ArrowRight className="h-4 w-4" />
                </>
              )}
            </Button>
          </form>

          {/* Social Logins */}
          <div className="relative my-7">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-border/80" />
            </div>
            <div className="relative flex justify-center text-[10px] uppercase">
              <span className="bg-card px-3 text-muted-foreground font-semibold tracking-wider">
                Or continue with
              </span>
            </div>
          </div>

          <div className="space-y-3">
            <Button
              type="button"
              variant="outline"
              onClick={() => handleOAuthLogin("google")}
              className="w-full h-11 bg-card hover:bg-accent/60 border-border/80 text-foreground font-medium rounded-lg text-xs flex items-center justify-center gap-2.5 transition-colors"
            >
              <svg className="h-4 w-4" viewBox="0 0 24 24">
                <path
                  fill="#4285F4"
                  d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
                />
                <path
                  fill="#34A853"
                  d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                />
                <path
                  fill="#FBBC05"
                  d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"
                />
                <path
                  fill="#EA4335"
                  d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"
                />
              </svg>
              <span>Google</span>
            </Button>

            <Button
              type="button"
              variant="outline"
              onClick={() => handleOAuthLogin("microsoft")}
              className="w-full h-11 bg-card hover:bg-accent/60 border-border/80 text-foreground font-medium rounded-lg text-xs flex items-center justify-center gap-2.5 transition-colors"
            >
              <svg className="h-4 w-4" viewBox="0 0 21 21">
                <rect x="1" y="1" width="9" height="9" fill="#F25022" />
                <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
                <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
                <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
              </svg>
              <span>Microsoft</span>
            </Button>
          </div>

          {/* Footer */}
          <div className="mt-8 text-center text-xs text-muted-foreground">
            Don't have an account?{" "}
            <button
              type="button"
              onClick={() => toast.info("Enterprise invite requested. Check your inbox.")}
              className="text-[#c96442] font-semibold hover:underline"
            >
              Request Access
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
