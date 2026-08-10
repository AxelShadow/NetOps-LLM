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


def ad_authenticate(upn: str, password: str) -> bool:
    s = get_settings()
    log.info("Попытка входа: %s | dev_mode=%s | домен в настройках=%r",
             upn, s.dev_mode, s.ad_domain)

    parsed = split_upn(upn)
    if not parsed:
        log.warning("ОТКАЗ: логин не в формате user@domain")
        return False
    name, domain = parsed

    if domain != s.ad_domain.strip().lower():
        log.warning("ОТКАЗ: домен логина %r не совпадает с доменом настроек %r",
                    domain, s.ad_domain)
        return False

    if s.dev_mode:
        log.warning(
            "DEV MODE: пароль не проверяется, доступ разрешён для %s", upn)
        return True

    try:
        server = Server(s.ad_server, connect_timeout=5)
        conn = Connection(server, user=upn, password=password,
                          auto_bind=False, receive_timeout=10)
        ok = bool(conn.bind())
        conn.unbind()
        log.info("LDAP bind: %s", ok)
        return ok
    except Exception:
        log.exception("LDAP bind failed")
        return False
