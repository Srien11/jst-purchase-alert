import hashlib
import hmac


def review_signature(request_id: int, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"join-request:{request_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def valid_review_signature(request_id: int, signature: str, secret: str) -> bool:
    expected = review_signature(request_id, secret)
    return hmac.compare_digest(expected, signature or "")
