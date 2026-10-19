"""CLI переиндексации runbook'ов для RAG (Этап 21).

Запуск (из backend/, интерпретатор backend/.venv):

    .\\.venv\\Scripts\\python.exe reindex_rag.py            # инкрементально
    .\\.venv\\Scripts\\python.exe reindex_rag.py --force    # всё заново
    .\\.venv\\Scripts\\python.exe reindex_rag.py --status   # только статус

Коды выхода: 0 — успех (в т.ч. «всё пропущено, изменений нет»); 1 — были
ошибки или индексация не выполнена.

Сеть: обращается только к Qdrant и серверу эмбеддингов из настроек
(`NETOPS_RAG_*`, `NETOPS_EMBEDDING_*`); секреты и адреса не печатаются.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Запуск из любого cwd: backend/ в sys.path, чтобы импортировался пакет app.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Переиндексация runbook'ов для RAG")
    parser.add_argument("--force", action="store_true",
                        help="пересчитать все файлы, не пропускать по sha256")
    parser.add_argument("--status", action="store_true",
                        help="только показать статус индекса")
    args = parser.parse_args(argv)

    try:
        # Импорт моделей регистрирует таблицы в metadata; create_all создаёт
        # недостающие (на свежей sqlite таблицу rag_documents создаёт
        # lifespan приложения, в проде она уже есть — create_all ничего
        # не ломает, существующие таблицы не трогает).
        from app import models  # noqa: F401
        from app.db import Base, engine
        from app.rag.service import get_status, reindex_documents
        Base.metadata.create_all(engine)
    except Exception as exc:  # noqa: BLE001 - CLI не должен падать стеком
        # Только тип/краткий текст: значения настроек и секреты не печатаем.
        print("Ошибка инициализации RAG: "
              f"{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1

    if args.status:
        _print(get_status())
        return 0

    result = reindex_documents(force=args.force)
    _print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
