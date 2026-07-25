from contextlib import asynccontextmanager
from html import escape
import asyncio
import json
import httpx
from urllib.parse import quote
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Cookie, FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from .authorization import (
    AUTHORIZED_PURCHASERS,
    authorized_login_identity,
    is_authorized_purchaser,
    is_procurement_manager,
)
from .config import settings
from .feishu import oauth_user, send_message
from .logic import resolve_overdue_days as parse_overdue_days
from .review import valid_review_signature
from .service import (
    current_report_rows,
    in_transit_percentage,
    run_check,
    run_scheduled_checks,
    refresh_order_cache,
    send_manager_report,
    send_manual_report,
)
from .storage import (
    active_buyers as stored_active_buyers,
    buyer_by_token as stored_buyer_by_token,
    cached_orders,
    cached_purchasers,
    claim_system_event,
    close_alert,
    closed_order_numbers,
    connect,
    consume_oauth_state,
    create_oauth_state,
    finish_system_event,
    reopen_alert,
    set_buyer_enabled,
    system_event,
    update_buyer_schedule,
    update_buyer_manual_overdue_days,
    upsert_buyer,
)

scheduler = AsyncIOScheduler(timezone=settings.timezone)
ONE_TIME_BOUND_TEST = "bound-buyers-test-v1"


def buyer_by_token(db, token: str):
    buyer = stored_buyer_by_token(db, token)
    if buyer and is_authorized_purchaser(buyer["purchaser"]):
        return buyer
    return None


def active_buyers(db):
    return [
        buyer
        for buyer in stored_active_buyers(db)
        if is_authorized_purchaser(buyer["purchaser"])
    ]


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
    scheduler.add_job(
        refresh_order_cache,
        "interval",
        minutes=settings.cache_refresh_minutes,
        kwargs={"full": False},
        id="incremental-order-cache",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_order_cache,
        "cron",
        hour=2,
        minute=30,
        kwargs={"full": True},
        id="full-order-cache",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    asyncio.create_task(refresh_order_cache(full=True))
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="聚水潭采购交期预警", lifespan=lifespan)


def public_url(path: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/{path.lstrip('/')}"


def remember_buyer(response: RedirectResponse, token: str) -> RedirectResponse:
    response.set_cookie(
        "purchase_alert_token",
        token,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


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
def join(purchase_alert_token: str | None = Cookie(None)):
    db = connect(settings.database_path)
    try:
        if purchase_alert_token and buyer_by_token(db, purchase_alert_token):
            return RedirectResponse(
                public_url(f"/subscribe/{purchase_alert_token}")
            )
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
    finally:
        db.close()
    purchaser = authorized_login_identity(name)
    if purchaser:
        db = connect(settings.database_path)
        try:
            token = upsert_buyer(
                db,
                purchaser,
                open_id,
                is_manager=is_procurement_manager(purchaser),
            )
            set_buyer_enabled(db, token, True)
        finally:
            db.close()
        return remember_buyer(
            RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303),
            token,
        )
    raise HTTPException(403, "当前飞书账号尚未授权，请联系管理员绑定采购身份")


@app.post("/admin/buyers")
def add_buyer(body: BuyerInput, x_admin_token: str | None = Header(None)):
    require_admin(x_admin_token)
    purchaser = authorized_login_identity(body.purchaser.strip())
    if not purchaser:
        raise HTTPException(403, "该采购身份不在固定授权名单中")
    db = connect(settings.database_path)
    try:
        token = upsert_buyer(
            db,
            purchaser,
            body.feishu_open_id.strip(),
            is_manager=is_procurement_manager(purchaser),
        )
    finally:
        db.close()
    return {"invite_url": f"{settings.app_base_url.rstrip('/')}/subscribe/{token}"}


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


def overdue_range_fields(prefix: str, overdue_days: int) -> str:
    preset = overdue_days if overdue_days in {0, 7, 14, 30, 60} else -1
    options = "".join(
        f'<option value="{value}"{" selected" if preset == value else ""}>{label}</option>'
        for value, label in (
            (0, "不含逾期"),
            (7, "最近 7 天"),
            (14, "最近 14 天"),
            (30, "最近 30 天"),
            (60, "最近 60 天"),
            (-1, "自定义"),
        )
    )
    custom_style = "" if preset == -1 else ' style="display:none"'
    custom_value = overdue_days if preset == -1 else 30
    return f"""
    <div class="range-fields">
    <div class="field"><label>逾期未完成范围</label>
    <select id="{prefix}-preset" name="overdue_preset">{options}</select></div>
    <div id="{prefix}-custom" class="field"{custom_style}>
    <label>自定义天数（1–180）</label>
    <input type="number" name="custom_overdue_days" min="1" max="180"
    value="{custom_value}"></div></div>"""


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
    manual_overdue_days = int(buyer["manual_overdue_days"])
    schedule_overdue_days = int(buyer["schedule_overdue_days"])
    manual_range_fields = overdue_range_fields(
        "manual-overdue", manual_overdue_days
    )
    schedule_range_fields = overdue_range_fields(
        "schedule-overdue", schedule_overdue_days
    )
    order_items = "".join(
        f"""<li><div><strong>{escape(order_no)}</strong><span>已关闭单据预警</span></div>
        <form method="post" action="{public_url(f'/subscribe/{token}/orders/{escape(order_no)}/reopen')}">
        <button class="small secondary">恢复</button></form></li>"""
        for order_no in closed
    ) or '<li class="empty">暂无单独关闭的采购单</li>'
    weekday_style = "" if frequency == "weekly" else ' style="display:none"'
    schedule_title = "团队定时推送" if buyer["is_manager"] else "个人推送时间"
    schedule_target = (
        f"""<div class="field"><label>推送范围</label>
        <select id="schedule-purchaser" name="purchaser">
        <option value="*">全部采购员</option><option disabled>正在读取聚水潭人员…</option>
        </select></div>"""
        if buyer["is_manager"] else ""
    )
    schedule_panel = f"""
<section class="card"><h2>{schedule_title}</h2>
<form class="schedule-form{' manager-schedule-form' if buyer['is_manager'] else ''}"
method="post" action="{public_url(f'/subscribe/{token}/schedule')}">
{schedule_target}
<div class="field"><label>频率</label><select id="schedule-frequency" name="frequency">{frequency_options}</select></div>
<div class="field"><label>推送时间（北京时间）</label>
<input id="schedule-time" type="time" name="schedule_time" value="{schedule_time}" required>
<button type="button" class="small secondary now-button" id="use-beijing-now">填入北京时间（下一分钟）</button>
<div class="help" id="beijing-now"></div></div>
<div id="weekday-field" class="field"{weekday_style}><label>每周日期</label><select name="weekday">{weekday_options}</select></div>
{schedule_range_fields}
<button>保存设置</button></form>
<p class="help">此处范围只用于自动推送，与手动获取范围分开保存。</p></section>
<script>
const frequencySelect=document.getElementById("schedule-frequency");
const weekdayField=document.getElementById("weekday-field");
const scheduleTimeInput=document.getElementById("schedule-time");
const beijingClock=document.getElementById("beijing-now");
const beijingFormatter=new Intl.DateTimeFormat("en-GB",{{
  timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hourCycle:"h23"
}});
function beijingTimeAfter(minutes=0) {{
  return beijingFormatter.format(new Date(Date.now()+minutes*60000));
}}
function updateBeijingClock() {{
  beijingClock.textContent=`当前北京时间：${{beijingTimeAfter()}}`;
}}
function toggleWeekday() {{
  weekdayField.style.display=frequencySelect.value==="weekly" ? "" : "none";
}}
function bindOverdueRange(prefix) {{
  const preset=document.getElementById(prefix+"-preset");
  const custom=document.getElementById(prefix+"-custom");
  if(!preset || !custom) return;
  const toggle=()=>{{custom.style.display=preset.value==="-1" ? "" : "none";}};
  preset.addEventListener("change",toggle);toggle();
}}
frequencySelect.addEventListener("change", toggleWeekday);
document.getElementById("use-beijing-now").addEventListener("click",()=>{{
  scheduleTimeInput.value=beijingTimeAfter(1);
}});
bindOverdueRange("schedule-overdue");
toggleWeekday();
updateBeijingClock();
setInterval(updateBeijingClock,30000);
</script>"""
    personal_panels = ""
    if not buyer["is_manager"]:
        personal_panels = f"""
<section class="card"><h2>立即获取在途数据</h2>
<p class="help">实时读取你名下尚未全部入库的明细。此处范围只用于手动获取，并会为你单独保存。</p>
<form class="manual-form" method="post" action="{public_url(f'/subscribe/{token}/fetch-now')}">
{manual_range_fields}
<button onclick="this.disabled=true;this.form.submit()">保存范围并发送给我</button></form>
<script>bindOverdueRange("manual-overdue");</script></section>
<section class="card"><h2>单据通知管理</h2>
<form class="close-form" method="post" action="{public_url(f'/subscribe/{token}/orders/close')}">
<input name="order_no" required placeholder="输入采购单号，例如 PO-20260701">
<button class="secondary">关闭该单预警</button></form>
<ul>{order_items}</ul></section>"""
    manager_panel = ""
    if buyer["is_manager"]:
        selected_schedule_purchaser = json.dumps(
            buyer["schedule_purchaser"], ensure_ascii=False
        )
        manager_panel = f"""
<section class="card manager-card"><div class="eyebrow">TEAM ACCESS</div>
<h2>采购团队数据</h2>
<p class="help">负责人专属权限。手动范围会为你单独保存，与团队自动推送范围互不影响。</p>
<form class="manager-form" method="post" action="{public_url(f'/subscribe/{token}/manager/fetch-now')}">
<div class="field"><label>查询对象</label><select id="team-purchaser" name="purchaser">
<option value="*">全部采购员</option><option disabled>正在读取聚水潭人员…</option>
</select></div>
{manual_range_fields}
<button onclick="this.disabled=true;this.form.submit()">保存范围并发送给我</button>
</form><p id="team-load-status" class="help"></p></section>
<script>
fetch("{public_url(f'/subscribe/{token}/manager/purchasers')}")
.then(response => {{if(!response.ok) throw new Error(); return response.json()}})
.then(data => {{
  const saved={selected_schedule_purchaser};
  ["team-purchaser","schedule-purchaser"].forEach(id => {{
    const select=document.getElementById(id);
    select.innerHTML='<option value="*">全部采购员</option>';
    data.purchasers.forEach(name => {{
      const option=document.createElement("option");
      option.value=name; option.textContent=name; select.appendChild(option);
    }});
    if(id==="schedule-purchaser" &&
       [...select.options].some(option => option.value===saved)) select.value=saved;
  }});
  document.getElementById("team-load-status").textContent=
    `已读取 ${{data.purchasers.length}} 位聚水潭采购员`;
}})
.catch(() => {{
  document.getElementById("team-load-status").textContent="人员读取失败，请刷新重试";
}});
bindOverdueRange("manual-overdue");
</script>"""
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
 .schedule-form{{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:start}}
 .manager-schedule-form{{grid-template-columns:1.2fr 1fr 1fr 1fr auto}}
 .manager-form,.manual-form{{display:grid;grid-template-columns:1fr;gap:10px}}
 .range-fields{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
 gap:10px;grid-column:1/-1}}
 .schedule-form>button,.manager-form>button,.manual-form>button{{justify-self:start}}
 .manager-card{{border:1px solid #d9e9ff;background:linear-gradient(145deg,#fff,#f2f8ff)}}
 .field label{{display:block;color:#607087;font-size:13px;margin-bottom:6px}}
 .now-button{{width:100%;margin-top:7px}}
ul{{list-style:none;padding:0;margin:0}} li{{display:flex;justify-content:space-between;align-items:center;
padding:13px 4px;border-top:1px solid #edf0f4}} li span{{display:block;color:#8792a4;font-size:12px;margin-top:4px}}
.empty{{color:#8b96a8;justify-content:center;padding:22px}} @media(max-width:560px){{.wrap{{margin:20px auto}}
.card{{padding:20px;border-radius:20px}} .status{{align-items:flex-start;gap:14px;flex-direction:column}}
 .close-form{{flex-direction:column;align-items:stretch}}
 .schedule-form,.manager-form,.manual-form,.range-fields{{grid-template-columns:1fr}}
 .schedule-form>button,.manager-form>button,.manual-form>button{{justify-self:stretch}}
 button{{width:100%}}}}
</style></head><body><main class="wrap">
<div class="brand"><div class="logo">◆</div><span>采购交期预警</span></div>
<section class="card"><div class="eyebrow">NOTIFICATION CENTER</div>
<h1>通知中心</h1><p class="who">采购员：<strong>{purchaser}</strong></p>
<div class="status {status_class}"><div><span class="dot"></span><strong>{status_text}</strong>
<div class="help">显示与推送均按交期直接计算剩余天数：0–6 天红色、7–10 天黄色、11–15 天绿色。</div></div>
<form method="post" action="{public_url(f'/subscribe/{token}/notifications/{action}')}">
<button class="{'danger' if enabled else ''}"{action_confirm}>{action_text}</button></form></div>
<p class="help">关闭后，此后再开启前不接收自动推送，系统重启或次日检查也不会自动恢复。
开启后会按设置时间发送完整 0–15 天数据；当前无在途数据时也会发送确认消息。</p>
</section>
{schedule_panel}
{manager_panel}
{personal_panels}</main></body></html>""", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


def full_report_page(
    token: str,
    buyer,
    selected: list[str],
    orders,
    overdue_days: int,
) -> HTMLResponse:
    sections = []
    db = connect(settings.database_path)
    try:
        personal_closed = (
            closed_order_numbers(db, buyer["purchaser"])
            if not buyer["is_manager"]
            else set()
        )
    finally:
        db.close()
    for name in selected:
        rows = current_report_rows(
            orders,
            name,
            personal_closed if name == buyer["purchaser"] else set(),
            overdue_days,
        )
        grouped = {}
        for row in rows:
            grouped.setdefault(row.order.order_no, []).append(row)
        table_rows = []
        for order_no, order_rows in grouped.items():
            order_rows.sort(key=lambda row: (row.effective_days_left, row.order.sku))
            orders_in_group = [row.order for row in order_rows]
            urgent = min(order_rows, key=lambda row: row.effective_days_left)
            ordered_qty = sum(order.ordered_qty for order in orders_in_group)
            received_qty = sum(order.received_qty for order in orders_in_group)
            pending_qty = sum(order.pending_qty for order in orders_in_group)
            suppliers = "、".join(
                sorted({order.supplier for order in orders_in_group if order.supplier})
            )
            item_names = "、".join(
                sorted(
                    {
                        order.item_name
                        for order in orders_in_group
                        if order.item_name
                    }
                )
            )
            is_overdue = urgent.effective_days_left < 0
            level_class = (
                "red"
                if is_overdue
                else {"红": "red", "黄": "yellow", "绿": "green"}[urgent.level]
            )
            level_text = (
                f"逾期 {abs(urgent.effective_days_left)} 天"
                if is_overdue
                else f"{urgent.level} · {urgent.effective_days_left} 天"
            )
            days_text = (
                f"逾期 {abs(urgent.effective_days_left)} 天"
                if is_overdue
                else f"剩余 {urgent.effective_days_left} 天"
            )
            table_rows.append(
                f"""<tr>
                <td data-label="预警"><span class="badge {level_class}">{level_text}</span></td>
                <td data-label="采购单"><strong>{escape(order_no)}</strong></td>
                <td data-label="商品名称" class="product-cell">{escape(item_names or "未命名商品")}</td>
                <td data-label="供应商">{escape(suppliers or "—")}</td>
                <td data-label="最早交期">{min(order.delivery_date for order in orders_in_group)}</td>
                <td data-label="时间状态">{days_text}</td>
                <td data-label="订购">{ordered_qty}</td>
                <td data-label="已入库">{received_qty}</td>
                <td data-label="在途">{pending_qty}</td>
                <td data-label="在途占比"><strong>{in_transit_percentage(pending_qty, ordered_qty)}</strong></td>
                </tr>"""
            )
        content = (
            f"""<div class="table-shell"><table>
            <colgroup><col class="status-col"><col class="order-col"><col class="product-col">
            <col class="supplier-col"><col class="date-col"><col class="days-col">
            <col class="qty-col"><col class="qty-col"><col class="qty-col"><col class="ratio-col"></colgroup>
            <thead><tr><th>预警</th><th>采购单</th><th>商品名称</th><th>供应商</th>
            <th>最早交期</th><th>最短剩余</th><th>订购</th><th>已入库</th><th>在途</th><th>在途占比</th>
            </tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>"""
            if table_rows
            else '<div class="empty-report">当前筛选范围内没有未完成的在途数据</div>'
        )
        sections.append(
            f"""<section class="buyer-section"><h2>{escape(name)}</h2>
            <p>{len(grouped)} 张采购单，{len(rows)} 条商品记录</p>{content}</section>"""
        )
    back = public_url(f"/subscribe/{token}")
    overdue_range_text = (
        "不含逾期数据"
        if overdue_days == 0
        else f"包含最近 {overdue_days} 天逾期未完成数据"
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>完整在途数据</title><style>
        *{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#172033;
        font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}}
        main{{width:min(1480px,calc(100% - 24px));margin:28px auto 60px}}
        .top{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:24px}}
        a{{background:#1677ff;color:#fff;text-decoration:none;padding:11px 16px;border-radius:12px}}
        h1{{margin:0}}.buyer-section{{margin-top:28px}}.buyer-section>p{{color:#718096}}
        .scope{{display:flex;justify-content:space-between;align-items:center;gap:16px;
        background:#fff;padding:16px 18px;border-radius:16px;box-shadow:0 8px 25px #29476810;
        margin-bottom:18px}}.scope strong{{color:#172033}}.scope span{{color:#607087;font-size:14px}}
        small{{display:block;color:#718096;font-size:12px}}
        .table-shell{{background:#fff;border-radius:18px;box-shadow:0 10px 35px #29476812;overflow:hidden}}
        table{{width:100%;border-collapse:collapse;table-layout:fixed}}
        th{{background:#f7f9fc;color:#64748b;font-size:12px;text-align:left;padding:13px 10px;
        position:sticky;top:0;z-index:1}}
        td{{padding:14px 10px;border-top:1px solid #edf1f6;font-size:13px;line-height:1.5;
        vertical-align:middle;overflow-wrap:anywhere}}
        tbody tr:hover{{background:#f8fbff}}.product-cell{{font-weight:600}}
        .status-col{{width:8%}}.order-col{{width:12%}}.product-col{{width:22%}}
        .supplier-col{{width:12%}}.date-col{{width:9%}}.days-col{{width:8%}}
        .qty-col{{width:7%}}.ratio-col{{width:8%}}
        .badge{{display:inline-block;padding:6px 9px;border-radius:99px;font-weight:700;white-space:nowrap}}
        .red{{background:#fff0f0;color:#d9363e}}.yellow{{background:#fff8df;color:#9a6900}}
        .green{{background:#eaf8ef;color:#17824b}}
        .empty-report{{background:#fff;padding:24px;border-radius:16px;color:#718096}}
        @media(max-width:760px){{main{{margin-top:18px}}.top{{align-items:flex-start;flex-direction:column}}
        .top a{{width:100%;text-align:center}}.scope{{align-items:flex-start;flex-direction:column}}
        .table-shell{{background:transparent;box-shadow:none;overflow:visible}}
        table,tbody{{display:block}}thead,colgroup{{display:none}}tr{{display:grid;grid-template-columns:1fr 1fr;
        gap:0;background:#fff;border-radius:16px;margin:12px 0;padding:8px;box-shadow:0 8px 28px #29476812}}
        td{{display:block;border:0;padding:8px;font-size:14px}}td::before{{content:attr(data-label);
        display:block;color:#718096;font-size:11px;margin-bottom:3px}}
        td:nth-child(2),td.product-cell{{grid-column:1/-1}}}}
        </style></head><body><main><div class="top"><div><small>采购交期预警</small>
        <h1>完整在途数据</h1></div><a href="{back}">返回通知中心</a></div>
        <div class="scope"><strong>本次发送范围</strong>
        <span>预计入库 0–15 天未完成数据；{overdue_range_text}</span></div>
        {''.join(sections)}</main></body></html>""",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/subscribe/{token}/report", response_class=HTMLResponse)
def full_report(
    token: str,
    purchaser: str = "*",
    overdue_days: int = 0,
):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        orders = cached_orders(db)
        available = cached_purchasers(db)
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    if overdue_days not in range(0, 181):
        raise HTTPException(400, "逾期范围须为 0–180 天")
    if buyer["is_manager"]:
        if purchaser != "*" and purchaser not in available:
            raise HTTPException(404, "采购员不存在")
        selected = available if purchaser == "*" else [purchaser]
    else:
        selected = [buyer["purchaser"]]
    return full_report_page(
        token, buyer, selected, orders, overdue_days
    )


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


def resolve_overdue_days(preset: int, custom_days: int) -> int:
    try:
        return parse_overdue_days(preset, custom_days)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/subscribe/{token}/schedule")
async def save_schedule(
    token: str,
    frequency: str = Form(...),
    schedule_time: str = Form(...),
    weekday: int = Form(0),
    purchaser: str = Form("*"),
    overdue_preset: int = Form(0),
    custom_overdue_days: int = Form(30),
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
    schedule_overdue_days = resolve_overdue_days(
        overdue_preset, custom_overdue_days
    )
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        if not buyer:
            raise HTTPException(404, "链接无效")
    finally:
        db.close()
    schedule_purchaser = "*"
    if buyer["is_manager"]:
        db = connect(settings.database_path)
        try:
            available = cached_purchasers(db)
        finally:
            db.close()
        if purchaser != "*" and purchaser not in available:
            raise HTTPException(400, "定时推送筛选的采购员不存在")
        schedule_purchaser = purchaser
    db = connect(settings.database_path)
    try:
        update_buyer_schedule(
            db,
            token,
            frequency,
            hour,
            minute,
            weekday,
            schedule_purchaser,
            schedule_overdue_days,
        )
    finally:
        db.close()
    return RedirectResponse(public_url(f"/subscribe/{token}"), status_code=303)


@app.post("/subscribe/{token}/fetch-now", response_class=HTMLResponse)
async def fetch_now(
    token: str,
    overdue_preset: int = Form(0),
    custom_overdue_days: int = Form(30),
):
    manual_overdue_days = resolve_overdue_days(
        overdue_preset, custom_overdue_days
    )
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
        if buyer:
            update_buyer_manual_overdue_days(
                db, token, manual_overdue_days
            )
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
async def manager_fetch_now(
    background_tasks: BackgroundTasks,
    token: str,
    purchaser: str = Form("*"),
    overdue_preset: int = Form(0),
    custom_overdue_days: int = Form(30),
):
    selected_overdue_days = resolve_overdue_days(
        overdue_preset, custom_overdue_days
    )
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    if not buyer["is_manager"]:
        raise HTTPException(403, "仅采购部负责人可查看团队数据")
    db = connect(settings.database_path)
    try:
        update_buyer_manual_overdue_days(
            db, token, selected_overdue_days
        )
    finally:
        db.close()
    background_tasks.add_task(
        send_manager_report, token, purchaser, None, selected_overdue_days
    )
    back = public_url(f"/subscribe/{token}")
    return HTMLResponse(
        f"""<meta name="viewport" content="width=device-width,initial-scale=1">
        <div style="max-width:520px;margin:12vh auto;font-family:sans-serif;padding:28px">
        <h2>实时数据任务已提交</h2>
        <p>系统正在从聚水潭读取并按采购员拆分，完成后会发送到你的飞书，
        无需停留在此页面等待。</p><p><a href="{back}">返回通知中心</a></p></div>"""
    )


@app.get("/subscribe/{token}/manager/purchasers")
async def manager_purchasers(token: str):
    db = connect(settings.database_path)
    try:
        buyer = buyer_by_token(db, token)
    finally:
        db.close()
    if not buyer:
        raise HTTPException(404, "链接无效")
    if not buyer["is_manager"]:
        raise HTTPException(403, "仅采购部负责人可查看团队数据")
    db = connect(settings.database_path)
    try:
        purchasers = cached_purchasers(db)
    finally:
        db.close()
    return {"purchasers": purchasers}


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
