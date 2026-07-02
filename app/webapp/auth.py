"""Авторизация Telegram Mini App по initData.

Подпись проверяется по алгоритму Telegram: secret_key = HMAC_SHA256("WebAppData", token),
hash = HMAC_SHA256(secret_key, data_check_string). Затем сверяем, что пользователь —
владелец (OWNER_TELEGRAM_ID). Пароль не нужен — доверяем подписи Telegram.
"""

import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status

from app.config.settings import get_settings


def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись initData. Возвращает разобранные поля или None."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    token = get_settings().bot_token
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    return parsed


async def require_owner(x_telegram_init_data: str = Header(default="")) -> int:
    """FastAPI-зависимость: пускает только владельца Mini App."""
    data = validate_init_data(x_telegram_init_data)
    if data is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid initData")
    try:
        user = json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad user")
    uid = user.get("id")
    if uid != get_settings().owner_telegram_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not owner")
    return uid
