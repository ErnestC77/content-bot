import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config.settings import get_settings

security = HTTPBasic()

CSRF_COOKIE = "csrf_token"


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    settings = get_settings()
    user_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учётные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def csrf_protect(request: Request) -> None:
    """Проверка CSRF-токена (double-submit) для изменяющих запросов админки.

    Реализована как зависимость, а не middleware: FastAPI кеширует request.form(),
    поэтому чтение здесь не мешает обработчику получить поля формы.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie_token = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    form_token = form.get("_csrf")
    if not (
        cookie_token
        and form_token
        and secrets.compare_digest(str(cookie_token), str(form_token))
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF check failed")
