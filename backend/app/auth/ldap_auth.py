import logging
from ldap3 import Server, Connection
from ..config import get_settings

log = logging.getLogger(__name__)


def split_upn(upn: str):
    upn = (upn or "").strip()
    if "@" not in upn:
        return None
    name, domain = upn.split("@", 1)
    return name.strip().lower(), domain.strip().lower()


def ad_authenticate(upn: str, password: str) -> dict | None:
    """Возвращает данные пользователя из AD при успешной аутентификации."""
    s = get_settings()
    parsed = split_upn(upn)
    if not parsed:
        log.warning("Неверный формат UPN: %s", upn)
        return None
    name, domain = parsed
    if domain != s.ad_domain.lower():
        log.warning("Домен %r не совпадает с %r", domain, s.ad_domain)
        return None

    if s.dev_mode:
        log.warning("DEV MODE: пропуск проверки AD для %s", upn)
        return {"upn": upn, "name": name, "display_name": name}

    try:
        server = Server(s.ad_server, connect_timeout=5)
        conn = Connection(server, user=upn, password=password, auto_bind=True)

        # Получаем данные пользователя из AD
        conn.search(
            search_base=s.ad_search_base,
            search_filter=f"(userPrincipalName={upn})",
            attributes=["displayName", "mail", "department"]
        )

        if not conn.entries:
            log.warning("Пользователь %s не найден в AD", upn)
            conn.unbind()
            return None

        entry = conn.entries[0]
        conn.unbind()

        return {
            "upn": upn,
            "name": name,
            "display_name": str(entry.displayName) if entry.displayName else name,
            "email": str(entry.mail) if entry.mail else "",
            "department": str(entry.department) if entry.department else "",
        }
    except Exception:
        log.exception("LDAP bind failed для %s", upn)
        return None
