import json
from app.devices.zabbix import ZabbixClient

zc = ZabbixClient()

problems = zc.call("problem.get", {
    "output": ["eventid", "objectid", "object", "name", "severity", "clock"],
    "recent": False,
    "sortfield": ["eventid"],
    "sortorder": "DESC",
    "limit": 5,
})
print("Проблем найдено:", len(problems))
if not problems:
    raise SystemExit("Активных проблем нет — диагностировать нечего")
p = problems[0]
print("Пример проблемы:", json.dumps(p, ensure_ascii=False))

print("\n--- Вариант 1: problem.get + selectHosts ---")
try:
    r = zc.call("problem.get", {
        "output": ["eventid", "name"],
        "eventids": [p["eventid"]],
        "selectHosts": ["hostid", "host", "name"],
    })
    print(json.dumps(r, ensure_ascii=False))
except Exception as e:
    print("ошибка:", e)

print("\n--- Вариант 2: trigger.get по objectid + selectHosts ---")
try:
    r = zc.call("trigger.get", {
        "output": ["triggerid", "description"],
        "triggerids": [p["objectid"]],
        "selectHosts": ["hostid", "host", "name"],
    })
    print(json.dumps(r, ensure_ascii=False))
except Exception as e:
    print("ошибка:", e)

print("\n--- Вариант 3: event.get по eventid ---")
try:
    r = zc.call("event.get", {
        "output": ["eventid", "object", "objectid"],
        "eventids": [p["eventid"]],
    })
    print(json.dumps(r, ensure_ascii=False))
except Exception as e:
    print("ошибка:", e)
