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
