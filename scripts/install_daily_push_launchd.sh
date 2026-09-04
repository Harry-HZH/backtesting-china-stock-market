#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
template="$project_dir/scripts/com.mhxy-agent.daily-push.plist.template"
launch_agents="$HOME/Library/LaunchAgents"
plist="$launch_agents/com.mhxy-agent.daily-push.plist"
mkdir -p "$launch_agents"
sed "s#__PROJECT_DIR__#$project_dir#g" "$template" > "$plist"

launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl enable "gui/$(id -u)/com.mhxy-agent.daily-push"
echo "已安装：工作日 16:30（Asia/Shanghai）执行"
echo "查看状态：launchctl print gui/$(id -u)/com.mhxy-agent.daily-push"
echo "立即试跑：$project_dir/scripts/run_daily_capital_pool_push.sh"
