from contextlib import asynccontextmanager
from html import escape
import httpx
from urllib.parse import quote
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from .config import settings
from .feishu import oauth_user, send_message
from .jushuitan import fetch_orders
from .service import run_check
from .storage import (
    buyer_by_token,
    close_alert,
    closed_order_numbers,
    connect,
    consume_oauth_state,
    create_join_request,
    create_join_session,
    create_oauth_state,
    enable_by_token,
    approve_join_request,
    join_session,
    pending_join_requests,
    reopen_alert,
    set_buyer_enabled,
    upsert_buyer,
)


scheduler = AsyncIOScheduler(timezone=settings.timezone)


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.add_job(
        run_check,
        "cron",
        hour=settings.check_hour,
        minute=settings.check_minute,
        id="daily-purchase-alert",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="聚水潭采购交期预警", lifespan=lifespan)


class BuyerInput(BaseModel):
    purchaser: str
    feishu_open_id: str


class FeishuTestInput(BaseModel):
    feishu_open_id: str
    message: str = "采购交期预警服务已成功上线，飞书通知通道测试正常。"


def require_admin(token: str | None):
    if token != settings.app_admin_token:
        raise HTTPException(401, "管理员令牌错误")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/join")
def join():
    db = connect(settings.database_path)
    try:
        state = create_oauth_state(db)
    finally:
        db.close()
    callback = f"{settings.app_base_url.rstrip('/')}/join/callback"
    url = (
        "https://open.feishu.cn/open-apis/authen/v1/index"
        f"?app_id={quote(settings.feishu_app_id)}"
        f"&redirect_uri={quote(callback, safe='')}"
        f"&state={quote(state)}"
    )
    return RedirectResponse(url)


@app.get("/join/callback", response_class=HTMLResponse)
async def join_callback(code: str, state: str):
    db = connect(settings.database_path)
    try:
        if not consume_oauth_state(db, state):
            raise HTTPException(400, "授权已失效，请重新打开开通链接")
        user = await oauth_user(code)
        open_id = str(user.get("open_id") or "")
        name = str(user.get("name") or "").strip()
        if not open_id or not name:
            raise HTTPException(400, "飞书未返回有效用户身份")
        token = create_join_session(db, open_id, name)
    finally:
        db.close()
    try:
        orders = await fetch_orders()
    except Exception as exc:
        raise HTTPException(502, f"读取采购员列表失败：{exc}") from exc
    purchasers = sorted({o.purchaser for o in orders if o.purchaser})
    exact = next((p for p in purchasers if p.strip() == name), None)
    options = "".join(
        f'<option value="{escape(p)}"{" selected" if p == exact else ""}>{escape(p)}</option>'
        for p in purchasers
    )
    hint = (
        "姓名已与聚水潭采购员匹配，确认后立即开通。"
        if exact
        else "未找到同名采购员，请选择对应姓名；提交后由管理员审核。"
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>开通采购预警</title><style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f7fb;color:#172033}}
.card{{max-width:560px;margin:8vh auto;background:white;border-radius:22px;padding:30px;
box-shadow:0 18px 60px #29476818}}h1{{margin-top:0}}label{{display:block;margin:22px 0 8px}}
select,button{{width:100%;padding:13px;border-radius:12px;font-size:16px}}
select{{border:1px solid #dce3ed;background:white}}button{{margin-top:20px;border:0;
background:#1677ff;color:white;font-weight:700}}p{{color:#607087;line-height:1.7}}</style>
</head><body><main class="card"><h1>开通采购交期预警</h1>
<p>飞书用户：<strong>{escape(name)}</strong></p><p>{escape(hint)}</p>
<form method="post" action="/join/confirm">
<input type="hidden" name="token" value="{escape(token)}">
<label>聚水潭采购员</label><select name="purchaser" required>{options}</select>
<button>确认开通</button></form></main></body></html>"""
    )


@app.post("/join/confirm", response_class=HTMLResponse)
async def join_confirm(token: str = Form(...), purchaser: str = Form(...)):
    try:
        purchasers = {o.purchaser for o in await fetch_orders() if o.purchaser}
    except Exception as exc:
        raise HTTPException(502, f"读取采购员列表失败：{exc}") from exc
    if purchaser not in purchasers:
        raise HTTPException(400, "采购员不存在或当前不可用")
    db = connect(settings.database_path)
    try:
        session = join_session(db, token)
        if not session:
            raise HTTPException(400, "确认页面已过期，请重新开通")
        if session["feishu_name"].strip() == purchaser.strip():
            manage_token = upsert_buyer(db, purchaser, session["open_id"])
            set_buyer_enabled(db, manage_token, True)
            return RedirectResponse(f"/subscribe/{manage_token}", status_code=303)
        request_id = create_join_request(
            db, session["open_id"], session["feishu_name"], purchaser
        )
    finally:
        db.close()
    return HTMLResponse(
        f"<h2>申请已提交</h2><p>申请编号：{request_id}。姓名不一致，管理员审核后生效。</p>"
    )


@app.post("/admin/buyers")
def add_buyer(body: BuyerInput, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    db = connect(settings.database_path)
    try:
        token = upsert_buyer(db, body.purchaser.strip(), body.feishu_open_id.strip())
    finally:
        db.close()
    return {"invite_url": f"{settings.app_base_url.rstrip('/')}/subscribe/{token}"}


@app.get("/admin/join-requests")
def list_join_requests(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    db = connect(settings.database_path)
    try:
        return [dict(row) for row in pending_join_requests(db)]
    finally:
        db.close()


@app.post("/admin/join-requests/{request_id}/approve")
def approve_join(request_id: int, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    db = connect(settings.database_path)
    try:
        token = approve_join_request(db, request_id)
    finally:
        db.close()
    if not token:
        raise HTTPException(404, "待审核申请不存在")
    return {"ok": True, "manage_url": f"{settings.app_base_url}/subscribe/{token}"}


@app.post("/admin/run")
async def manual_run(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    return await run_check()


@app.post("/admin/test-feishu")
async def test_feishu(
    body: FeishuTestInput, x_admin_token: str | None = Header(None)
):
    require_admin(x_admin_token)
    try:
        await send_message(
            body.feishu_open_id.strip(),
            "采购交期预警 · 上线测试",
            body.message.strip(),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"飞书测试发送失败：{exc}") from exc
    return {"ok": True, "message": "测试消息已发送"}


def notification_page(token: str, buyer, closed: list[str]) -> HTMLResponse:
    purchaser = escape(buyer["purchaser"])
    enabled = bool(buyer["enabled"])
    status_text = "全部通知已开启" if enabled else "全部通知已关闭"
    status_class = "on" if enabled else "off"
    action = "disable" if enabled else "enable"
    action_text = "关闭全部通知" if enabled else "确认并开启全部通知"
    action_confirm = (
        ' onclick="return confirm(\'关闭后，在你再次手动开启前不会收到任何采购提醒。确认关闭？\')"'
        if enabled else ""
    )
    order_items = "".join(
        f"""<li><div><strong>{escape(order_no)}</strong><span>已关闭单据预警</span></div>
        <form method="post" action="/subscribe/{token}/orders/{escape(order_no)}/reopen">
        <button class="small secondary">恢复</button></form></li>"""
        for order_no in closed
    ) or '<li class="empty">暂无单独关闭的采购单</li>'
    return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>采购预警通知中心</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;color:#172033;background:
radial-gradient(circle at 10% 0,#dff4ff 0,transparent 34%),
linear-gradient(160deg,#f7fbff,#f4f7fb);font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{width:min(760px,calc(100% - 28px));margin:48px auto}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:22px}} .logo{{width:42px;height:42px;
border-radius:13px;background:linear-gradient(135deg,#1687ff,#35c8c3);display:grid;place-items:center;
color:white;font-size:22px;box-shadow:0 9px 24px #1687ff40}} .brand span{{font-weight:800;font-size:20px}}
.card{{background:#ffffffdd;border:1px solid #fff;border-radius:24px;padding:28px;
box-shadow:0 18px 60px #29476818;backdrop-filter:blur(14px);margin-bottom:18px}}
.eyebrow{{color:#718096;font-size:13px;letter-spacing:.08em}} h1{{font-size:28px;margin:8px 0 6px}}
.who{{color:#607087;margin:0 0 24px}} .status{{display:flex;justify-content:space-between;
align-items:center;padding:18px;border-radius:16px;background:#f6f8fb;margin-bottom:18px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:99px;margin-right:8px}}
.on .dot{{background:#19b66b;box-shadow:0 0 0 5px #19b66b18}} .off .dot{{background:#94a3b8}}
button{{border:0;border-radius:12px;background:#1677ff;color:white;font-weight:700;padding:12px 18px;
cursor:pointer}} button.danger{{background:#fff0f0;color:#d93f46}} button.secondary{{background:#edf4ff;color:#1769cf}}
button.small{{padding:8px 13px}} form{{margin:0}} .help{{font-size:13px;color:#7a8799;line-height:1.7}}
h2{{font-size:18px;margin:0 0 14px}} .close-form{{display:flex;gap:10px;margin:12px 0 18px}}
input{{min-width:0;flex:1;border:1px solid #dce3ed;border-radius:12px;padding:12px 14px;font-size:15px}}
ul{{list-style:none;padding:0;margin:0}} li{{display:flex;justify-content:space-between;align-items:center;
padding:13px 4px;border-top:1px solid #edf0f4}} li span{{display:block;color:#8792a4;font-size:12px;margin-top:4px}}
.empty{{color:#8b96a8;justify-content:center;padding:22px}} @media(max-width:560px){{.wrap{{margin:20px auto}}
.card{{padding:20px;border-radius:20px}} .status{{align-items:flex-start;gap:14px;flex-direction:column}}
.close-form{{flex-direction:column}} button{{width:100%}}}}
</style></head><body><main class="wrap">
<div class="brand"><div class="logo">◆</div><span>采购交期预警</span></div>
<section class="card"><div class="eyebrow">NOTIFICATION CENTER</div>
<h1>通知中心</h1><p class="who">采购员：<strong>{purchaser}</strong></p>
<div class="status {status_class}"><div><span class="dot"></span><strong>{status_text}</strong>
<div class="help">每天 09:00 检查；扣除 3 天运输缓冲，在剩余 12 / 7 / 3 天时提醒。</div></div>
<form method="post" action="/subscribe/{token}/{action}">
<button class="{'danger' if enabled else ''}"{action_confirm}>{action_text}</button></form></div>
<p class="help">关闭后，此后再开启前不接收任何提醒，系统重启或次日检查也不会自动恢复。
首次开启即表示确认订阅；没有命中预警时不发送飞书消息，节假日照常检查。</p>
</section>
<section class="card"><h2>单据通知管理</h2>
<form class="close-form" method="post" action="/subscribe/{token}/orders/close">
<input name="order_no" required placeholder="输入采购单号，例如 PO-20260701">
<button class="secondary">关闭该单预警</button></form>
<ul>{order_items}</ul></section></main></body></html>""")


@app.get("/subscribe/{token}", response_class=HTMLResponse)
def subscribe(token: str):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        closed = sorted(closed_order_numbers(db, buyer["purchaser"])) if buyer else []
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    return notification_page(token, buyer, closed)


@app.post("/subscribe/{token}/{action}")
def toggle_subscription(token: str, action: str):
    if action not in {"enable", "disable"}:
        raise HTTPException(404)
    db = connect(settings.database_path)
    try:
        if not buyer_by_token(db, token):
            raise HTTPException(404, "链接无效")
        set_buyer_enabled(db, token, action == "enable")
    finally:
        db.close()
    return RedirectResponse(f"/subscribe/{token}", status_code=303)


@app.get("/manage/{token}", response_class=HTMLResponse)
def manage(token: str):
    return RedirectResponse(f"/subscribe/{token}", status_code=302)


@app.post("/subscribe/{token}/orders/close")
def close_one(token: str, order_no: str = Form(...)):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        if not buyer:
            raise HTTPException(404, "链接无效")
        close_alert(db, order_no.strip(), buyer["purchaser"])
    finally:
        db.close()
    return RedirectResponse(f"/subscribe/{token}", status_code=303)


@app.post("/subscribe/{token}/orders/{order_no}/reopen")
def reopen_one(token: str, order_no: str):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        if not buyer:
            raise HTTPException(404, "链接无效")
        reopen_alert(db, order_no, buyer["purchaser"])
    finally:
        db.close()
    return RedirectResponse(f"/subscribe/{token}", status_code=303)
