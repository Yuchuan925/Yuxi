from pathlib import Path

from yuxi.config import get_runtime_dir, get_save_dir


def test_runtime_directory_uses_explicit_environment(monkeypatch, tmp_path: Path):
    """显式运行目录应由当前进程环境拥有。"""
    runtime_dir = tmp_path / "runtime" / "api"
    monkeypatch.setenv("YUXI_RUNTIME_DIR", str(runtime_dir))

    assert get_runtime_dir() == runtime_dir


def test_runtime_directory_default_does_not_fall_back_to_save_dir(monkeypatch, tmp_path: Path):
    """缺少配置时，可丢弃运行数据不得回落到持久保存目录。"""
    save_dir = tmp_path / "saves"
    monkeypatch.setenv("SAVE_DIR", str(save_dir))
    monkeypatch.delenv("YUXI_RUNTIME_DIR", raising=False)

    runtime_dir = get_runtime_dir()

    assert runtime_dir != get_save_dir()
    assert save_dir not in runtime_dir.parents
    assert runtime_dir.name.startswith("yuxi-runtime-")
