import re


PURCHASER_SUFFIXES = ("桐乡",)


def normalize_person_name(name: str) -> str:
    normalized = re.sub(r"\s+", "", name or "")
    for suffix in PURCHASER_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def unique_purchaser_match(feishu_name: str, purchasers: set[str] | list[str]) -> str | None:
    target = normalize_person_name(feishu_name)
    matches = [p for p in purchasers if normalize_person_name(p) == target]
    return matches[0] if len(matches) == 1 else None
