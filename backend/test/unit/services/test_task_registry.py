import pytest

from yuxi.modules.tasks.registry import get_task_definition


def test_unknown_handler_version_is_rejected() -> None:
    assert get_task_definition("knowledge_parse", 1).version == 1

    with pytest.raises(ValueError, match="Unsupported handler version"):
        get_task_definition("knowledge_parse", 2)

    with pytest.raises(ValueError, match="Unknown task type"):
        get_task_definition("no_such_task")
