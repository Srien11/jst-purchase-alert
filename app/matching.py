import re


PURCHASER_SUFFIXES = ("桐乡",)


def normalize_person_name(name: str) -> str:
    normalized = re.sub(r"\s+", "", name or "")
    for suffix in PURCHASER_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def person_name_aliases(name: str) -> set[str]:
    normalized = normalize_person_name(name)
    return {
        part
        for part in re.split(r"[&＆]", normalized)
        if part
    } | {normalized}


def unique_purchaser_match(feishu_name: str, purchasers: set[str] | list[str]) -> str | None:
    targets = person_name_aliases(feishu_name)
    matches = [
        purchaser
        for purchaser in purchasers
        if targets & person_name_aliases(purchaser)
    ]
    return matches[0] if len(matches) == 1 else None
