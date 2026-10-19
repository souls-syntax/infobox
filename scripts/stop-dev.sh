#!/usr/bin/env bash
set -u

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
MARKER="LOCAL_MAIL_PROJECT_ROOT=$ROOT"

is_project_process() {
  local pid="$1"
  [[ -d "/proc/$pid" ]] || return 1
  if [[ -r "/proc/$pid/environ" ]] && grep -zFqx -- "$MARKER" "/proc/$pid/environ" 2>/dev/null; then
    return 0
  fi

  # Also recognize services launched before the project marker was added.
  # Require both this checkout's working directory and a known app command.
  local cwd command
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" || return 1
  command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  if [[ "$cwd" == "$ROOT" ]]; then
    [[ "$command" == *"scripts/dev.py"* || "$command" == *"uvicorn backend.main:app"* || "$command" == *"llama-server"* ]]
  elif [[ "$cwd" == "$ROOT/frontend" ]]; then
    [[ "$command" == *"node_modules/.bin/vite"* || "$command" == *"vite --strictPort"* ]]
  else
    return 1
  fi
}

find_project_processes() {
  local envfile pid
  for envfile in /proc/[0-9]*/environ; do
    [[ -r "$envfile" ]] || continue
    pid="${envfile#/proc/}"
    pid="${pid%/environ}"
    is_project_process "$pid" && printf '%s\n' "$pid"
  done
}

mapfile -t pids < <(find_project_processes)
if ((${#pids[@]} == 0)); then
  echo "No Local Mail project processes are running."
  exit 0
fi

for pid in "${pids[@]}"; do
  is_project_process "$pid" && kill -TERM "$pid" 2>/dev/null || true
done

deadline=$((SECONDS + 5))
while ((SECONDS < deadline)); do
  remaining=0
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null && is_project_process "$pid"; then
      remaining=1
      break
    fi
  done
  ((remaining == 0)) && break
  sleep 0.1
done

stopped=0
for pid in "${pids[@]}"; do
  if kill -0 "$pid" 2>/dev/null && is_project_process "$pid"; then
    kill -KILL "$pid" 2>/dev/null || true
    ((stopped += 1))
  fi
done

if ((stopped)); then
  echo "Force-stopped $stopped tagged Local Mail process(es)."
else
  echo "Stopped Local Mail project processes."
fi
