import json
import httpx
from .config import settings


async def _tenant_token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: {body.get('msg')}")
    return body["tenant_access_token"]


async def oauth_user(code: str) -> dict:
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("尚未配置飞书应用凭据")
    async with httpx.AsyncClient(timeout=30) as client:
        app_response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        app_response.raise_for_status()
        app_body = app_response.json()
        if app_body.get("code") != 0:
            raise RuntimeError(f"飞书应用鉴权失败: {app_body.get('msg')}")
        token_response = await client.post(
            "https://open.feishu.cn/open-apis/authen/v1/access_token",
            headers={"Authorization": f"Bearer {app_body['app_access_token']}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        token_response.raise_for_status()
        token_body = token_response.json()
        if token_body.get("code") != 0:
            raise RuntimeError(f"飞书登录失败: {token_body.get('msg')}")
        user_response = await client.get(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={
                "Authorization": f"Bearer {token_body['data']['access_token']}"
            },
        )
        user_response.raise_for_status()
        user_body = user_response.json()
        if user_body.get("code") != 0:
            raise RuntimeError(f"获取飞书用户信息失败: {user_body.get('msg')}")
        return user_body["data"]


async def admin_open_id() -> str:
    if settings.feishu_admin_open_id:
        return settings.feishu_admin_open_id
    if not settings.feishu_admin_mobile:
        return ""
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _tenant_token(client)
        response = await client.post(
            "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={"mobiles": [settings.feishu_admin_mobile]},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书管理员身份查询失败: {body.get('msg')}")
        users = body.get("data", {}).get("user_list", [])
        return str(users[0].get("user_id") or "") if users else ""


async def send_admin_message(title: str, content: str) -> bool:
    open_id = await admin_open_id()
    if not open_id:
        return False
    await send_message(open_id, title, content)
    return True


async def send_message(open_id: str, title: str, content: str):
    if settings.demo_mode:
        return {"demo": True, "open_id": open_id, "title": title, "content": content}
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("尚未配置飞书应用凭据")
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _tenant_token(client)
        response = await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": open_id,
                "msg_type": "post",
                "content": json.dumps({
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": content}]],
                    }
                }, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书发送失败: {body.get('msg')}")


async def send_card(open_id: str, card: dict):
    if settings.demo_mode:
        return {"demo": True, "open_id": open_id, "card": card}
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("尚未配置飞书应用凭据")
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _tenant_token(client)
        response = await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书卡片发送失败: {body.get('msg')}")
