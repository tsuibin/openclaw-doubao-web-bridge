#!/usr/bin/env bash
# 启动豆包网页版桥接（OpenClaw 的 openai-completions provider）
set -euo pipefail
cd "$(dirname "$0")"

export CHROMIUM_PATH="${CHROMIUM_PATH:-/usr/bin/chromium}"
# 本机是 loongarch64：playwright 官方没有对应轮子，这里用 x86_64 wheel 里
# 与架构无关的 driver JS，配合系统 node 运行（详见 LOONGARCH-NOTES.md）
export PLAYWRIGHT_NODEJS_PATH="${PLAYWRIGHT_NODEJS_PATH:-$(command -v node)}"
export HEADLESS="${HEADLESS:-1}"
export PORT="${PORT:-8799}"

exec .venv/bin/python bridge.py
