import ssl
import http.client
import sys
from app.db import SessionLocal
from app.models import Device, DeviceType

with SessionLocal() as db:
    d = (db.query(Device)
         .filter(Device.type.in_([DeviceType.vcenter, DeviceType.esxi]))
         .first())
    if not d:
        print("В инвентаре нет VMware-устройств")
        sys.exit(1)
    host, port = d.host, d.port or 443
    print(f"В инвентаре: host={d.host!r}, port={port}, user={d.username!r}")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    c = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
    c.request("GET", "/sdk")
    r = c.getresponse()
    print("HTTP статус:", r.status, r.reason)
    print("Начало ответа:", r.read(200))
except Exception as e:
    print("ОШИБКА ПОДКЛЮЧЕНИЯ:", type(e).__name__, "-", e)
