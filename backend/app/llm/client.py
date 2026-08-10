import contextlib
import logging
from openai import AsyncOpenAI
from ..config import get_settings

log = logging.getLogger(__name__)


class LLMService:
    """Одна заявка к LM Studio одновременно + счётчик очереди."""

    def __init__(self):
        s = get_settings()
        self.client = AsyncOpenAI(base_url=s.llm_base_url,
                                  api_key=s.llm_api_key, timeout=s.llm_timeout)
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
        resp = await self.client.models.list()
        return [m.id for m in resp.data]

    async def pick_model(self) -> str:
        s = get_settings()
        if s.llm_default_model:
            return s.llm_default_model
        models = await self.list_models()
        if not models:
            raise RuntimeError("В LM Studio не загружено ни одной модели")
        return models[0]


llm = LLMService()
