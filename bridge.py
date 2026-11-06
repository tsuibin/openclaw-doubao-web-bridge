"""
豆包网页版 → OpenAI 兼容桥接（给 OpenClaw 用）

原理：用一个已登录豆包账号的持久化浏览器 profile，把 doubao.com/chat 的
对话界面当作"模型后端"，对外暴露 /v1/chat/completions，OpenClaw 侧只当它是
一个 openai-completions 类型的 provider。

只做界面层驱动：不读 cookie、不伪造签名、不碰私有协议。
"""

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from playwright.async_api import async_playwright

CONFIG = {
    # 浏览器
    "chromium": os.environ.get("CHROMIUM_PATH", "/usr/local/bin/chromium"),
    "profile_dir": os.environ.get("DOUBAO_PROFILE_DIR", os.path.expanduser("~/.doubao-web-profile")),
    "headless": os.environ.get("HEADLESS", "1") == "1",
    "doubao_url": os.environ.get("DOUBAO_URL", "https://www.doubao.com/chat/"),
    # 行为
    "new_chat_each_request": os.environ.get("NEW_CHAT_EACH_REQUEST", "1") == "1",
    "reply_stable_seconds": float(os.environ.get("REPLY_STABLE_SECONDS", "2.0")),
    "max_wait_seconds": float(os.environ.get("MAX_WAIT_SECONDS", "180")),
    "lang": os.environ.get("BRIDGE_LANG", "zh"),
}

# 已观测到的真实节点（2026-11 实测）
SEL_INPUT = '[data-testid="chat_input_input"]'            # 输入框：可能是 textarea，也可能是 contenteditable div
SEL_NEW_CHAT = '[data-testid="create_conversation_button"]'
SEL_MSG_TEXT = '[data-testid="message-text-content"]'   # 每条消息的纯文本
SEL_MSG_FAILED = '[data-testid="message_box_failed_icon"]'


class DoubaoSession:
    """单实例、串行化：网页 UI 天然不能并发，用锁排队。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pw = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()

    async def start(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.cfg["profile_dir"],
            executable_path=self.cfg["chromium"],
            headless=self.cfg["headless"],
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1440, "height": 900},
        )
        self._page = self._browser.pages[0] if self._browser.pages else await self._browser.new_page()
        await self._page.goto(self.cfg["doubao_url"], timeout=60000)
        await self._page.wait_for_selector(SEL_INPUT, timeout=60000)

    async def _ensure_input(self):
        for _ in range(3):
            try:
                await self._page.wait_for_selector(SEL_INPUT, state="visible", timeout=15000)
                return
            except Exception:
                await self._page.goto(self.cfg["doubao_url"], timeout=60000)

    async def _focus_input(self):
        """用 JS 聚焦，避开 click 的 stability 检查（豆包输入框会被反复重建）。"""
        for i in range(10):
            ok = await self._page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    el.focus();
                    return document.activeElement === el;
                }""",
                SEL_INPUT,
            )
            if ok:
                await self._page.wait_for_timeout(200)
                return
            # 兜底：force click
            try:
                await self._page.locator(SEL_INPUT).first.click(force=True, timeout=3000)
                await self._page.wait_for_timeout(300)
                return
            except Exception:
                await self._page.wait_for_timeout(500)
        body = await self._page.inner_text("body")
        raise RuntimeError(f"无法聚焦输入框；页面文本片段：{body[:200]!r}")

    async def _new_chat(self):
        try:
            btn = self._page.locator(SEL_NEW_CHAT).first
            if await btn.count() > 0:
                await btn.click(timeout=5000)
                await self._page.wait_for_timeout(800)
        except Exception:
            await self._page.goto(self.cfg["doubao_url"], timeout=60000)

    async def _read_last_assistant(self) -> str:
        """取最后一条非"回显用户输入"的消息文本。"""
        texts = await self._page.eval_on_selector_all(
            SEL_MSG_TEXT, "els => els.map(e => e.innerText || '')"
        )
        return texts[-1].strip() if texts else ""

    async def chat(self, prompt: str) -> Dict[str, Any]:
        async with self._lock:
            # 上游边缘节点限流/异常时，页面会直接渲染错误文案而不是输入框
            page_text = await self._page.inner_text("body")
            if "upstream connect error" in page_text or "reset reason" in page_text:
                return {
                    "ok": False,
                    "error": "upstream_edge_error",
                    "detail": "豆包边缘节点返回 upstream connect error（多为自动化访问触发的限流）。请降低频率、换 IP 或改用官方 API。",
                }

            await self._ensure_input()
            if self.cfg["new_chat_each_request"]:
                await self._new_chat()

            await self._focus_input()
            await self._page.keyboard.press("Control+A")
            await self._page.keyboard.press("Backspace")
            # 必须走键盘输入，fill() 不会触发 React 的 onChange，发送按钮保持禁用
            await self._page.keyboard.type(prompt, delay=20)
            await self._page.wait_for_timeout(300)

            n_before = await self._page.locator(SEL_MSG_TEXT).count()
            await self._page.keyboard.press("Enter")

            deadline = time.time() + self.cfg["max_wait_seconds"]
            last, stable_since = "", time.time()
            got_reply = False

            while time.time() < deadline:
                await self._page.wait_for_timeout(600)

                if await self._page.locator(SEL_MSG_FAILED).count() > 0:
                    return {
                        "ok": False,
                        "error": "send_failed",
                        "detail": "豆包网页版未登录或会话失效（页面出现发送失败标记）。请先在桥接浏览器里完成登录。",
                    }

                cur = await self._read_last_assistant()
                n_now = await self._page.locator(SEL_MSG_TEXT).count()
                if n_now > n_before and cur and cur != prompt:
                    got_reply = True
                    if cur != last:
                        last, stable_since = cur, time.time()
                    elif time.time() - stable_since >= self.cfg["reply_stable_seconds"]:
                        return {"ok": True, "text": cur}

            if got_reply and last:
                return {"ok": True, "text": last, "truncated": True}
            return {"ok": False, "error": "timeout", "detail": "等待回复超时，未取到助手消息。"}

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass


session: Optional[DoubaoSession] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session
    session = DoubaoSession(CONFIG)
    await session.start()
    yield
    await session.close()


app = FastAPI(lifespan=lifespan, title="doubao-web-bridge")


# ---------- OpenAI 消息 → 单条文本 ----------

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)

TOOL_PROTOCOL = """你可以调用以下工具。需要调用工具时，只输出一行 JSON，并用 <tool_call></tool_call> 包裹，不要输出其他内容：
<tool_call>{{"name":"工具名","arguments":{{...}}}}</tool_call>
可用工具：
{tools}
若无需调用工具，直接用自然语言回答。"""


def flatten(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]) -> str:
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):  # 多模态数组，取文本片段
            content = "\n".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        content = (content or "").strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[系统指令]\n{content}")
        elif role == "assistant":
            parts.append(f"[助手]\n{content}")
        elif role == "tool":
            parts.append(f"[工具结果 {m.get('name') or m.get('tool_call_id') or ''}]\n{content}")
        else:
            parts.append(f"[用户]\n{content}")
    body = "\n\n".join(parts)

    if tools:
        schema = json.dumps(tools, ensure_ascii=False, indent=1)
        body = TOOL_PROTOCOL.format(tools=schema) + "\n\n---\n\n" + body
    return body


def to_openai(reply: str, model: str, has_tools: bool) -> Dict[str, Any]:
    base = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
    }

    if has_tools:
        m = TOOL_CALL_RE.search(reply)
        if m:
            try:
                call = json.loads(m.group(1))
                return {
                    **base,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "call_" + uuid.uuid4().hex[:20],
                                "type": "function",
                                "function": {
                                    "name": call.get("name", ""),
                                    "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            except json.JSONDecodeError:
                pass

    return {
        **base,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
async def health():
    return {"ok": True, "engine": "doubao-web", "logged_in_check": "call /v1/probe"}


@app.post("/v1/probe")
async def probe():
    try:
        r = await session.chat("你好，请只回复：pong")
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": "driver_error", "detail": str(e)})
    return r if r.get("ok") else JSONResponse(status_code=502, content=r)


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [
        {"id": n, "object": "model", "owned_by": "doubao-web"}
        for n in ("doubao-web", "doubao-web-chat")
    ]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "doubao-web")
    tools = body.get("tools")
    prompt = flatten(body.get("messages", []), tools)
    if not prompt:
        return JSONResponse(status_code=400, content={"error": {"message": "empty messages"}})

    try:
        result = await session.chat(prompt)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": {"message": f"driver_error: {e}", "type": "bridge_error"}})

    if not result.get("ok"):
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"{result.get('error')}: {result.get('detail','')}", "type": "upstream_error"}},
        )

    payload = to_openai(result["text"], model, bool(tools))

    if body.get("stream"):
        async def gen():
            ch = payload["choices"][0]
            delta = ch["message"].get("content") or ""
            chunk = {"id": payload["id"], "object": "chat.completion.chunk",
                     "created": payload["created"], "model": model,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": delta},
                                  "finish_reason": None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'id': payload['id'], 'object': 'chat.completion.chunk', 'created': payload['created'], 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': ch['finish_reason']}]}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse(payload)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8799")))
