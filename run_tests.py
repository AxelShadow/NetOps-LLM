"""Консолидированный runner всех тестов netops-llm — Фаза 13 «Тестирование»
миграции UI (migration.md).

Что делает:
  * прогоняет КАЖДЫЙ тест отдельным subprocess — со своим интерпретатором
    венва и рабочим каталогом (backend/.venv из backend/, chainlit/.venv
    из chainlit/);
  * порядок фиксированный: сначала существующие наборы, затем новые
    тесты Фазы 13 (RBAC инструментов, лимит шагов агента, e2e-поток,
    адаптеры Chainlit);
  * для каждого теста печатает строку «N. имя — OK (Xs)» либо FAIL
    (с последними ~15 строками stdout/stderr для диагностики);
  * ещё не созданный тест Фазы 13 прогон не ломает — статус MISSING;
  * в конце — сводка всего/OK/FAIL/MISSING и общее время.

Изоляция БД: каждый тест проекта сам подменяет NETOPS_DATABASE_URL на
временную sqlite ДО импорта приложения, поэтому рабочая netops.db
не затрагивается; runner лишь добавляет PYTHONIOENCODING=utf-8,
чтобы кириллица в выводе тестов на Windows не превращалась в кракозябры.

Запуск (из корня репо, обычным python — сам runner венва не требует):
    python run_tests.py

Коды выхода:
    0 — все существующие тесты зелёные (MISSING провалом не считается);
    1 — хотя бы один FAIL;
    2 — не найден venv backend/chainlit (в сообщении — как создать).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# Кириллица в консоли Windows: гарантируем utf-8 на своих потоках вывода
# (при редиректе в файл Python по умолчанию берёт кодовую страницу cp1251).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # поток без reconfigure — редкий случай
        pass

# Все пути считаем от расположения этого файла — диски не хардкодим.
ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
CHAINLIT_DIR = ROOT / "chainlit"
BE_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"   # интерпретатор backend
CL_PY = CHAINLIT_DIR / ".venv" / "Scripts" / "python.exe"   # интерпретатор chainlit

TIMEOUT_SEC = 300   # запас: e2e/лимит гоняют SSE-циклы с ~20 tool-вызовами + sqlite
TAIL_LINES = 15     # сколько строк хвоста показывать при FAIL

# Реестр наборов: (имя для отчёта, интерпретатор, cwd, argv, файл для детекта MISSING).
# Порядок важен: сначала существующие тесты, затем новые (Фаза 13).
SUITES: list[tuple[str, Path, Path, list[str], str]] = [
    # --- backend: интерпретатор backend/.venv, cwd backend/ ---
    ("smoke_test.py",                        BE_PY, BACKEND_DIR, ["smoke_test.py"],                               "smoke_test.py"),
    ("mock_smoke.py",                        BE_PY, BACKEND_DIR, ["mock_smoke.py"],                               "mock_smoke.py"),
    ("internal_api_test.py",                 BE_PY, BACKEND_DIR, ["internal_api_test.py"],                        "internal_api_test.py"),
    ("internal_ext_test.py",                 BE_PY, BACKEND_DIR, ["internal_ext_test.py"],                        "internal_ext_test.py"),
    ("admin_ui_test.py",                     BE_PY, BACKEND_DIR, ["admin_ui_test.py"],                            "admin_ui_test.py"),
    ("tests/test_admin_ui_inventory.py",     BE_PY, BACKEND_DIR, ["tests/test_admin_ui_inventory.py"],             "tests/test_admin_ui_inventory.py"),
    ("tests/test_admin_ui_inventory_bulk.py", BE_PY, BACKEND_DIR, ["tests/test_admin_ui_inventory_bulk.py"],         "tests/test_admin_ui_inventory_bulk.py"),
    ("tests/test_admin_ui_audit.py",         BE_PY, BACKEND_DIR, ["tests/test_admin_ui_audit.py"],                 "tests/test_admin_ui_audit.py"),
    ("tests/test_admin_ui_settings.py",      BE_PY, BACKEND_DIR, ["tests/test_admin_ui_settings.py"],              "tests/test_admin_ui_settings.py"),
    ("tests/test_admin_ui_conversations.py", BE_PY, BACKEND_DIR, ["tests/test_admin_ui_conversations.py"],         "tests/test_admin_ui_conversations.py"),
    ("pytest tests/test_mock_mode.py -v",    BE_PY, BACKEND_DIR, ["-m", "pytest", "tests/test_mock_mode.py", "-v"], "tests/test_mock_mode.py"),
    ("tests/test_agent_tools_rbac.py (Фаза 13)", BE_PY, BACKEND_DIR, ["tests/test_agent_tools_rbac.py"],          "tests/test_agent_tools_rbac.py"),
    ("tests/test_agent_limit.py (Фаза 13)",      BE_PY, BACKEND_DIR, ["tests/test_agent_limit.py"],               "tests/test_agent_limit.py"),
    ("tests/test_e2e_flow.py (Фаза 13)",          BE_PY, BACKEND_DIR, ["tests/test_e2e_flow.py"],                 "tests/test_e2e_flow.py"),
    ("tests/test_ui_switch.py (Фаза 14)",         BE_PY, BACKEND_DIR, ["tests/test_ui_switch.py"],                "tests/test_ui_switch.py"),
    ("tests/test_snmp_migration.py (Этап SNMP)",  BE_PY, BACKEND_DIR, ["tests/test_snmp_migration.py"],            "tests/test_snmp_migration.py"),
    ("tests/test_snmp_tools.py (Этап SNMP)",      BE_PY, BACKEND_DIR, ["tests/test_snmp_tools.py"],              "tests/test_snmp_tools.py"),
    ("tests/test_printers_report.py (Этап 19)",  BE_PY, BACKEND_DIR, ["tests/test_printers_report.py"],          "tests/test_printers_report.py"),
    ("tests/test_printer_discovery.py (Этап 19)", BE_PY, BACKEND_DIR, ["tests/test_printer_discovery.py"],       "tests/test_printer_discovery.py"),
    # --- chainlit: интерпретатор chainlit/.venv, cwd chainlit/ ---
    ("sse_parser_test.py",                   CL_PY, CHAINLIT_DIR, ["sse_parser_test.py"],                         "sse_parser_test.py"),
    ("dev_sse/scenarios_test.py",            CL_PY, CHAINLIT_DIR, ["dev_sse/scenarios_test.py"],                   "dev_sse/scenarios_test.py"),
    ("adapters_test.py (Фаза 13)",           CL_PY, CHAINLIT_DIR, ["adapters_test.py"],                           "adapters_test.py"),
]


def fmt_plural(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: 1 набор / 2 набора / 5 наборов."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def fmt_dur(seconds: float) -> str:
    """Секунды -> человекочитаемо: «7.3s» / «4m 05s»."""
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def check_venvs() -> None:
    """Венвы обязательны; если нет — понятная инструкция и exit 2."""
    lost = [(label, py) for label, py in (("backend", BE_PY), ("chainlit", CL_PY))
            if not py.exists()]
    if not lost:
        return
    print("ОШИБКА: не найдены интерпретаторы виртуальных окружений:", flush=True)
    for label, py in lost:
        print(f"  [{label}] {py}", flush=True)
    print(flush=True)
    print(f"Как создать венвы (из корня репо: {ROOT}):", flush=True)
    print("  cd backend && python -m venv .venv", flush=True)
    print("  backend\\.venv\\Scripts\\pip install -r requirements.txt -r requirements-dev.txt", flush=True)
    print("  cd chainlit && python -m venv .venv", flush=True)
    print("  chainlit\\.venv\\Scripts\\pip install -r requirements.txt", flush=True)
    sys.exit(2)


def _as_text(data) -> str | None:
    """TimeoutExpired может отдать bytes (вывод не успели декодировать)."""
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def print_tail(stdout: str | None, stderr: str | None) -> None:
    """Последние ~TAIL_LINES строк stdout/stderr проваленного теста."""
    shown = False
    for label, text in (("stdout", stdout), ("stderr", stderr)):
        lines = (text or "").strip().splitlines()[-TAIL_LINES:]
        if not lines:
            continue
        shown = True
        print(f"    ----- последние строки {label} -----", flush=True)
        for line in lines:
            print(f"    | {line}", flush=True)
    if not shown:
        print("    (тест не оставил вывода)", flush=True)


def main() -> int:
    check_venvs()

    # env для дочерних процессов: кириллица в их выводе — utf-8
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    print("=" * 64, flush=True)
    print(f"NetOps-LLM — консолидированный прогон тестов "
          f"({len(SUITES)} {fmt_plural(len(SUITES), 'набор', 'набора', 'наборов')})", flush=True)
    print("=" * 64, flush=True)

    ok = fail = missing = 0
    failed: list[str] = []        # имена проваленных
    not_created: list[str] = []   # имена ещё не созданных (Фаза 13)

    started = time.monotonic()

    for num, (name, interp, cwd, argv, rel) in enumerate(SUITES, start=1):
        # Ещё не созданный тест Фазы 13 — не провал: фиксируем и идём дальше
        if not (cwd / rel).is_file():
            print(f"{num:>2}. {name} — MISSING — тест ещё не создан (Фаза 13 в работе)", flush=True)
            missing += 1
            not_created.append(name)
            continue

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [str(interp), *argv],   # интерпретатор нужного венва + аргументы
                cwd=str(cwd),            # правильный рабочий каталог
                capture_output=True,
                text=True,
                encoding="utf-8",       # тесты пишут utf-8 (см. env выше)
                errors="replace",
                env=env,
                timeout=TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            dur = time.monotonic() - t0
            print(f"{num:>2}. {name} — FAIL (таймаут {TIMEOUT_SEC}s, прошло {fmt_dur(dur)})", flush=True)
            print_tail(_as_text(exc.stdout), _as_text(exc.stderr))
            fail += 1
            failed.append(name)
            continue
        except OSError as exc:
            # интерпретатор не запустился (маловероятно: венвы проверены выше)
            print(f"{num:>2}. {name} — FAIL (не запустился: {exc})", flush=True)
            fail += 1
            failed.append(name)
            continue

        dur = time.monotonic() - t0
        if proc.returncode == 0:
            print(f"{num:>2}. {name} — OK ({fmt_dur(dur)})", flush=True)
            ok += 1
        else:
            print(f"{num:>2}. {name} — FAIL ({fmt_dur(dur)}, exit={proc.returncode})", flush=True)
            print_tail(proc.stdout, proc.stderr)
            fail += 1
            failed.append(name)

    total = len(SUITES)
    elapsed = time.monotonic() - started

    print(flush=True)
    print("=" * 64, flush=True)
    print("ИТОГОВАЯ СВОДКА", flush=True)
    print(f"  всего:   {total}", flush=True)
    print(f"  OK:      {ok}", flush=True)
    print(f"  FAIL:    {fail}", flush=True)
    print(f"  MISSING: {missing}  (не провал: тест Фазы 13 ещё пишется)", flush=True)
    print(f"  время:   {fmt_dur(elapsed)}", flush=True)
    if failed:
        print("  проваленные наборы:", flush=True)
        for n in failed:
            print(f"    - {n}", flush=True)
    if not_created:
        print("  ещё не созданы:", flush=True)
        for n in not_created:
            print(f"    - {n}", flush=True)
    print("=" * 64, flush=True)

    # MISSING провалом не считается; любой FAIL -> exit 1
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
