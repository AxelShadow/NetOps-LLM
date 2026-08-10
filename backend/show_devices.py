from app.db import SessionLocal
from app.models import Device

with SessionLocal() as db:
    rows = db.query(Device).all()
    if not rows:
        print("Таблица devices пуста")
    for d in rows:
        print(f"id={d.id} name={d.name!r} type={d.type.value} "
              f"host={d.host}:{d.port} user={d.username!r} enabled={d.enabled}")
