import contextlib
import json
import logging
from types import SimpleNamespace as NS
from openai import AsyncOpenAI
from ..config import get_settings

log = logging.getLogger(__name__)


def _chunk(delta):
    """Чанк стрима в формате openai: choices[0].delta."""
    return NS(choices=[NS(delta=delta)])


def _text_chunk(text: str):
    return _chunk(NS(content=text, tool_calls=None))


def _tool_chunk(index: int, call_id: str, name: str, arguments: str):
    tc = NS(index=index, id=call_id,
            function=NS(name=name, arguments=arguments))
    return _chunk(NS(content=None, tool_calls=[tc]))


class _MockCompletions:
    """Детерминированная заглушка chat.completions.create для мок-режима.

    Логика по последнему сообщению пользователя:
    - «ошибка» → 1-й ответ: tool_call ping(device="mock-ошибка"),
      после tool-результата — финальный текст.
    - «лимит»  → каждый ответ содержит tool_call get_current_time,
      агентский цикл упирается в лимит шагов.
    - обычное  → 1-й ответ: tool_call get_current_time; когда в истории
      уже есть role="tool" — финальный текст.
    """

    @staticmethod
    def _last_user_text(messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return m.get("content") or ""
        return ""

    @staticmethod
    def _has_tool_reply(messages) -> bool:
        return any(m.get("role") == "tool" for m in messages)

    async def create(self, *, messages, model, tools, stream: bool):
        text = self._last_user_text(messages)
        low = text.lower()
        scenario = "normal"

        if "лимит" in low:
            scenario = "limit"
        elif "ошибк" in low:  # ошибка/ошибкой/ошибку (морфология)
            scenario = "error"
        elif self._has_tool_reply(messages):
            scenario = "final"
        elif "tool" in low or "пинг" in low or "инструмент" in low:
            # явный запрос инструмента, отличного от сценария ошибки
            scenario = "final" if self._has_tool_reply(messages) else "tool"

        if scenario == "limit":
            # tool_call на каждом шаге → цикл упирается в лимит шагов
            chunks = [_tool_chunk(0, "mock-limit-1", "get_current_time", "{}")]
        elif scenario == "error":
            if self._has_tool_reply(messages):
                chunks = [_text_chunk(
                    "Инструмент завершился с ошибкой. "
                    "Это детерминированный ответ мок-режима (сценарий «ошибка»).")]
            else:
                chunks = [_tool_chunk(
                    0, "mock-err-1", "ping",
                    json.dumps({"device": "mock-ошибка"}, ensure_ascii=False))]
        elif scenario == "final":
            chunks = [_text_chunk(
                "Это детерминированный ответ мок-режима NETOPS_MOCK_MODE. "
                "Сетевые вызовы не выполнялись.")]
        else:  # normal / tool
            name = "get_current_time"
            args = "{}"
            if "пинг" in low or "ping" in low:
                name, args = "ping", json.dumps(
                    {"device": "mock-host"}, ensure_ascii=False)
            chunks = [_tool_chunk(0, "mock-call-1", name, args)]

        async def _iter():
            for c in chunks:
                yield c

        return _iter()


class _MockModels:
    async def list(self):
        return NS(data=[NS(id="mock-model"), NS(id="mock-model-tools")])


class _MockChat:
    completions = _MockCompletions()


class MockLLMClient:
    """Имитирует AsyncOpenAI при NETOPS_MOCK_MODE=true: без сетевых вызовов."""

    chat = _MockChat()
    models = _MockModels()


class LLMService:
    """Одна заявка к LM Studio одновременно + счётчик очереди."""

    def __init__(self):
        s = get_settings()
        if s.mock_mode:
            self.client = MockLLMClient()
            log.info("Мок-режим LLM: сетевые вызовы отключены")
        else:
            self.client = AsyncOpenAI(base_url=s.llm_base_url,
                                      api_key=s.llm_api_key,
                                      timeout=s.llm_timeout)
        self._sem = __import__("asyncio").Semaphore(1)
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def slot(self):
        self._waiting += 1
        try:
            await self._sem.acquire()
            yield
        finally:
            self._sem.release()
            self._waiting -= 1

    async def list_models(self) -> list[str]:
        s = get_settings()
        if s.mock_mode:
            return ["mock-model", "mock-model-tools"]
        resp = await self.client.models.list()
        return [m.id for m in resp.data]

    async def pick_model(self) -> str:
        s = get_settings()
        if s.mock_mode:
            return "mock-model"
        if s.llm_default_model:
            return s.llm_default_model
        models = await self.list_models()
        if not models:
            raise RuntimeError("В LM Studio не загружено ни одной модели")
        return models[0]


llm = LLMService()
