import os
from abc import ABC, abstractmethod

import httpx
import numpy as np
import requests

from yuxi.services.model_cache import model_cache
from yuxi.utils import get_docker_safe_url, hashstr, logger


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


class BaseEmbeddingModel(ABC):
    def __init__(
        self,
        model=None,
        name=None,
        dimension=None,
        url=None,
        base_url=None,
        api_key=None,
        model_id=None,
        batch_size=40,
    ):
        base_url = base_url or url
        self.model = model or name or model_id
        self.dimension = dimension
        self.base_url = get_docker_safe_url(base_url)
        self.api_key = os.getenv(api_key, api_key)
        self.batch_size = int(batch_size or 40)
        self.embed_state = {}

    @abstractmethod
    def encode(self, message: list[str] | str) -> list[list[float]]:
        raise NotImplementedError("Subclasses must implement this method")

    def encode_queries(self, queries: list[str] | str) -> list[list[float]]:
        return self.encode(queries)

    @abstractmethod
    async def aencode(self, message: list[str] | str) -> list[list[float]]:
        raise NotImplementedError("Subclasses must implement this method")

    async def aencode_queries(self, queries: list[str] | str) -> list[list[float]]:
        return await self.aencode(queries)

    def batch_encode(self, messages: list[str], batch_size: int | None = None) -> list[list[float]]:
        batch_size = batch_size or self.batch_size
        data = []
        task_id = None
        if len(messages) > batch_size:
            task_id = hashstr(messages)
            self.embed_state[task_id] = {"status": "in-progress", "total": len(messages), "progress": 0}

        for i in range(0, len(messages), batch_size):
            group_msg = messages[i : i + batch_size]
            logger.info(f"Encoding [{i}/{len(messages)}] messages (bsz={batch_size})")
            response = self.encode(group_msg)
            data.extend(response)
            if task_id:
                self.embed_state[task_id]["progress"] = i + len(group_msg)

        if task_id:
            self.embed_state[task_id]["status"] = "completed"

        return data

    async def abatch_encode(self, messages: list[str], batch_size: int | None = None) -> list[list[float]]:
        batch_size = batch_size or self.batch_size
        data = []
        task_id = None
        if len(messages) > batch_size:
            task_id = hashstr(messages)
            self.embed_state[task_id] = {"status": "in-progress", "total": len(messages), "progress": 0}

        for i in range(0, len(messages), batch_size):
            group_msg = messages[i : i + batch_size]
            logger.info(f"Async encoding [{i}/{len(messages)}] messages (bsz={batch_size})")
            res = await self.aencode(group_msg)
            data.extend(res)
            if task_id:
                self.embed_state[task_id]["progress"] = i + len(group_msg)

        if task_id:
            self.embed_state[task_id]["status"] = "completed"

        return data

    async def test_connection(self) -> tuple[bool, str]:
        try:
            await self.aencode(["Hello world"])
            return True, "连接正常"
        except Exception as e:
            error_msg = str(e)
            error_msg += f", maybe you can check the `{self.base_url}` end with /embeddings as examples."
            logger.error(error_msg)
            return False, error_msg


class OtherEmbedding(BaseEmbeddingModel):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def build_payload(self, message: list[str] | str) -> dict:
        return {"model": self.model, "input": message}

    def encode(self, message: list[str] | str) -> list[list[float]]:
        payload = self.build_payload(message)
        try:
            response = requests.post(self.base_url, json=payload, headers=self.headers, timeout=60)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or "data" not in result:
                raise ValueError(f"Embedding failed: Invalid response format {result}")
            return [item["embedding"] for item in result["data"]]
        except requests.RequestException as e:
            logger.error(f"Embedding request failed: {e}, {payload}")
            raise ValueError(f"Embedding request failed: {e}")

    async def aencode(self, message: list[str] | str) -> list[list[float]]:
        payload = self.build_payload(message)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.base_url, json=payload, headers=self.headers, timeout=60)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or "data" not in result:
                    raise ValueError(f"Embedding failed: Invalid response format {result}")
                return [item["embedding"] for item in result["data"]]
            except httpx.RequestError as e:
                raise ValueError(f"Embedding async request failed: {e}, {payload}, {self.base_url=}")


def get_embedding_model_info_by_id(model_id: str) -> dict:
    info = model_cache.get_model_info(model_id)
    if not info:
        raise ValueError(f"Unknown embedding model spec: {model_id}")
    if info.model_type != "embedding":
        raise ValueError(f"Model {model_id} is not an embedding model (type={info.model_type})")

    logger.info(f"Loaded embedding model info for {model_id}")
    return {
        "name": info.model_id,
        "display_name": info.display_name,
        "dimension": info.dimension,
        "base_url": info.base_url,
        "api_key": info.api_key,
        "model_id": info.spec,
        "batch_size": info.batch_size,
    }


def select_embedding_model(model_id: str):
    info = model_cache.get_model_info(model_id)
    if not info:
        raise ValueError(f"Unknown embedding model spec: {model_id}")

    if info.model_type != "embedding":
        raise ValueError(f"Model {model_id} is not an embedding model (type={info.model_type})")

    logger.info(f"Selecting embedding model: {model_id} (provider_type={info.provider_type})")
    return OtherEmbedding(
        model=info.model_id,
        base_url=info.base_url,
        api_key=info.api_key,
        dimension=info.dimension,
        batch_size=info.batch_size,
    )


async def test_embedding_model_status_by_spec(spec: str) -> dict:
    try:
        model = select_embedding_model(spec)
        success, message = await model.test_connection()
        return {
            "spec": spec,
            "status": "available" if success else "unavailable",
            "message": "连接正常" if success else message,
        }
    except Exception as e:
        logger.warning(f"测试 Embedding 模型状态失败 {spec}: {e}")
        return {"spec": spec, "status": "error", "message": str(e)}
