import pytest

from yuxi.storage.filestore import (
    FileStoreError,
    normalize_key,
    normalize_prefix,
    shared_skill_key,
    shared_skills_prefix,
    thread_output_key,
    thread_skill_key,
    thread_upload_key,
    user_workspace_key,
)


def test_key_helpers_build_canonical_namespaces():
    assert thread_upload_key("thread-1", "attachments/report.pdf") == "threads/thread-1/uploads/attachments/report.pdf"
    assert thread_output_key("thread-1", "charts/result.png") == "threads/thread-1/outputs/charts/result.png"
    assert user_workspace_key("user-1", "agents/AGENTS.md") == "users/user-1/workspace/agents/AGENTS.md"
    assert shared_skills_prefix() == "skills/"
    assert shared_skill_key("pdf", "SKILL.md") == "skills/pdf/SKILL.md"
    assert thread_skill_key("thread-1", "pdf/SKILL.md") == "threads/thread-1/skills/pdf/SKILL.md"


@pytest.mark.parametrize("key", ["../secret", "a/../secret", "/absolute", "a//b", "a/./b", "a\\b", ""])
def test_normalize_key_rejects_traversal_and_ambiguous_paths(key):
    with pytest.raises(FileStoreError):
        normalize_key(key)


def test_key_helpers_validate_identifiers_and_relative_paths():
    with pytest.raises(FileStoreError):
        thread_upload_key("../other-thread", "file.txt")
    with pytest.raises(FileStoreError):
        user_workspace_key("user-1", "../../secret")


def test_normalize_prefix_accepts_empty_and_directory_style_prefixes():
    assert normalize_prefix("") == ""
    assert normalize_prefix("threads/thread-1/") == "threads/thread-1/"

    with pytest.raises(FileStoreError):
        normalize_prefix("threads/../secret/")
