import pytest

from yuxi.modules.background_jobs.registry import get_job_definition


def test_unknown_handler_version_is_rejected() -> None:
    assert get_job_definition("knowledge_parse", 1).version == 1

    with pytest.raises(ValueError, match="Unsupported handler version"):
        get_job_definition("knowledge_parse", 2)

    with pytest.raises(ValueError, match="Unknown job type"):
        get_job_definition("no_such_job")
