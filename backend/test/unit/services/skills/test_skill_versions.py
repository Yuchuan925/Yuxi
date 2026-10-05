"""不可变内容索引的路径边界。"""

import pytest
from yuxi.modules.extensions.skills.package import validated_shared_skill_parts


@pytest.mark.parametrize(
    "path", ["packages/other/" + "a" * 32, "packages/demo/../escape", "packages/demo/" + "A" * 32, "shared/other"]
)
def test_content_reference_cannot_escape_own_skill(path):
    """异常持久引用在访问文件前拒绝。"""
    with pytest.raises(ValueError, match="来源目录非法"):
        validated_shared_skill_parts("demo", path)
