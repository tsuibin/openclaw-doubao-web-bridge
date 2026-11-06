# 本机部署记录（loongarch64 / AOSC OS 14）

部署位置 `~/openclaw-doubao-bridge`，systemd user 单元 `doubao-bridge.service`（已 enable，开机自启），
监听 `0.0.0.0:8799`。

## 日常操作

```bash
systemctl --user status doubao-bridge.service
systemctl --user restart doubao-bridge.service
journalctl --user -u doubao-bridge.service -f
curl -s localhost:8799/health
curl -s -X POST localhost:8799/v1/probe     # ok=true 才代表登录有效
```

## 首次登录（只有你能做）

带界面登录必须人工扫码，且桥接服务和浏览器不能同时占用同一个 profile：

```bash
cd ~/openclaw-doubao-bridge && ./login.sh
```

**必须在本机桌面/RDP 的终端里跑**（要有 DISPLAY），脚本会先停服务、开 Chromium 扫码、
关窗后自动拉起服务并自检。不要用 root、不要在纯 ssh 里跑（本机 `:0` 是 sddm 的 seat，
没有授权，会报 `Authorization required`）。

## 为什么不能直接 pip install -r requirements.txt

本机是 loongarch64，PyPI 上 playwright 只有 x86_64 / arm64 / macos / win 的 wheel，**没有 sdist**，
官方不支持该架构。本地的落地方式：

1. `pip download playwright --platform ...` 不可用 → 直接从 tuna 镜像取
   `playwright-1.64.1-py3-none-manylinux1_x86_64.whl`，解压进 `.venv/lib/python3.14/site-packages`。
   （wheel 里的 Python 代码和 driver 的 JS 与架构无关，只有 `driver/node` 是 x86_64 ELF。）
2. 用系统 node 顶掉自带的 x86_64 node：`PLAYWRIGHT_NODEJS_PATH=$(command -v node)`，已写进 `start.sh`。
3. 浏览器不用 playwright 自带的下载版，直接指定系统 Chromium：
   `CHROMIUM_PATH=/usr/bin/chromium`（系统里是 Chromium 148，实测 CDP 兼容）。
4. `greenlet`（playwright 的硬依赖）源码编译在 loongarch64 上失败
   （`switch_loongarch64_linux.h` 里 `$r22 cannot be used in 'asm' here`），
   改用发行版自带的 `/usr/lib/python3.14/site-packages/greenlet`（3.5.4）：
   把 `.venv/pyvenv.cfg` 的 `include-system-site-packages` 改成 `true`。
5. `pydantic-core` 会在本地从源码编译（AOSC 有 gcc/rustc），首次安装慢属正常。

## 对 bridge.py 的本地改动（1 处）

`_focus_input`：空会话/引导页时 `[data-testid="chat_input_input"]` 是**外层容器 div**，
真正的可编辑节点是它内部的 `textarea`（`textarea-YelHeN`）或 `contenteditable` div。
原实现直接对 `document.querySelector` 命中的节点 `focus()`，命中外层 div 时永远失败，
表现为 probe 卡满 `MAX_WAIT_SECONDS` 后返回 `timeout`。现在改为：取最后一个可见容器，
下钻到内部的 `textarea / [contenteditable=true]` 再聚焦，兜底 force click 内层节点。

## 已知限制

- 网页版未登录时代理一律返回 `send_failed`（502），这是预期行为。
- 单实例串行、无并发；高频调用会触发豆包边缘节点限流（`upstream connect error`）。
- 无原生 FunctionCall，工具调用靠 prompt 约定 + `<tool_call>` 解析。

## 对 bridge.py 的第二处本地改动

`chat()` 里原来用 `keyboard.type(prompt, delay=20)` 逐字符打字。OpenClaw 的 prompt 带系统提示词与
工具 schema，动辄几千字，逐字符输入要几分钟，provider 侧直接超时（实测 34s 就 abort）。
现改为 `keyboard.insert_text()` 一次性插入（同样会触发 React 的 input 事件），插入后读回输入框内容校验，
为空才退回逐字符 type。实测一个完整 agent turn 的请求从超时降到 ~77s（未登录时返回 502 send_failed）。

## OpenClaw 侧（2026.11.1，npm 全局装好）

- CLI：`~/.npm-global/bin/openclaw`，配置：`~/.openclaw/openclaw.json`
- 已写入 provider `doubao-web`：`baseUrl http://127.0.0.1:8799/v1`、`api openai-completions`、
  model id `doubao-web`，`openclaw models list` 可见 `doubao-web/doubao-web`。
- 注意：上游 snippet 里的 `agents.defaults.tools.deny` 在 2026.11.1 已不合 schema（工具白名单是全局
  `tools.deny`），所以没写进去；要收紧就用 `openclaw config set tools.deny '["exec","shell","write","delete"]'`。
- 默认模型仍是你原来的，**扫码登录、`/v1/probe` 返回 ok=true 之后**再切：
  `openclaw config set agents.defaults.model.primary doubao-web/doubao-web`
- 实测链路（未登录，属预期失败）：`openclaw agent --local --model doubao-web/doubao-web -m "hi"`
  → 桥接 502 `send_failed`。
