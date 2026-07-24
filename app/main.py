from contextlib import asynccontextmanager
from html import escape
import httpx
from urllib.parse import quote
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from .config import settings
from .feishu import oauth_user, send_admin_message, send_message
from .jushuitan import fetch_orders
from .matching import normalize_person_name, unique_purchaser_match
from .review import review_signature, valid_review_signature
from .service import (
    run_check,
    run_scheduled_checks,
    send_manager_report,
    send_manual_report,
)
from .storage import (
    active_buyers,
    all_buyers,
    buyer_by_token,
    claim_system_event,
    close_alert,
    closed_order_numbers,
    connect,
    consume_oauth_state,
    create_join_request,
    create_join_session,
    create_oauth_state,
    enable_by_token,
    finish_system_event,
    approve_join_request,
    join_request_by_id,
    join_session,
    pending_join_requests,
    reject_join_request,
    reopen_alert,
    set_buyer_enabled,
    system_event,
    update_buyer_schedule,
    upsert_buyer,
)

PROCUREMENT_MANAGERS = {"吴子杰&茴香"}


def is_procurement_manager(purchaser: str) -> bool:
    normalized = normalize_person_name(purchaser)
    return any(
        normalized == normalize_person_name(manager)
        for manager in PROCUREMENT_MANAGERS
    )

scheduler = AsyncIOScheduler(timezone=settings.timezone)
ONE_TIME_BOUND_TEST = "bound-buyers-test-v1"


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.add_job(
        run_scheduled_checks,
        "cron",
        minute="*",
        id="personal-purchase-alerts",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="聚水潭采购交期预警", lifespan=lifespan)


def public_url(path: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/{path.lstrip('/')}"


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
    callback = public_url("/join/callback")
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
    purchasers = sorted(
        {o.purchaser for o in orders if o.purchaser} | PROCUREMENT_MANAGERS
    )
    exact = unique_purchaser_match(name, purchasers)
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
<form method="post" action="{public_url('/join/confirm')}">
<input type="hidden" name="token" value="{escape(token)}">
<label>聚水潭采购员</label><select name="purchaser" required>{options}</select>
<button>确认开通</button></form></main></body></html>"""
    )


@app.post("/join/confirm", response_class=HTMLResponse)
async def join_confirm(token: str = Form(...), purchaser: str = Form(...)):
    try:
        purchasers = (
            {o.purchaser for o in await fetch_orders() if o.purchaser}
            | PROCUREMENT_MANAGERS
        )
    except Exception as exc:
        raise HTTPException(502, f"读取采购员列表失败：{exc}") from exc
    if purchaser not in purchasers:
        raise HTTPException(400, "采购员不存在或当前不可用")
    db = connect(settings.database_path)
    try:
        session = join_session(db, token)
        if not session:
            raise HTTPException(400, "确认页面已过期，请重新开通")
        automatic_match = unique_purchaser_match(
            session["feishu_name"], purchasers
        )
        if automatic_match == purchaser:
            manage_token = upsert_buyer(
                db,
                purchaser,
                session["open_id"],
                is_manager=is_procurement_manager(purchaser),
            )
            set_buyer_enabled(db, manage_token, True)
            return RedirectResponse(public_url(f"/subscribe/{manage_token}"), status_code=303)
        request_id = create_join_request(
            db, session["open_id"], session["feishu_name"], purchaser
        )
    finally:
        db.close()
    if settings.feishu_admin_open_id or settings.feishu_admin_mobile:
        try:
            review_url = public_url(
                f"/admin/review/{request_id}?sig="
                f"{review_signature(request_id, settings.app_admin_token)}"
            )
            await send_admin_message(
                "采购预警开通申请待审核",
                (
                    f"申请编号：{request_id}\n"
                    f"飞书姓名：{session['feishu_name']}\n"
                    f"申请采购员：{purchaser}\n"
                    f"姓名无法安全自动匹配，请管理员审核。\n{review_url}"
                ),
            )
        except (httpx.HTTPError, RuntimeError):
            pass
    return HTMLResponse(
        f"<h2>申请已提交</h2><p>申请编号：{request_id}。姓名不一致，管理员审核后生效。</p>"
    )


@app.post("/admin/buyers")
def add_buyer(body: BuyerInput, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    db = connect(settings.database_path)
    try:
        purchaser = body.purchaser.strip()
        token = upsert_buyer(
            db,
            purchaser,
            body.feishu_open_id.strip(),
            is_manager=is_procurement_manager(purchaser),
        )
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


@app.get("/admin/review/{request_id}", response_class=HTMLResponse)
def review_join_request(request_id: int, sig: str):
    if not valid_review_signature(request_id, sig, settings.app_admin_token):
        raise HTTPException(403, "审核链接无效")
    db = connect(settings.database_path)
    try:
        request = join_request_by_id(db, request_id)
    finally:
        db.close()
    if not request:
        raise HTTPException(404, "申请不存在")
    status = escape(request["status"])
    disabled = " disabled" if status != "pending" else ""
    approve_url = public_url(f"/admin/review/{request_id}/approve")
    reject_url = public_url(f"/admin/review/{request_id}/reject")
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>审核采购预警开通</title><style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f7fb;color:#172033}}
.card{{max-width:560px;margin:8vh auto;background:white;border-radius:22px;padding:30px;
box-shadow:0 18px 60px #29476818}}.row{{padding:12px 0;border-bottom:1px solid #edf1f6}}
.actions{{display:flex;gap:12px;margin-top:24px}}form{{flex:1}}button{{width:100%;padding:13px;
border:0;border-radius:12px;color:white;font-weight:700;background:#1677ff}}
.reject{{background:#e5484d}}button:disabled{{background:#aab4c3}}</style></head>
<body><main class="card"><h1>开通申请审核</h1>
<div class="row">申请编号：{request_id}</div>
<div class="row">飞书姓名：{escape(request["feishu_name"])}</div>
<div class="row">聚水潭采购员：{escape(request["purchaser"])}</div>
<div class="row">当前状态：{status}</div><div class="actions">
<form method="post" action="{approve_url}"><input type="hidden" name="sig" value="{escape(sig)}">
<button{disabled}>同意开通</button></form>
<form method="post" action="{reject_url}"><input type="hidden" name="sig" value="{escape(sig)}">
<button class="reject"{disabled}>拒绝</button></form></div></main></body></html>"""
    )


@app.post("/admin/review/{request_id}/approve", response_class=HTMLResponse)
def approve_join_from_page(request_id: int, sig: str = Form(...)):
    if not valid_review_signature(request_id, sig, settings.app_admin_token):
        raise HTTPException(403, "审核链接无效")
    db = connect(settings.database_path)
    try:
        token = approve_join_request(db, request_id)
    finally:
        db.close()
    if not token:
        raise HTTPException(409, "申请已处理或不存在")
    return HTMLResponse("<h2>已同意开通</h2><p>该采购员的预警通知已启用。</p>")


@app.post("/admin/review/{request_id}/reject", response_class=HTMLResponse)
def reject_join_from_page(request_id: int, sig: str = Form(...)):
    if not valid_review_signature(request_id, sig, settings.app_admin_token):
        raise HTTPException(403, "审核链接无效")
    db = connect(settings.database_path)
    try:
        rejected = reject_join_request(db, request_id)
    finally:
        db.close()
    if not rejected:
        raise HTTPException(409, "申请已处理或不存在")
    return HTMLResponse("<h2>已拒绝申请</h2><p>未给该申请人开通预警。</p>")


@app.post("/admin/run")
async def manual_run(x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    return await run_check()


@app.get("/admin/test-bound-once", response_class=HTMLResponse)
def test_bound_once_page(sig: str):
    if not valid_review_signature(0, sig, settings.app_admin_token):
        raise HTTPException(403, "测试链接无效")
    db = connect(settings.database_path)
    try:
        buyers = active_buyers(db)
        event = system_event(db, ONE_TIME_BOUND_TEST)
    finally:
        db.close()
    status = (
        f"已执行：{escape(event['detail'])}"
        if event
        else f"尚未执行，将向 {len(buyers)} 位已绑定且已启用的采购员发送测试消息。"
    )
    disabled = " disabled" if event else ""
    action = public_url("/admin/test-bound-once")
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>一次性通知测试</title><style>
body{{font-family:"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f7fb;color:#172033}}
.card{{max-width:560px;margin:10vh auto;background:white;border-radius:22px;padding:30px;
box-shadow:0 18px 60px #29476818}}button{{width:100%;padding:13px;border:0;border-radius:12px;
color:white;font-weight:700;background:#1677ff}}button:disabled{{background:#aab4c3}}</style></head>
<body><main class="card"><h1>一次性通知测试</h1><p>{status}</p>
<form method="post" action="{action}"><input type="hidden" name="sig" value="{escape(sig)}">
<button{disabled}>确认发送一次</button></form></main></body></html>"""
    )


@app.post("/admin/test-bound-once", response_class=HTMLResponse)
async def run_test_bound_once(sig: str = Form(...)):
    if not valid_review_signature(0, sig, settings.app_admin_token):
        raise HTTPException(403, "测试链接无效")
    db = connect(settings.database_path)
    try:
        if not claim_system_event(db, ONE_TIME_BOUND_TEST):
            raise HTTPException(409, "一次性测试已经执行，不能重复发送")
        buyers = [dict(row) for row in active_buyers(db)]
    finally:
        db.close()
    sent = 0
    failures = []
    for buyer in buyers:
        try:
            await send_message(
                buyer["feishu_open_id"],
                "采购交期预警 · 一次性测试",
                (
                    f"采购员：{buyer['purchaser']}\n"
                    "这是一条上线验证消息，不是正式交期预警。"
                ),
            )
            sent += 1
        except (httpx.HTTPError, RuntimeError) as exc:
            failures.append(f"{buyer['purchaser']}: {exc}")
    detail = f"成功 {sent}/{len(buyers)}"
    if failures:
        detail += f"，失败 {len(failures)}"
    db = connect(settings.database_path)
    try:
        finish_system_event(db, ONE_TIME_BOUND_TEST, detail)
    finally:
        db.close()
    return HTMLResponse(f"<h2>测试完成</h2><p>{escape(detail)}</p>")


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


def notification_page(
    token: str, buyer, closed: list[str], team_buyers: list[str]
) -> HTMLResponse:
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
    frequency = buyer["schedule_frequency"]
    frequency_options = "".join(
        f'<option value="{value}"{" selected" if frequency == value else ""}>{label}</option>'
        for value, label in (
            ("daily", "每天"),
            ("weekdays", "工作日（周一至周五）"),
            ("weekly", "每周一次"),
        )
    )
    weekday_options = "".join(
        f'<option value="{value}"{" selected" if buyer["schedule_weekday"] == value else ""}>{label}</option>'
        for value, label in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日"))
    )
    schedule_time = f"{buyer['schedule_hour']:02d}:{buyer['schedule_minute']:02d}"
    order_items = "".join(
        f"""<li><div><strong>{escape(order_no)}</strong><span>已关闭单据预警</span></div>
        <form method="post" action="{public_url(f'/subscribe/{token}/orders/{escape(order_no)}/reopen')}">
        <button class="small secondary">恢复</button></form></li>"""
        for order_no in closed
    ) or '<li class="empty">暂无单独关闭的采购单</li>'
    manager_options = "".join(
        f'<option value="{escape(name)}">{escape(name)}</option>'
        for name in team_buyers
    )
    manager_panel = ""
    if buyer["is_manager"]:
        manager_panel = f"""
<section class="card manager-card"><div class="eyebrow">TEAM ACCESS</div>
<h2>采购团队数据</h2>
<p class="help">负责人专属权限。可筛选单个采购员，或获取全团队实时在途数据；结果只发送到你的飞书。</p>
<form class="manager-form" method="post" action="{public_url(f'/subscribe/{token}/manager/fetch-now')}">
<select name="purchaser"><option value="*">全部采购员</option>{manager_options}</select>
<button onclick="this.disabled=true;this.form.submit()">筛选并发送给我</button>
</form></section>"""
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
 input,select{{min-width:0;flex:1;border:1px solid #dce3ed;border-radius:12px;padding:12px 14px;font-size:15px;background:white}}
 .schedule-form{{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end}}
 .manager-form{{display:grid;grid-template-columns:1fr auto;gap:10px}}
 .manager-card{{border:1px solid #d9e9ff;background:linear-gradient(145deg,#fff,#f2f8ff)}}
 .field label{{display:block;color:#607087;font-size:13px;margin-bottom:6px}} .manual{{display:flex;gap:14px;align-items:center}}
ul{{list-style:none;padding:0;margin:0}} li{{display:flex;justify-content:space-between;align-items:center;
padding:13px 4px;border-top:1px solid #edf0f4}} li span{{display:block;color:#8792a4;font-size:12px;margin-top:4px}}
.empty{{color:#8b96a8;justify-content:center;padding:22px}} @media(max-width:560px){{.wrap{{margin:20px auto}}
.card{{padding:20px;border-radius:20px}} .status{{align-items:flex-start;gap:14px;flex-direction:column}}
 .close-form,.manual{{flex-direction:column;align-items:stretch}} .schedule-form{{grid-template-columns:1fr}} button{{width:100%}}}}
</style></head><body><main class="wrap">
<div class="brand"><div class="logo">◆</div><span>采购交期预警</span></div>
<section class="card"><div class="eyebrow">NOTIFICATION CENTER</div>
<h1>通知中心</h1><p class="who">采购员：<strong>{purchaser}</strong></p>
<div class="status {status_class}"><div><span class="dot"></span><strong>{status_text}</strong>
<div class="help">按你的个人时间检查；扣除 3 天运输缓冲，在剩余 12 / 7 / 3 天时提醒。</div></div>
<form method="post" action="{public_url(f'/subscribe/{token}/notifications/{action}')}">
<button class="{'danger' if enabled else ''}"{action_confirm}>{action_text}</button></form></div>
<p class="help">关闭后，此后再开启前不接收任何提醒，系统重启或次日检查也不会自动恢复。
首次开启即表示确认订阅；没有命中预警时不发送飞书消息，节假日照常检查。</p>
</section>
<section class="card"><h2>个人推送时间</h2>
<form class="schedule-form" method="post" action="{public_url(f'/subscribe/{token}/schedule')}">
<div class="field"><label>频率</label><select name="frequency">{frequency_options}</select></div>
<div class="field"><label>推送时间</label><input type="time" name="schedule_time" value="{schedule_time}" required></div>
<div class="field"><label>每周日期（仅每周一次生效）</label><select name="weekday">{weekday_options}</select></div>
<button>保存设置</button></form>
<p class="help">每天与工作日模式忽略“每周日期”。时间使用北京时间。</p></section>
<section class="card"><h2>立即获取在途数据</h2>
<div class="manual"><p class="help">实时读取你名下全部未入库明细并发送飞书表格，不影响自动提醒记录。</p>
<form method="post" action="{public_url(f'/subscribe/{token}/fetch-now')}">
<button onclick="this.disabled=true;this.form.submit()">立即发送给我</button></form></div></section>
{manager_panel}
<section class="card"><h2>单据通知管理</h2>
<form class="close-form" method="post" action="{public_url(f'/subscribe/{token}/orders/close')}">
<input name="order_no" required placeholder="输入采购单号，例如 PO-20260701">
<button class="secondary">关闭该单预警</button></form>
<ul>{order_items}</ul></section></main></body></html>""", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


@app.get("/subscribe/{token}", response_class=HTMLResponse)
def subscribe(token: str):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        closed = sorted(closed_order_numbers(db, buyer["purchaser"])) if buyer else []
        team_buyers = [row["purchaser"] for row in all_buyers(db)] if buyer else []
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    return notification_page(token, buyer, closed, team_buyers)


@app.post("/subscribe/{token}/notifications/{action}")
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
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303)


@app.post("/subscribe/{token}/schedule")
def save_schedule(
    token: str,
    frequency: str = Form(...),
    schedule_time: str = Form(...),
    weekday: int = Form(0),
):
    if frequency not in {"daily", "weekdays", "weekly"} or weekday not in range(7):
        raise HTTPException(400, "推送频率设置无效")
    try:
        hour_text, minute_text = schedule_time.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "推送时间格式无效") from exc
    if hour not in range(24) or minute not in range(60):
        raise HTTPException(400, "推送时间无效")
    db = connect(settings.database_path)
    try:
        if not buyer_by_token(db, token):
            raise HTTPException(404, "链接无效")
        update_buyer_schedule(db, token, frequency, hour, minute, weekday)
    finally:
        db.close()
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303)


@app.post("/subscribe/{token}/fetch-now", response_class=HTMLResponse)
async def fetch_now(token: str):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    try:
        result = await send_manual_report(token)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"立即获取失败：{exc}") from exc
    back = public_url(f"/subscribe/{token}")
    return HTMLResponse(
        f"""<meta name="viewport" content="width=device-width,initial-scale=1">
        <div style="max-width:520px;margin:12vh auto;font-family:sans-serif;padding:28px">
        <h2>已发送到飞书</h2>
        <p>共 {result['rows']} 条在途明细，{result['orders']} 张采购单，
        发送 {result['messages']} 张卡片。</p><p><a href="{back}">返回通知中心</a></p></div>"""
    )


@app.post("/subscribe/{token}/manager/fetch-now", response_class=HTMLResponse)
async def manager_fetch_now(token: str, purchaser: str = Form("*")):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    if not buyer["is_manager"]:
        raise HTTPException(403, "仅采购部负责人可查看团队数据")
    try:
        result = await send_manager_report(token, purchaser)
    except KeyError as exc:
        raise HTTPException(400, "筛选的采购员不存在") from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"团队数据获取失败：{exc}") from exc
    back = public_url(f"/subscribe/{token}")
    return HTMLResponse(
        f"""<meta name="viewport" content="width=device-width,initial-scale=1">
        <div style="max-width:520px;margin:12vh auto;font-family:sans-serif;padding:28px">
        <h2>团队数据已发送到飞书</h2>
        <p>覆盖 {result['buyers']} 位采购员、{result['orders']} 张采购单，
        发送 {result['messages']} 张卡片。</p><p><a href="{back}">返回通知中心</a></p></div>"""
    )


@app.get("/manage/{token}", response_class=HTMLResponse)
def manage(token: str):
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=302)


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
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303)


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
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303)
