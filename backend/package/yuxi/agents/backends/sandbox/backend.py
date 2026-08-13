from __future__ import annotations

import base64
import os
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import MAX_BINARY_BYTES, BaseSandbox
from deepagents.backends.utils import _get_file_type

from yuxi.utils.logging_config import logger
from yuxi.utils.paths import (
    OUTPUTS_DIR_NAME,
    UPLOADS_DIR_NAME,
    VIRTUAL_PATH_PREFIX,
    VIRTUAL_SKILLS_PATH,
    WORKSPACE_DIR_NAME,
)

from .provider import get_sandbox_provider, sandbox_id_for_thread, sandbox_provisioner_token
from .synchronizer import SandboxFileSynchronizer

_USER_DATA_ROOT = "/" + VIRTUAL_PATH_PREFIX.strip("/")
_WORKSPACE_ROOT = f"{_USER_DATA_ROOT}/{WORKSPACE_DIR_NAME}"
_UPLOADS_ROOT = f"{_USER_DATA_ROOT}/{UPLOADS_DIR_NAME}"
_OUTPUTS_ROOT = f"{_USER_DATA_ROOT}/{OUTPUTS_DIR_NAME}"
_SKILLS_ROOT = "/" + VIRTUAL_SKILLS_PATH.strip("/")
_READABLE_ROOTS = (_USER_DATA_ROOT, _SKILLS_ROOT)
_WRITABLE_ROOTS = (_WORKSPACE_ROOT, _OUTPUTS_ROOT)
_BINARY_PREVIEW_TOO_LARGE_ERROR = f"Binary file exceeds maximum preview size of {MAX_BINARY_BYTES} bytes"
_IMAGE_EXTENSIONS = frozenset({".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp"})
_DOCUMENT_EXTENSIONS = frozenset({".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx"})


def _normalize_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("path is required")
    if not raw.startswith("/"):
        raise ValueError("path must start with /")
    pure = PurePosixPath(raw)
    if ".." in pure.parts:
        raise ValueError("path traversal is not allowed")
    return str(pure)


def _is_same_or_child(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    if root == "/":
        return path == "/" or path.startswith("/")
    return path == root or path.startswith(f"{root}/")


def _path_overlaps_root(path: str, root: str) -> bool:
    return _is_same_or_child(path, root) or _is_same_or_child(root, path)


def _can_read_path(path: str) -> bool:
    return any(_is_same_or_child(path, root) for root in _READABLE_ROOTS)


def _can_list_path(path: str) -> bool:
    return any(_path_overlaps_root(path, root) for root in _READABLE_ROOTS)


def _can_write_path(path: str) -> bool:
    return any(_is_same_or_child(path, root) for root in _WRITABLE_ROOTS)


def _readable_search_paths(path: str) -> list[str]:
    if _can_read_path(path):
        return [path]
    return [root for root in _READABLE_ROOTS if _is_same_or_child(root, path)]


def _glob_for_search_root(pattern: str, root: str) -> str:
    bare_pattern = str(pattern or "*").lstrip("/")
    bare_root = root.strip("/")
    if bare_pattern == bare_root:
        return "*"
    root_prefix = f"{bare_root}/"
    if bare_pattern.startswith(root_prefix):
        return bare_pattern[len(root_prefix) :] or "*"
    return pattern


def _filter_readable_infos(infos: list[FileInfo]) -> list[FileInfo]:
    result: list[FileInfo] = []
    for info in infos:
        try:
            path = _normalize_path(info.get("path", ""))
        except ValueError:
            continue
        if _can_list_path(path):
            result.append(info)
    return result


def _filter_readable_matches(matches: list[GrepMatch]) -> list[GrepMatch]:
    result: list[GrepMatch] = []
    for match in matches:
        try:
            path = _normalize_path(match.get("path", ""))
        except ValueError:
            continue
        if _can_read_path(path):
            result.append(match)
    return result


def _permission_error(operation: str, path: str) -> str:
    return f"permission denied for {operation} on '{path}'"


def _describe_read_error(file_path: str, exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"Error: File '{file_path}' not found"
    if isinstance(exc, IsADirectoryError):
        return f"Error: Path '{file_path}' is a directory"
    if isinstance(exc, PermissionError):
        return f"Error: Access denied for '{file_path}'"
    if isinstance(exc, ValueError):
        return f"Error: Invalid path '{file_path}': {exc}"
    detail = str(exc).strip()
    if detail:
        return f"Error: Failed to read '{file_path}': {detail}"
    return f"Error: Failed to read '{file_path}'"


def _is_missing_file_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code == 404 or getattr(response, "status_code", None) == 404:
        return True

    detail = str(exc).lower()
    return "status_code: 404" in detail or "file does not exist" in detail


def _looks_like_binary(content: bytes) -> bool:
    if not content:
        return False
    if b"\x00" in content:
        return True
    try:
        content.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _is_utf8_decode_failure(exc: Exception) -> bool:
    detail = str(exc).lower()
    return "utf-8" in detail and "can't decode" in detail


class ProvisionerSandboxBackend(BaseSandbox):
    def __init__(
        self,
        thread_id: str,
        *,
        uid: str,
        readable_skills: list[str] | None = None,
        skill_sources: dict[str, str] | None = None,
        file_thread_id: str | None = None,
        skills_thread_id: str | None = None,
        inherit_env: bool = True,
    ):
        self._thread_id = str(thread_id or "").strip()
        if not self._thread_id:
            raise ValueError("thread_id is required for ProvisionerSandboxBackend")
        self._file_thread_id = str(file_thread_id or self._thread_id).strip()
        if not self._file_thread_id:
            raise ValueError("file_thread_id is required for ProvisionerSandboxBackend")
        self._skills_thread_id = str(skills_thread_id or self._thread_id).strip()
        if not self._skills_thread_id:
            raise ValueError("skills_thread_id is required for ProvisionerSandboxBackend")
        self._uid = str(uid or "").strip()
        if not self._uid:
            raise ValueError("uid is required for ProvisionerSandboxBackend")

        self._readable_skills = list(readable_skills or [])
        self._skill_sources = dict(skill_sources or {})
        self._inherit_env = inherit_env
        self._provider = get_sandbox_provider()
        self._id = sandbox_id_for_thread(self._file_thread_id, self._skills_thread_id, uid=self._uid)
        self._client: Any | None = None
        self._client_url: str | None = None
        self._command_timeout_seconds = int(os.getenv("SANDBOX_EXEC_TIMEOUT_SECONDS") or 180)
        self._max_output_bytes = int(os.getenv("SANDBOX_MAX_OUTPUT_BYTES") or 262_144)
        self._synchronizer = SandboxFileSynchronizer(
            uid=self._uid,
            file_thread_id=self._file_thread_id,
            skills_thread_id=self._skills_thread_id,
            sandbox_id=self._id,
        )

    @property
    def id(self) -> str:
        return self._id

    def _build_client(self, sandbox_url: str):
        try:
            from agent_sandbox import Sandbox as AgentSandboxClient
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "agent-sandbox is required. Install dependency `agent-sandbox` in the docker image."
            ) from exc

        return AgentSandboxClient(
            base_url=sandbox_url,
            headers={"Authorization": f"Bearer {sandbox_provisioner_token()}"},
            timeout=self._command_timeout_seconds,
        )

    def _get_client(self) -> Any:
        connection = self._provider.get(
            self._thread_id,
            uid=self._uid,
            create_if_missing=True,
            file_thread_id=self._file_thread_id,
            skills_thread_id=self._skills_thread_id,
            inherit_env=self._inherit_env,
        )
        if connection is None:
            raise RuntimeError(f"sandbox is unavailable for thread {self._thread_id}")

        if self._client is None or self._client_url != connection.sandbox_url:
            self._client = self._build_client(connection.sandbox_url)
            self._client_url = connection.sandbox_url
            if hasattr(getattr(self._client, "file", None), "list_path"):
                with self._synchronizer.operation(
                    self._client,
                    timeout_seconds=self._command_timeout_seconds,
                ):
                    pass

        return self._client

    @contextmanager
    def _file_operation(self, *, timeout: int | None = None, scopes: set[str] | None = None):
        """覆盖 refresh、Sandbox 变更与 sync_back 的完整同步操作窗口。"""
        client = self._get_client()
        if not hasattr(getattr(client, "file", None), "list_path"):
            yield client
            return
        operation_timeout = timeout if timeout is not None else self._command_timeout_seconds
        with self._synchronizer.operation(client, timeout_seconds=operation_timeout):
            self._synchronizer.refresh(client, scopes=scopes)
            yield client

    @staticmethod
    def _scope_for_path(path: str) -> set[str]:
        """返回虚拟路径对应的 FileStore scope。"""
        if _is_same_or_child(path, _WORKSPACE_ROOT):
            return {"workspace"}
        if _is_same_or_child(path, _UPLOADS_ROOT):
            return {"uploads"}
        if _is_same_or_child(path, _OUTPUTS_ROOT):
            return {"outputs"}
        if _is_same_or_child(path, _SKILLS_ROOT):
            return {"skills"}
        return {"workspace", "uploads", "outputs", "skills"}

    def _read_binary(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        """Read file content from the sandbox file API and normalize it to bytes.

        The underlying API returns plain text by default and may include an
        explicit `encoding="base64"` marker for binary payloads. This helper is
        the single normalization point used by read() and edit().
        """
        start_line = max(0, int(offset))
        end_line = start_line + int(limit) if limit is not None else None

        result = self._get_client().file.read_file(
            file=path,
            start_line=start_line,
            end_line=end_line,
        )

        content = result.data.content
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        if not isinstance(content, str):
            return str(content).encode("utf-8")

        encoding = getattr(result.data, "encoding", None)
        if isinstance(encoding, str) and encoding.lower() == "base64":
            return base64.b64decode(content, validate=True)
        return content.encode("utf-8")

    def _file_size_bytes(self, path: str) -> int:
        path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
        command = (
            'python3 -c "'
            "import base64, os, stat; "
            f"path = base64.b64decode('{path_b64}').decode('utf-8'); "
            "st = os.stat(path); "
            "print(st.st_size if stat.S_ISREG(st.st_mode) else -1)"
            '"'
        )
        result = self.execute(command)
        if result.exit_code not in (0, None):
            detail = (result.output or "").strip()
            raise RuntimeError(detail or f"failed to stat '{path}'")

        output = (result.output or "").strip().splitlines()
        try:
            size = int(output[-1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"failed to stat '{path}'") from exc
        if size < 0:
            raise IsADirectoryError(path)
        return size

    def _read_file_base64(self, path: str) -> str:
        path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
        output_path = f"/tmp/yuxi-read-file-{uuid.uuid4().hex}.b64"
        output_path_b64 = base64.b64encode(output_path.encode("utf-8")).decode("ascii")
        command = (
            'python3 -c "'
            "import base64; "
            f"path = base64.b64decode('{path_b64}').decode('utf-8'); "
            f"output_path = base64.b64decode('{output_path_b64}').decode('utf-8'); "
            "open(output_path, 'w').write(base64.b64encode(open(path, 'rb').read()).decode('ascii'))"
            '"'
        )
        client = self._get_client()
        try:
            result = client.shell.exec_command(
                command=command,
                timeout=self._command_timeout_seconds,
                truncate=False,
            )
            output = result.data.output or ""
            if result.data.exit_code not in (0, None):
                raise RuntimeError(output.strip() or f"failed to read '{path}'")

            content = self._read_binary(output_path).decode("ascii").strip()
            base64.b64decode(content, validate=True)
            return content
        finally:
            with suppress(Exception):
                client.shell.exec_command(command=f"rm -f {output_path}", timeout=10)

    def _read_base64_file(self, path: str) -> ReadResult:
        if self._file_size_bytes(path) > MAX_BINARY_BYTES:
            return ReadResult(error=_BINARY_PREVIEW_TOO_LARGE_ERROR)
        return ReadResult(file_data={"content": self._read_file_base64(path), "encoding": "base64"})

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read allowed file content via the sandbox file API."""
        try:
            normalized_path = _normalize_path(file_path)
        except Exception as exc:  # noqa: BLE001
            return ReadResult(error=f"Invalid path '{file_path}': {exc}")
        if not _can_read_path(normalized_path):
            return ReadResult(error=_permission_error("read", normalized_path))

        document_read_error = (
            "read_file does not support PDF or Office documents. "
            "Use ocr_parse_file to convert the file to Markdown first."
        )
        binary_read_error = "read_file only supports UTF-8 text and image files. This file type is not supported."
        try:
            with self._file_operation(scopes=self._scope_for_path(normalized_path)):
                extension = PurePosixPath(normalized_path).suffix.lower()
                if extension in _IMAGE_EXTENSIONS:
                    return self._read_base64_file(normalized_path)
                if extension in _DOCUMENT_EXTENSIONS:
                    self._file_size_bytes(normalized_path)
                    return ReadResult(error=document_read_error)
                if _get_file_type(normalized_path) != "text":
                    self._file_size_bytes(normalized_path)
                    return ReadResult(error=binary_read_error)

                try:
                    content = self._read_binary(normalized_path, offset=offset, limit=limit)
                except Exception as exc:  # noqa: BLE001
                    if not _is_utf8_decode_failure(exc):
                        raise
                    return ReadResult(error=binary_read_error)

            if not _looks_like_binary(content):
                return ReadResult(file_data={"content": content.decode("utf-8"), "encoding": "utf-8"})

            return ReadResult(error=binary_read_error)
        except Exception as exc:  # noqa: BLE001
            error = _describe_read_error(file_path, exc)
            return ReadResult(error=error.removeprefix("Error: "))

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Execute a shell command in the sandbox.

        Output is normalized to text and truncated to the configured maximum
        payload size before being returned.
        """
        try:
            with self._file_operation(timeout=timeout) as client:
                kwargs: dict[str, Any] = {"command": command}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                    kwargs["hard_timeout"] = timeout
                    kwargs["request_options"] = {"timeout_in_seconds": timeout}
                try:
                    result = client.shell.exec_command(**kwargs)
                finally:
                    if hasattr(getattr(client, "file", None), "list_path"):
                        self._synchronizer.sync_back(client)

            output = result.data.output or ""
            exit_code = result.data.exit_code

            truncated = False
            encoded = output.encode("utf-8", errors="ignore")
            if len(encoded) > self._max_output_bytes:
                output = encoded[: self._max_output_bytes].decode("utf-8", errors="ignore")
                truncated = True

            return ExecuteResponse(
                output=output,
                exit_code=exit_code if isinstance(exit_code, int) else None,
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Sandbox execute failed for thread {self._thread_id}: {exc}")
            return ExecuteResponse(output=f"Error: {exc}", exit_code=1, truncated=False)

    def ls(self, path: str) -> LsResult:
        """List direct children of an allowed sandbox path with lightweight metadata."""
        try:
            normalized_path = _normalize_path(path)
        except Exception as exc:  # noqa: BLE001
            return LsResult(error=f"Invalid path '{path}': {exc}")
        if not _can_list_path(normalized_path):
            return LsResult(error=_permission_error("read", normalized_path))

        try:
            with self._file_operation(scopes=self._scope_for_path(normalized_path)) as client:
                result = client.file.list_path(path=normalized_path, recursive=False, include_size=True)
        except Exception as exc:  # noqa: BLE001
            return LsResult(error=str(exc) or f"Failed to list '{path}'")

        entries = result.data.files or []
        infos: list[FileInfo] = []
        for entry in entries:
            info: FileInfo = {"path": entry.path, "is_dir": entry.is_directory}
            size = entry.size
            if isinstance(size, int):
                info["size"] = size
            modified_time = entry.modified_time
            if modified_time:
                if isinstance(modified_time, str) and modified_time.isdigit():
                    info["modified_at"] = datetime.fromtimestamp(int(modified_time)).isoformat()
                elif isinstance(modified_time, str):
                    try:
                        info["modified_at"] = datetime.fromisoformat(modified_time).isoformat()
                    except ValueError:
                        info["modified_at"] = modified_time
                elif isinstance(modified_time, (int, float)):
                    info["modified_at"] = datetime.fromtimestamp(modified_time).isoformat()
            infos.append(info)
        return LsResult(entries=_filter_readable_infos(infos))

    def write(self, file_path: str, content: str) -> WriteResult:
        """Write a new text file.

        This method is intentionally text-only. Binary payloads should go through
        upload_files(), which uses base64 encoding for the sandbox file API.
        """
        try:
            normalized_path = _normalize_path(file_path)
        except Exception as exc:  # noqa: BLE001
            return WriteResult(error=f"Error: Invalid path '{file_path}': {exc}")
        if not _can_write_path(normalized_path):
            return WriteResult(error=f"Error: {_permission_error('write', normalized_path)}")
        if not isinstance(content, str):
            return WriteResult(error="Error: write() only supports text content; use upload_files() for binary data")
        try:
            with self._file_operation() as client:
                try:
                    self._read_binary(normalized_path)
                except Exception:  # noqa: BLE001
                    pass
                else:
                    return WriteResult(error=f"Error: File '{file_path}' already exists")
                result = client.file.write_file(file=normalized_path, content=content)
                if not result.success:
                    return WriteResult(error=result.message or f"Failed to write file '{file_path}'")
                if hasattr(getattr(client, "file", None), "list_path"):
                    self._synchronizer.sync_back(client)
        except Exception as exc:  # noqa: BLE001
            return WriteResult(error=str(exc) or f"Failed to write file '{file_path}'")
        return WriteResult(path=normalized_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """Edit an existing text file by replacing string content.

        This method operates on UTF-8-decoded text content only. Binary files
        are not supported here and should be handled via download/upload flows.
        """
        try:
            normalized_path = _normalize_path(file_path)
        except Exception as exc:  # noqa: BLE001
            return EditResult(error=f"Error: Invalid path '{file_path}': {exc}")
        if not _can_write_path(normalized_path):
            return EditResult(error=f"Error: {_permission_error('write', normalized_path)}")

        try:
            with self._file_operation() as client:
                try:
                    text = self._read_binary(normalized_path).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    return EditResult(error=f"Error: File '{file_path}' not found")
                count = text.count(old_string)
                if count == 0:
                    return EditResult(error=f"Error: String not found in file: '{old_string}'")
                if count > 1 and not replace_all:
                    return EditResult(
                        error=(
                            f"Error: String '{old_string}' appears multiple times. "
                            "Use replace_all=True to replace all occurrences."
                        )
                    )
                replace_mode = "ALL" if replace_all else "FIRST"
                result = client.file.str_replace_editor(
                    command="str_replace",
                    path=normalized_path,
                    old_str=old_string,
                    new_str=new_string,
                    replace_mode=replace_mode,
                )
                if not result.success:
                    return EditResult(error=result.message or f"Error editing file '{file_path}'")
                if hasattr(getattr(client, "file", None), "list_path"):
                    self._synchronizer.sync_back(client)
        except Exception as exc:  # noqa: BLE001
            return EditResult(error=f"Error editing file: {exc}")
        return EditResult(path=normalized_path, occurrences=count if replace_all else 1)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """Search allowed sandbox paths for literal text."""
        try:
            normalized_path = _normalize_path(path or "/")
        except Exception as exc:  # noqa: BLE001
            return GrepResult(error=f"Invalid path '{path or '/'}': {exc}")

        search_paths = _readable_search_paths(normalized_path)
        if not search_paths:
            return GrepResult(error=_permission_error("read", normalized_path))

        matches: list[GrepMatch] = []
        scopes = set().union(*(self._scope_for_path(search_path) for search_path in search_paths))
        with self._file_operation(scopes=scopes):
            for search_path in search_paths:
                result = super().grep(pattern=pattern, path=search_path, glob=glob)
                if result.error:
                    return result
                matches.extend(result.matches or [])
        return GrepResult(matches=_filter_readable_matches(matches))

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        """Return files matching a glob pattern under allowed sandbox paths."""
        try:
            normalized_path = _normalize_path(path)
        except Exception as exc:  # noqa: BLE001
            return GlobResult(error=f"Invalid path '{path}': {exc}")
        if ".." in PurePosixPath(str(pattern or "")).parts:
            return GlobResult(error="Invalid glob pattern: path traversal is not allowed")

        search_paths = _readable_search_paths(normalized_path)
        if not search_paths:
            return GlobResult(error=_permission_error("read", normalized_path))

        infos: list[FileInfo] = []
        scopes = set().union(*(self._scope_for_path(search_path) for search_path in search_paths))
        try:
            with self._file_operation(scopes=scopes) as client:
                for search_path in search_paths:
                    result = client.file.find_files(
                        path=search_path,
                        glob=_glob_for_search_root(pattern, search_path),
                    )
                    for file_path in result.data.files or []:
                        infos.append({"path": file_path})
        except Exception as exc:  # noqa: BLE001
            return GlobResult(error=str(exc) or f"Failed to glob '{path}'")
        infos = _filter_readable_infos(infos)
        infos.sort(key=lambda item: item.get("path", ""))
        return GlobResult(matches=infos)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload binary or text file payloads via the sandbox file API.

        Contents are base64-encoded before calling the remote write_file API so
        arbitrary bytes can be transferred safely.
        """
        responses: list[FileUploadResponse] = []
        writable_files: list[tuple[str, bytes]] = []
        for path, content in files:
            try:
                normalized_path = _normalize_path(path)
            except ValueError:
                responses.append(FileUploadResponse(path=str(path), error="invalid_path"))
                continue
            if not _can_write_path(normalized_path):
                responses.append(FileUploadResponse(path=normalized_path, error="permission_denied"))
                continue
            writable_files.append((normalized_path, content))
        if not writable_files:
            return responses

        try:
            with self._file_operation() as client:
                for normalized_path, content in writable_files:
                    try:
                        result = client.file.write_file(
                            file=normalized_path,
                            content=base64.b64encode(content).decode("ascii"),
                            encoding="base64",
                        )
                        if not result.success:
                            raise Exception(result.message or "Upload failed")
                        responses.append(FileUploadResponse(path=normalized_path, error=None))
                    except PermissionError:
                        responses.append(FileUploadResponse(path=normalized_path, error="permission_denied"))
                    except IsADirectoryError:
                        responses.append(FileUploadResponse(path=normalized_path, error="is_directory"))
                    except FileNotFoundError:
                        responses.append(FileUploadResponse(path=normalized_path, error="file_not_found"))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"Upload to sandbox failed for {normalized_path}: {exc}")
                        responses.append(FileUploadResponse(path=normalized_path, error="invalid_path"))
                if hasattr(getattr(client, "file", None), "list_path"):
                    self._synchronizer.sync_back(client)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Sandbox upload operation failed for thread {self._thread_id}: {exc}")
            responses.extend(
                FileUploadResponse(path=path, error=f"sync_failed: {exc}") for path, _ in writable_files
            )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download file payloads as raw bytes from the sandbox file API."""
        responses: list[FileDownloadResponse | None] = [None] * len(paths)
        normalized_paths: list[tuple[int, str]] = []
        for index, path in enumerate(paths):
            try:
                normalized_path = _normalize_path(path)
                if not _can_read_path(normalized_path):
                    responses[index] = FileDownloadResponse(
                        path=normalized_path,
                        content=None,
                        error="permission_denied",
                    )
                    continue
                normalized_paths.append((index, normalized_path))
            except ValueError:
                responses[index] = FileDownloadResponse(path=str(path), content=None, error="invalid_path")

        scopes = (
            set().union(*(self._scope_for_path(path) for _, path in normalized_paths)) if normalized_paths else set()
        )
        try:
            with self._file_operation(scopes=scopes) as client:
                for index, normalized_path in normalized_paths:
                    try:
                        content = b"".join(
                            client.file.download_file(
                                path=normalized_path,
                                request_options={"timeout_in_seconds": self._command_timeout_seconds},
                            )
                        )
                        responses[index] = FileDownloadResponse(path=normalized_path, content=content, error=None)
                    except PermissionError:
                        responses[index] = FileDownloadResponse(
                            path=normalized_path,
                            content=None,
                            error="permission_denied",
                        )
                    except IsADirectoryError:
                        responses[index] = FileDownloadResponse(
                            path=normalized_path,
                            content=None,
                            error="is_directory",
                        )
                    except FileNotFoundError:
                        responses[index] = FileDownloadResponse(
                            path=normalized_path,
                            content=None,
                            error="file_not_found",
                        )
                    except Exception as exc:  # noqa: BLE001
                        if _is_missing_file_error(exc):
                            responses[index] = FileDownloadResponse(
                                path=normalized_path,
                                content=None,
                                error="file_not_found",
                            )
                            continue
                        logger.warning(f"Download from sandbox failed for {normalized_path}: {exc}")
                        responses[index] = FileDownloadResponse(
                            path=normalized_path,
                            content=None,
                            error=f"read_failed: {exc}",
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Download refresh failed: {exc}")
            for index, path in normalized_paths:
                responses[index] = FileDownloadResponse(path=path, content=None, error=f"read_failed: {exc}")
        return [response for response in responses if response is not None]
