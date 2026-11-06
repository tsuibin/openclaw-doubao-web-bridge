# OpenClaw 接入豆包网页版

把 `doubao.com/chat` 的对话界面包装成一个 OpenAI 兼容接口，OpenClaw 侧只当它是一个普通
`openai-completions` provider。**不读 cookie、不伪造签名、不碰私有协议，只驱动可见 UI。**

## 实测结论（先看这个，决定要不要做）

在 Linux + Chromium 上实测到的真实情况：

| 项目 | 实测结果 |
|---|---|
| 页面是否可打开 | 可以，`https://www.doubao.com/chat/` 正常加载 |
| 输入框 | **不稳定**：有时是 `textarea`（class `textarea-YelHeN`），有时是 `contenteditable` div。**唯一稳定的是 `data-testid="chat_input_input"`**，代码按这个定位 |
| 消息节点 | `[data-testid="message-text-content"]`（每条消息一个）+ `message-block-container`，稳定 |
| 发送失败的标记 | `[data-testid="message_box_failed_icon"]` |
| 未登录能否对话 | **不能**，回车后消息带失败标记，页面不给回答 |
| 自动化高频访问 | 会被边缘节点拦：页面直接变成 `upstream connect error` / `reset reason: connection timeout` |
| 图片/文件 | 网页版是上传交互，桥接默认只传文本，多模态不生效 |
| FunctionCall | 网页版无原生工具调用，只能靠 prompt 约定 + 解析，稳定性差 |

**已验证到哪一步**：输入→回车→检测发送结果这条链路在真机跑通，未登录时会正确返回
`send_failed`。唯一没过的是**登录**——这一步只能你本人扫码，我无法代做。

**结论：管线可跑通，但只适合个人单机、低频、自己账号的辅助场景。不适合做 LoongAgent 那种多用户并发的生产集群。**

## 三条路线，按稳定性排序

### 路线 1（推荐）：官方 API，不是网页版

OpenClaw 原生自带 `volcengine` provider，直连火山方舟：

```bash
openclaw plugins install @openclaw/volcengine-provider
openclaw onboard --auth-choice volcengine-api-key
```

它和网页版不是一套东西：方舟是开发者 API，有正式配额、支持多模态与 FunctionCall、不会因为"像机器人"被封。
**如果你的目标是"用豆包模型驱动 Agent"，这里就够了，不需要桥接。**

---

### 路线 2：Playwright 持久化 profile 桥接（本目录代码）

把一个已登录的浏览器 profile 当作后端，逐条把 prompt 打进输入框、等回答稳定、取回文本。

#### 部署

```bash
cd openclaw-doubao-web-bridge
pip install fastapi uvicorn playwright
```

本机已有 Chromium：`/usr/local/bin/chromium`（可用 `CHROMIUM_PATH` 覆盖）。

#### 第一次登录（必须人工，我无法代你登录）

浏览器 profile 目录默认 `~/.doubao-web-profile`。首次要用**带界面**的方式启动，让页面显示二维码供你扫码：

```bash
HEADLESS=0 python3 bridge.py
```

然后用 noVNC / 远程桌面连到这台机器，在弹出的 Chromium 里扫码登录豆包。
登录态会写进 profile 目录，之后可以 `HEADLESS=1` 后台跑。

> 如果这台机器没有图形界面出口，也可以用 `chromium --user-data-dir=$HOME/.doubao-web-profile` 手动开一次，登录完关掉再启动桥接。

#### 起服务

```bash
HEADLESS=1 python3 bridge.py     # 默认 0.0.0.0:8799
```

#### 自检（登录是否生效就看这一步）

```bash
curl -s localhost:8799/health
curl -s -X POST localhost:8799/v1/probe    # 期望 {"ok":true,"text":"pong" 之类}
```

`/v1/probe` 返回 `send_failed`，就是没登录或登录失效。

#### 接到 OpenClaw

把 `openclaw.config.snippet.json5` 的内容合并进 `~/.openclaw/openclaw.json5`，然后：

```bash
openclaw models list          # 应能看到 doubao-web/doubao-web
openclaw "用一句话说明什么是 LoongArch"
```

#### 可调参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | 8799 | 监听端口 |
| `HEADLESS` | 1 | 0 = 带界面（首次登录用） |
| `CHROMIUM_PATH` | `/usr/local/bin/chromium` | 浏览器二进制 |
| `DOUBAO_PROFILE_DIR` | `~/.doubao-web-profile` | 登录态目录，**别提交到仓库** |
| `NEW_CHAT_EACH_REQUEST` | 1 | 每条请求开新会话，避免上下文串味 |
| `REPLY_STABLE_SECONDS` | 2.0 | 文本多久不变算"生成结束" |
| `MAX_WAIT_SECONDS` | 180 | 单轮最长等待 |

#### 已知实现坑（我踩过的，写进代码了）

1. **`fill()` 无效**：豆包输入框是 React 受控组件，`fill()` 不触发 onChange，发送键保持禁用。必须 `keyboard.type()` 逐字符输入。
2. **`click()` 报 element is not stable**：输入框会被反复重建。改成 JS `focus()` + `activeElement` 校验，失败再 `force=True` 兜底。
3. **profile 单实例锁**：同一 profile 目录不能开两个浏览器进程，报 `ProcessSingleton ... File exists`。桥接和调试脚本不能同时用同一个目录。
4. **回复提取用 `message-text-content` 的最后一个节点**，并且要和回显的用户输入做区分。

---

### 路线 3（最稳但最笨）：OpenClaw WebChat 人工中转

`openclaw dashboard` 自带 WebChat（默认 `http://127.0.0.1:18789`）。
在网页版豆包和 WebChat 之间手动复制粘贴指令。

没有任何自动化价值，但**零风控风险、零维护成本**。适合"就是想用网页版额度"的偶发场景。

---

## 风险清单（务必读）

1. **违反服务条款**：用程序驱动网页版会话属于非常规使用，账号可能被限流或封禁。
2. **协议随时变**：DOM 选择器、页面结构一改，桥接就挂。上面的选择器是 2026-11 实测，不是承诺。
3. **对你的 LoongAgent 集群不可用**：单实例串行、无并发、无配额管理、无 FunctionCall 稳定性保证。多用户并发会立刻触发限流。
4. **别把 profile 目录往外拷**：里面是你的登录态。
5. **成本对比**：网页版"免费"换来的是限流、封号、以及大量调试时间；方舟 API 按量计费但稳定可预期。**从工程角度，路 1 便宜得多。**

## 我的建议

- 做 LoongAgent 云主机：**用火山方舟 API + OpenClaw 的 volcengine provider**，别碰网页版桥接。
- 只是想"省点 API 费"、单机自己用：可以用路线 2，但要接受随时失效。
- 需要长期给别人用的服务：**不要用网页版做后端**。
