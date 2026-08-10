"""Клиент Zabbix API (JSON-RPC, токен через параметр auth в теле)."""
import httpx
import datetime as dt
from ..config import get_settings


class ZabbixError(Exception):
    pass


class ZabbixClient:
    def __init__(self):
        s = get_settings()
        self.url = s.zabbix_url.rstrip("/") + "/api_jsonrpc.php"
        self.token = s.zabbix_token

    def call(self, method: str, params: dict | None = None):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        if self.token:
            body["auth"] = self.token
        headers = {"Content-Type": "application/json-rpc"}
        r = httpx.post(self.url, headers=headers, timeout=30, json=body)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise ZabbixError(err.get("data") or err.get("message"))
        return data["result"]

    def get_hosts(self) -> list[dict]:
        return self.call("host.get", {
            "output": ["hostid", "host", "name", "status", "description"],
            "selectHostGroups": ["name"],
            "selectInterfaces": ["ip", "dns", "useip", "type", "main"],
        })

    def get_problems(self, hostids: list[str] | None = None, limit: int = 30):
        params = {
            "output": ["eventid", "objectid", "object", "name", "severity",
                       "clock", "acknowledged"],
            "recent": False,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": limit,
        }
        if hostids:
            params["hostids"] = hostids
        problems = self.call("problem.get", params)
        if not problems:
            return []

        # objectid триггерной проблемы = triggerid → хосты берём из trigger.get
        trigger_ids = list({p["objectid"] for p in problems
                            if p.get("object", "0") == "0"})
        host_by_trigger = {}
        if trigger_ids:
            triggers = self.call("trigger.get", {
                "output": ["triggerid"],
                "triggerids": trigger_ids,
                "selectHosts": ["hostid", "host", "name"],
            })
            host_by_trigger = {
                t["triggerid"]: (t.get("hosts") or [{}])[0].get("host", "")
                for t in triggers
            }
        for p in problems:
            p["host"] = host_by_trigger.get(p["objectid"], "")
        return problems

    def get_items(self, hostids: list[str], search: str | None = None,
                  limit: int = 50):
        """Последние значения метрик хоста, с поиском по имени."""
        params = {
            "output": ["itemid", "name", "key_", "lastvalue", "units",
                       "value_type", "clock"],
            "hostids": hostids,
            "monitored": True,
            "sortfield": "name",
            "limit": limit,
        }
        if search:
            params["search"] = {"name": search}
            params["searchWildcardsEnabled"] = True
        return self.call("item.get", params)

    def get_history(self, itemid: str, value_type: int, hours: int = 24,
                    limit: int = 500):
        """История значений метрики (в пределах срока хранения Zabbix)."""
        time_from = int(dt.datetime.now(
            dt.timezone.utc).timestamp()) - hours * 3600
        return self.call("history.get", {
            "output": ["clock", "value"],
            "history": value_type,
            "itemids": [itemid],
            "time_from": time_from,
            "sortfield": "clock",
            "sortorder": "DESC",
            "limit": limit,
        })
