from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yuxi.workers.background_job_context import BackgroundJobContext

JobHandler = Callable[["BackgroundJobContext"], Awaitable[object]]
JobSuccessHandler = Callable[[object, object, object], Awaitable[None]]
JobFailureHandler = Callable[[object, object, str], Awaitable[None]]


@dataclass(frozen=True)
class JobDefinition:
    """描述可持久重建的任务 Handler。"""

    job_type: str
    module: str
    function: str
    success_function: str | None = None
    failure_function: str | None = None
    version: int = 1

    def load_handler(self) -> JobHandler:
        return self._load(self.function, "handler")

    def load_success_handler(self) -> JobSuccessHandler | None:
        return self._load(self.success_function, "success handler") if self.success_function is not None else None

    def load_failure_handler(self) -> JobFailureHandler | None:
        return self._load(self.failure_function, "failure handler") if self.failure_function is not None else None

    def _load(self, function: str, label: str):
        """惰性加载并校验一个注册 Handler。"""
        handler = getattr(import_module(self.module), function)
        if not callable(handler):
            raise TypeError(f"Job {label} is not callable: {self.module}:{function}")
        return handler


def get_job_definition(job_type: str, handler_version: int = 1) -> JobDefinition:
    """返回当前 shipping JobDefinition，并拒绝未知类型或版本。"""
    from yuxi.bootstrap.background_job_handlers import JOB_DEFINITIONS

    definition = JOB_DEFINITIONS.get(job_type)
    if definition is None:
        raise ValueError(f"Unknown job type: {job_type}")
    if definition.version != handler_version:
        raise ValueError(f"Unsupported handler version for {job_type}: {handler_version}; expected {definition.version}")
    return definition
