from app.db import SessionLocal
from app.models import AuditLog

with SessionLocal() as db:
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(10).all()
    if not rows:
        print("Журнал пуст — инструменты НЕ вызывались")
    for r in rows:
        print("-" * 60)
        print(f"{r.created_at} | {r.tool} | статус={r.status}")
        print(f"аргументы: {r.arguments}")
        print(f"результат: {r.result[:500]}")
