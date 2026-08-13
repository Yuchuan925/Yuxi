import pytest

from yuxi.storage.filestore import FileStoreError, LocalFileStore, S3FileStore, get_file_store, reset_file_store


@pytest.fixture(autouse=True)
def reset_store_singleton():
    reset_file_store()
    yield
    reset_file_store()


def test_factory_defaults_to_s3_and_falls_back_to_minio_environment(monkeypatch):
    monkeypatch.delenv("FILESTORE_BACKEND", raising=False)
    monkeypatch.delenv("FILESTORE_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("FILESTORE_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("FILESTORE_S3_SECRET_KEY", raising=False)
    monkeypatch.setenv("MINIO_URI", "http://minio:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "access")
    monkeypatch.setenv("MINIO_SECRET_KEY", "secret")

    store = get_file_store()

    assert isinstance(store, S3FileStore)
    assert store.bucket == "yuxi-filestore"
    assert store._client_options["endpoint_url"] == "http://minio:9000"
    assert store._client_options["aws_access_key_id"] == "access"
    assert store._client_options["aws_secret_access_key"] == "secret"


def test_factory_returns_process_singleton(monkeypatch):
    monkeypatch.setenv("FILESTORE_BACKEND", "s3")

    first = get_file_store()
    monkeypatch.setenv("FILESTORE_BACKEND", "local")
    second = get_file_store()

    assert second is first


def test_reset_file_store_recreates_singleton(monkeypatch):
    monkeypatch.setenv("FILESTORE_BACKEND", "s3")
    first = get_file_store()

    reset_file_store()
    second = get_file_store()

    assert second is not first


def test_factory_builds_local_store(monkeypatch, tmp_path):
    monkeypatch.setenv("FILESTORE_BACKEND", "local")
    monkeypatch.setenv("FILESTORE_ALLOW_LOCAL", "true")
    monkeypatch.setenv("FILESTORE_LOCAL_ROOT", str(tmp_path))

    store = get_file_store()

    assert isinstance(store, LocalFileStore)
    assert store.root == tmp_path.resolve()


def test_factory_rejects_local_store_in_standard_docker_topology(monkeypatch):
    monkeypatch.setenv("RUNNING_IN_DOCKER", "true")
    monkeypatch.setenv("FILESTORE_BACKEND", "local")
    monkeypatch.delenv("FILESTORE_ALLOW_LOCAL", raising=False)

    with pytest.raises(FileStoreError, match="不支持 local FileStore"):
        get_file_store()
