#!/usr/bin/env bash
set -e

# ANSI Color Codes
CYAN="\033[1;36m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"
RED="\033[1;31m"
RESET="\033[0m"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"

# Helper to kill any existing process occupying a target port
kill_port() {
    local port="$1"
    local name="$2"
    if command -v fuser >/dev/null 2>&1; then
        local pids
        pids=$(fuser "${port}/tcp" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo -e "${YELLOW}[KRE]${RESET} Freeing port ${port} (${name})..."
            fuser -k -9 "${port}/tcp" >/dev/null 2>&1 || true
            sleep 0.3
        fi
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo -e "${YELLOW}[KRE]${RESET} Freeing port ${port} (${name})..."
            echo "$pids" | xargs -r kill -9 2>/dev/null || true
            sleep 0.3
        fi
    fi
}

# Cleanup function to cleanly stop all processes on Ctrl+C (SIGINT) / SIGTERM / EXIT
cleanup() {
    echo ""
    echo -e "${YELLOW}[KRE]${RESET} Shutting down servers..."
    if [ -n "${BACKEND_PID:-}" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo -e "${YELLOW}[KRE]${RESET} Stopping backend server (PID: $BACKEND_PID)..."
        kill -TERM "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "${FRONTEND_PID:-}" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo -e "${YELLOW}[KRE]${RESET} Stopping frontend server (PID: $FRONTEND_PID)..."
        kill -TERM "$FRONTEND_PID" 2>/dev/null || true
    fi
    # Also kill child background jobs if any remain
    jobs -p | xargs -r kill 2>/dev/null || true
    wait 2>/dev/null || true
    echo -e "${YELLOW}[KRE]${RESET} All servers stopped."
}

trap cleanup INT TERM EXIT

echo -e "${YELLOW}[KRE]${RESET} Starting Knowledge Retrieval Engine..."

# Ensure ports 8001 (Backend) and 5173 (Frontend) are free before starting
kill_port 8001 "Backend"
kill_port 5173 "Frontend"

# Prefix helper functions (line-buffered real-time streaming)
prefix_backend() {
    while IFS= read -r line || [ -n "$line" ]; do
        echo -e "${CYAN}[BACKEND]${RESET} $line"
    done
}

prefix_frontend() {
    while IFS= read -r line || [ -n "$line" ]; do
        echo -e "${GREEN}[FRONTEND]${RESET} $line"
    done
}

# Start Backend on http://localhost:8001
echo -e "${CYAN}[BACKEND]${RESET} Starting on http://localhost:8001..."
(
    cd "$BACKEND_DIR"
    export PYTHONPATH=src
    export PYTHONUNBUFFERED=1
    uv run uvicorn src.main:app --port 8001 --reload 2>&1 | prefix_backend
) &
BACKEND_PID=$!

# Start Frontend on http://localhost:5173
echo -e "${GREEN}[FRONTEND]${RESET} Starting on http://localhost:5173..."
(
    cd "$FRONTEND_DIR"
    npm run dev 2>&1 | prefix_frontend
) &
FRONTEND_PID=$!

echo -e "${YELLOW}[KRE]${RESET} Servers running. Press ${RED}Ctrl+C${RESET} to stop both."

# Wait for background processes
wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true