from app.config import get_settings
from app.auth.ldap_auth import ad_authenticate
from app.db import SessionLocal
from app.models import User

s = get_settings()
print("dev_mode  :", s.dev_mode)
print("ad_domain :", repr(s.ad_domain))
print("bootstrap :", repr(s.bootstrap_admin))

with SessionLocal() as db:
    users = db.query(User).all()
    print("В БД:", [(u.id, u.username, u.role.value, u.is_active) for u in users]
          or "ПУСТО — админ не создан!")

test_login = "admin@" + s.ad_domain.strip()
print(f"\nТестовый вход: {test_login} (пароль любой)")
print("Результат:", ad_authenticate(test_login, "test"))
