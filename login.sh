#!/usr/bin/env bash
# 首次登录 / 登录失效后重新扫码。
# 必须在**有图形界面的终端**里运行（本机是 RDP/本地桌面，别在 ssh 里跑）：
#   cd ~/openclaw-doubao-bridge && ./login.sh
# 会先停掉后台桥接（同一 profile 不能开两个浏览器），扫码登录后自动恢复服务并自检。
set -euo pipefail
cd "$(dirname "$0")"

export CHROMIUM_PATH="${CHROMIUM_PATH:-/usr/bin/chromium}"
PROFILE="${DOUBAO_PROFILE_DIR:-$HOME/.doubao-web-profile}"

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "!! 没有可用图形会话（DISPLAY / WAYLAND_DISPLAY 都为空），请在本机桌面终端里再运行一次" >&2
    exit 1
fi

systemctl --user stop doubao-bridge.service || true
# unit 若因 TimeoutStopSec 超时被 SIGKILL，浏览器来不及自己删单实例锁，
# 下一次 Chromium 会以 "Failed to create .../SingletonLock" 直接 abort、窗口不弹。
# SingletonLock 是指向 "hostname-pid" 的符号链接：pid 还活着说明真有人占着，别动；
# 否则就是残留锁，清掉。
lock="$PROFILE/SingletonLock"
lock_pid=""
if [ -L "$lock" ]; then
    lock_pid="$(readlink "$lock")"
    lock_pid="${lock_pid##*-}"
fi
if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "!! profile 仍被 pid $lock_pid 占用，跳过锁清理（服务没停干净？）" >&2
else
    rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket"
fi
echo ">>> profile: $PROFILE"
echo ">>> 注意：要在这个新开的 Chromium 窗口里扫码登录（不是你平时的浏览器），"
echo ">>>       等右上角出现头像/昵称后再关闭窗口。"
# 必须和桥接用同一个 cookie 加密后端：桥接跑在 systemd user service 里，拿不到
# 桌面 keyring，Chromium 会退回 basic（cookie 前缀 v10）；而桌面终端里的 Chromium
# 默认用 keyring（v11）。两边不一致时，登录窗口写下的 cookie 桥接读不出来，
# 豆包会立刻把会话降级成匿名。统一钉成 basic。
"$CHROMIUM_PATH" --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    --password-store=basic https://www.doubao.com/chat/ || true

echo ">>> 检查 profile 里的登录态"
python3 - "$PROFILE" <<'PY' || true
import sqlite3, shutil, sys, datetime, os
db = os.path.join(sys.argv[1], "Default", "Cookies")
if not os.path.exists(db):
    print("   !! 没有 Cookies 文件"); raise SystemExit
shutil.copy2(db, "/tmp/.doubao_ck_check.db")
rows = sqlite3.connect("/tmp/.doubao_ck_check.db").execute(
    "select expires_utc from cookies where host_key like '%doubao%' and name='sessionid'"
).fetchall()
if not rows:
    print("   !! profile 里没有 doubao sessionid —— 登录没写进这个 profile"); raise SystemExit
exp = max(r[0] for r in rows)
dt = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=exp)
now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
days = (dt - now).total_seconds() / 86400
print(f"   sessionid 有效期到 {dt:%Y-%m-%d %H:%M} UTC（约 {days:.0f} 天）")
if days < 3:
    print("   !! 仍是匿名/短期 session，多半没在这个窗口登录成功")
else:
    print("   ok: 看起来是正式登录态")
PY

echo ">>> 启动桥接服务"
systemctl --user start doubao-bridge.service
for i in $(seq 1 20); do
    sleep 2
    if curl -sS -m 3 http://127.0.0.1:8799/health >/dev/null 2>&1; then break; fi
done
echo ">>> 自检 /v1/probe（ok=true 即登录生效）"
OUT=$(curl -sS -m 200 -X POST http://127.0.0.1:8799/v1/probe || true)
echo "$OUT"
case "$OUT" in
  *'"ok":true'*)
      echo ">>> 登录生效。可以把 OpenClaw 默认模型切过去："
      echo "    openclaw config set agents.defaults.model.primary doubao-web/doubao-web" ;;
  *)
      echo ">>> 登录仍未生效。确认是在上面那个 Chromium 窗口里扫码的，而不是平时的浏览器；" 
      echo "    如果窗口没弹出来，说明当前终端没有图形会话。" ;;
esac
