"""Account screens and Chainlit authentication boundary integration."""

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
import json
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

actor = ContextVar("history_actor", default=None)


def current_actor():
    if actor.get():
        return actor.get()
    from chainlit.context import context_var

    try:
        user = context_var.get().session.user
        return user.identifier if user else None
    except (LookupError, AttributeError):
        return None


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>账号管理 · 中文数据助手</title>
<style>body{font:16px system-ui;max-width:560px;margin:48px auto;padding:24px;background:#f5f7fb;color:#172033}section{background:white;padding:24px;margin:20px 0;border-radius:12px}input,button{box-sizing:border-box;width:100%;padding:12px;margin:6px 0}button{background:#234ed8;color:white;border:0;border-radius:6px;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#234ed8}</style>
<h1>账号管理</h1><a href="/login">返回登录</a>
<section><h2>注册</h2><p>注册后须由管理员分配数据源。密码至少 12 个字符。恢复码仅显示一次，请保存到密码管理器。</p>
<form data-action="register"><input name="username" autocomplete="username" placeholder="用户名" required maxlength="64"><input name="password" type="password" autocomplete="new-password" placeholder="密码" minlength="12" maxlength="128" required><button>创建账号</button></form></section>
<section><h2>恢复账号</h2><p>使用注册时保存的恢复码重置密码。成功后旧恢复码和所有旧登录均失效，请保存新的恢复码。</p>
<form data-action="recover"><input name="username" autocomplete="username" placeholder="用户名" required maxlength="64"><input name="recovery" type="password" placeholder="一次性恢复码" required maxlength="128"><input name="password" type="password" autocomplete="new-password" placeholder="新密码" minlength="12" maxlength="128" required><button>重置密码</button></form></section>
<section><h2>已登录账号</h2><p id="me">尚未登录</p><form data-action="rotate"><input name="password" type="password" autocomplete="current-password" placeholder="当前密码" maxlength="128" required><input name="new_password" type="password" autocomplete="new-password" placeholder="新密码（至少 12 位）" minlength="12" maxlength="128" required><button>修改密码并更新恢复码</button></form><button id="revoke">退出所有设备</button></section>
<section role="status"><pre id="status">恢复码丢失且无法登录时，请联系管理员。系统不发送恢复邮件。</pre></section>
<script src="/accounts/page.js" defer></script></html>"""

SCRIPT = """const out=document.getElementById('status');
async function act(action,data){try{const r=await fetch('/accounts/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const v=await r.json();out.textContent=r.ok?(v.recovery?'请立即保存新的恢复码（仅显示一次）：\\n'+v.recovery+'\\n完成后重新登录。':v.message):v.detail; if(r.ok){document.querySelectorAll('input[type=password]').forEach(x=>x.value='');}}catch(e){out.textContent='请求失败，请稍后重试。';}}
document.querySelectorAll('form').forEach(f=>f.onsubmit=e=>{e.preventDefault();act(f.dataset.action,Object.fromEntries(new FormData(f)));});
document.getElementById('revoke').onclick=()=>act('revoke',{});
fetch('/accounts/me').then(r=>r.ok?r.json():null).then(v=>{if(v)document.getElementById('me').textContent=v.username+' · 可用配置：'+(v.profiles.join(', ')||'等待管理员授权');});"""


def install_accounts(get_store):
    from chainlit import auth, server, socket
    from chainlit.auth.cookie import get_token_from_cookies
    import chainlit.auth.jwt as jwt_module
    from chainlit.session import WebsocketSession

    if getattr(server.app.state, "accounts_installed", False):
        return
    server.app.state.accounts_installed = True
    original_decode = auth.decode_jwt

    def decode(token):
        user = original_decode(token)
        get_store().validate(user)
        return user

    # Chainlit HTTP dependencies, JWT exchange, and socket login all share this check.
    auth.decode_jwt = server.decode_jwt = jwt_module.decode_jwt = decode
    original_restore = socket.restore_existing_session

    def restore(sid, session_id, emit_fn, emit_call_fn, environ):
        existing = WebsocketSession.get_by_id(session_id)
        if existing:
            token = socket._get_token(environ)
            user = decode(token)
            if not existing.user or existing.user.identifier != user.identifier:
                raise ConnectionRefusedError("authorization failed")
            # The previous connection may have been revoked independently.
            decode(existing.token)
        return original_restore(sid, session_id, emit_fn, emit_call_fn, environ)

    socket.restore_existing_session = restore

    # Validate already-connected devices before every subsequent application event.
    for event, handler in list(server.sio.handlers.get("/", {}).items()):
        if event in {"connect", "disconnect"}:
            continue

        def wrap(callback):
            async def checked(sid, *args, **kwargs):
                session = WebsocketSession.get(sid)
                if not session or not session.token:
                    await server.sio.disconnect(sid)
                    return None
                try:
                    user = decode(session.token)
                except Exception:
                    await server.sio.disconnect(sid)
                    return None
                context = actor.set(user.identifier)
                try:
                    return await callback(sid, *args, **kwargs)
                finally:
                    actor.reset(context)

            return checked

        server.sio.handlers["/"][event] = wrap(handler)

    attempts = OrderedDict()

    @server.app.middleware("http")
    async def account_boundary(request: Request, call_next):
        path = request.url.path
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        origin = request.headers.get("origin")
        if (
            mutation
            and origin
            and (
                urlsplit(origin).netloc != request.url.netloc
                or urlsplit(origin).scheme != request.url.scheme
            )
        ):
            return JSONResponse({"detail": "跨站请求被拒绝。"}, status_code=403)
        if mutation and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "跨站请求被拒绝。"}, status_code=403)
        if mutation and (path == "/login" or path.startswith("/accounts/")):
            ip = request.client.host if request.client else "unknown"
            count, expiry = attempts.get(ip, (0, 0))
            if expiry < time.monotonic():
                count, expiry = 0, time.monotonic() + 60
            attempts[ip] = (count + 1, expiry)
            attempts.move_to_end(ip)
            while len(attempts) > 4096:
                attempts.popitem(last=False)
            if count >= 20:
                return JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试。"}, status_code=429
                )
            try:
                content_length = int(request.headers.get("content-length", "0") or 0)
            except ValueError:
                return JSONResponse({"detail": "请求格式无效。"}, status_code=400)
            if content_length > 4096:
                return JSONResponse({"detail": "请求过大。"}, status_code=413)
        token = get_token_from_cookies(request.cookies)
        if not token and request.headers.get("authorization", "").lower().startswith(
            "bearer "
        ):
            token = request.headers["authorization"][7:]
        identifier = None
        public = path in {
            "/",
            "/auth/config",
            "/login",
            "/logout",
            "/accounts/register",
            "/accounts/recover",
            "/account",
            "/accounts/page.js",
        } or path.startswith(("/assets/", "/public/", "/locales/"))
        if token and not public:
            try:
                identifier = decode(token).identifier
            except Exception:
                return JSONResponse(
                    {"detail": "登录已失效，请重新登录。"}, status_code=401
                )
        context = actor.set(identifier)
        try:
            response = await call_next(request)
        except PermissionError:
            response = JSONResponse({"detail": "无权访问该记录。"}, status_code=403)
        finally:
            actor.reset(context)
        if path.startswith("/accounts/") or path == "/account":
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"
            )
        return response

    async def body(request):
        raw = await request.body()
        if len(raw) > 4096:
            raise HTTPException(413, "请求过大。")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or any(
                not isinstance(v, str) for v in data.values()
            ):
                raise ValueError()
            return data
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "请求格式无效。") from exc

    def logged_user(request):
        token = get_token_from_cookies(request.cookies)
        try:
            return decode(token)
        except Exception as exc:
            raise HTTPException(401, "请先登录。") from exc

    async def action(request: Request, action: str):
        data = await body(request)
        store = get_store()
        try:
            if action == "register":
                import os

                if os.getenv("DATA_AGENT_ALLOW_REGISTRATION", "true").lower() != "true":
                    raise HTTPException(403, "注册已关闭，请联系管理员。")
                code = await asyncio.to_thread(
                    store.register, data.get("username", ""), data.get("password", "")
                )
                return {"recovery": code}
            if action == "recover":
                code = await asyncio.to_thread(
                    store.reset,
                    data.get("username", ""),
                    data.get("recovery", ""),
                    data.get("password", ""),
                )
                return {"recovery": code}
            user = logged_user(request)
            if action == "revoke":
                await asyncio.to_thread(store.revoke, user.identifier)
                return {"message": "所有设备的登录已失效，请重新登录。"}
            if action == "rotate":
                return {
                    "recovery": await asyncio.to_thread(
                        store.change_password,
                        user,
                        data.get("password", ""),
                        data.get("new_password", ""),
                    )
                }
            raise HTTPException(404)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @server.app.get("/account")
    async def page():
        return HTMLResponse(PAGE)

    @server.app.get("/accounts/page.js")
    async def script():
        from fastapi.responses import Response

        return Response(SCRIPT, media_type="application/javascript")

    @server.app.get("/accounts/me")
    async def me(request: Request):
        user = logged_user(request)
        return {
            "username": user.identifier,
            "profiles": sorted(get_store().profiles(user)),
        }

    @server.app.post("/accounts/{action}")
    async def account_action(request: Request, action: str):
        return await action_fn(request, action)

    action_fn = action
    # Routes must precede Chainlit's catch-all SPA route.
    for _ in range(4):
        server.app.router.routes.insert(0, server.app.router.routes.pop())
