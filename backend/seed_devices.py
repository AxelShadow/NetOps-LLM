"""Однократное добавление устройств: python seed_devices.py"""
from app.db import SessionLocal, Base, engine
from app.models import Device, DeviceType

Base.metadata.create_all(engine)

DEVICES = [
    dict(name="vmh08", type=DeviceType.esxi,
         host="172.27.214.56", port=443,
         username="test_llm", password="Dre55Kod"),
    dict(name="vcenter", type=DeviceType.vcenter,
         host="172.27.214.68", port=443,
         username="test_llm@vmwr.samges.ru", password="Dre%5Kod")
    # dict(name="esxi-solo-1", type=DeviceType.esxi,
    #      host="10.0.0.11", port=443,
    #      username="ro-user", password="ЗАМЕНИТЕ"),
]

with SessionLocal() as db:
    for data in DEVICES:
        d = db.query(Device).filter_by(name=data["name"]).first()
        if d:
            for k, v in data.items():
                setattr(d, k, v)
            print(f"{data['name']}: обновлён -> {data['host']}")
        else:
            db.add(Device(**data))
            print(f"{data['name']}: добавлен -> {data['host']}")
    db.commit()
