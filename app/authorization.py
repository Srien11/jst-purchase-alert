from .matching import unique_purchaser_match


PROCUREMENT_MANAGERS = {"吴子杰&茴香", "刘智博&木耳"}
AUTHORIZED_PURCHASERS = {
    "夏雨磊&大虾 桐乡",
    "廖钰&桑葚 桐乡",
    "张利兰&饺子 桐乡",
    "张小薇&花卷 桐乡",
    "朱虹&菱角 桐乡",
    "钟晓盈&椰子 桐乡",
}
AUTHORIZED_USERS = AUTHORIZED_PURCHASERS | PROCUREMENT_MANAGERS


def authorized_login_identity(feishu_name: str) -> str | None:
    return unique_purchaser_match(feishu_name, AUTHORIZED_USERS)


def is_authorized_purchaser(purchaser: str) -> bool:
    return purchaser in AUTHORIZED_USERS


def is_procurement_manager(purchaser: str) -> bool:
    return purchaser in PROCUREMENT_MANAGERS
