# 聚水潭采购交期预警服务

服务器常驻服务：每天拉取聚水潭采购单，按采购员隔离数据，直接按交期计算剩余天数，
在剩余 15、10、6 天时各发送一次飞书提醒；0–6 天为红色、7–10 天为黄色、
11–15 天为绿色，区间外不统计；同时输出已入库/未入库
对比及优先跟进建议。

## 部署

```bash
cp .env.example .env
# 编辑 .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

建议用 Nginx/Caddy 为 8000 端口配置 HTTPS 域名，并把 `APP_BASE_URL` 改为该域名。

## 给采购员生成专属确认链接

```bash
curl -X POST https://你的域名/admin/buyers \
  -H "X-Admin-Token: .env里的APP_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"purchaser":"王采购","feishu_open_id":"ou_xxx"}'
```

返回的 `invite_url` 发给该采购员。采购员打开后订阅生效，以后只收到聚水潭中
`采购员=王采购` 的数据。

每封飞书预警均附带该采购员的“预警管理”链接。采购员可按采购单号手动关闭预警；
关闭后该单不再发送 15/10/6 天提醒，也可随时恢复。关闭只影响本人，不影响其他采购员。
同一页面还提供“关闭全部通知”；关闭状态会持久保存，在本人再次手动开启前不接收任何提醒。

## 手动试跑

```bash
curl -X POST https://你的域名/admin/run \
  -H "X-Admin-Token: .env里的APP_ADMIN_TOKEN"
```

## 本地演示

在 `.env` 中设置 `DEMO_MODE=true` 后，服务使用三条内置模拟采购明细，不访问聚水潭。
演示采购员名称为 `演示采购员`。生产部署必须改回 `false`。

## 仍需提供的聚水潭信息

已确认使用采购单查询和采购入库查询两个 v2 接口，并按官方规则实现：
`app_secret + 字典序(key+value)` 经 UTF-8 MD5 生成小写 `sign`。生产凭据仅写入服务器
`.env`，不得提交到 Git。

拿到这些信息后，修改 `app/jushuitan.py` 的签名与字段映射即可接通。

## 飞书应用要求

使用企业自建应用机器人，并开通发送消息权限。需要 `APP_ID`、`APP_SECRET`，
以及每位采购员的 `open_id`。服务不会把这些凭据写入代码或日志。

## 上线后测试飞书

配置飞书应用凭据后，可通过管理员专用接口立即发送一条测试消息：

```bash
curl -X POST https://你的域名/admin/test-feishu \
  -H "X-Admin-Token: .env里的APP_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"feishu_open_id":"ou_xxx","message":"采购预警服务测试成功"}'
```

该接口不会触发采购预警或写入提醒记录。未携带正确管理员令牌时返回 `401`。

## 采购员自助开通

统一入口：`https://jushuitan.skills.kktree.cn/purchase-alert/join`

管理员先通过 `POST /admin/buyers` 将采购员与飞书 `open_id` 授权绑定。用户经飞书
OAuth 登录后直接进入本人页面，不需要选择采购员；未授权账号会被拒绝，避免冒领
其他采购员的数据。刘智博&木耳、吴子杰&茴香拥有负责人权限，登录后可筛选查看团队数据。

飞书开放平台需要开启网页应用能力，并将以下地址加入安全设置中的重定向 URL：

`https://jushuitan.skills.kktree.cn/purchase-alert/join/callback`

采购身份授权接口需要 `X-Admin-Token` 请求头。
