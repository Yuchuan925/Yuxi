from openai import AsyncOpenAI
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from yuxi.services.model_cache import model_cache
from yuxi.utils import logger


class OpenAIBase:
    def __init__(self, api_key, base_url, model_name, **kwargs):
        self.model_params = kwargs.pop("model_params", {}) or {}
        self.api_key = api_key
        self.base_url = base_url
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, **kwargs)
        self.model_name = model_name
        self.info = kwargs

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((Exception,)),
        before_sleep=before_sleep_log(logger, log_level="WARNING"),
        reraise=True,
    )
    async def call(self, message, stream=False):
        if isinstance(message, str):
            messages = [{"role": "user", "content": message}]
        else:
            messages = message

        try:
            if stream:
                response = self._stream_response(messages)
            else:
                response = await self._get_response(messages)
        except Exception as e:
            err = (
                f"Error streaming response: {e}, URL: {self.base_url}, "
                f"API Key: {self.api_key[:5]}***, Model: {self.model_name}"
            )
            logger.error(err)
            raise Exception(err)

        return response

    async def _stream_response(self, messages):
        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            stream=True,
            **self.model_params,
        )
        async for chunk in response:
            if len(chunk.choices) > 0:
                yield chunk.choices[0].delta

    async def _get_response(self, messages):
        response = await self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            stream=False,
            **self.model_params,
        )
        return response.choices[0].message

    async def get_models(self):
        try:
            return await self.client.models.list(extra_query={"type": "text"})
        except Exception as e:
            logger.error(f"Error getting models: {e}")
            return []


class GeneralResponse:
    def __init__(self, content):
        self.content = content
        self.is_full = False


def select_model(model_spec: str, **kwargs) -> OpenAIBase:
    if not model_spec:
        raise ValueError("model_spec 不能为空")

    info = model_cache.get_model_info(model_spec)
    if not info:
        available = model_cache.get_all_specs("chat")
        available_ids = [item.spec for item in available[:10]]
        raise ValueError(f"未找到模型: '{model_spec}'。可用聊天模型 ({len(available)}): {available_ids}")

    if info.model_type != "chat":
        raise ValueError(f"Model {model_spec} is not a chat model (type={info.model_type})")

    logger.info(f"Selecting model: {model_spec} (provider_type={info.provider_type})")

    return OpenAIBase(
        api_key=info.api_key,
        base_url=info.base_url,
        model_name=info.model_id,
        **kwargs,
    )


async def test_chat_model_status_by_spec(spec: str) -> dict:
    try:
        logger.debug(f"Testing model status by spec: {spec}")
        model = select_model(model_spec=spec)

        test_messages = [{"role": "user", "content": "Say 1"}]
        response = await model.call(test_messages, stream=False)

        if response and response.content:
            return {"spec": spec, "status": "available", "message": "连接正常"}
        return {"spec": spec, "status": "unavailable", "message": "响应无效"}

    except Exception as e:
        logger.error(f"测试模型状态失败 {spec}: {e}")
        return {"spec": spec, "status": "error", "message": str(e)}


if __name__ == "__main__":
    pass
