import os
import uuid
from pathlib import Path

from loguru import logger
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from imbue.system_interface.models import AttachmentError

# Chat attachments live under a top-level, gitignored ``uploads/`` directory.
# They are kept OUT of ``runtime/`` because a user upload can be arbitrarily
# large and of any format, which does not belong in the runtime-backed content;
# the directory is gitignored so uploads are never committed to the main branch.
_UPLOADS_SUBPATH = Path("uploads")

_DEFAULT_UPLOAD_FILENAME = "upload"


def get_uploads_directory() -> Path:
    """Return the directory where chat attachments are stored on the agent VM.

    Resolved under the primary agent's work dir (the workspace repo root, where
    ``runtime/`` lives), falling back to the current working directory when
    ``MNGR_AGENT_WORK_DIR`` is unset -- mirroring how the rest of the app
    resolves runtime paths.
    """
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base_directory = Path(work_dir) if work_dir else Path.cwd()
    return base_directory / _UPLOADS_SUBPATH


def sanitize_upload_filename(original_filename: str) -> str:
    """Return a filesystem-safe basename for a user-provided filename."""
    safe_name = secure_filename(original_filename)
    if not safe_name:
        return _DEFAULT_UPLOAD_FILENAME
    return safe_name


def store_uploaded_file(uploads_directory: Path, original_filename: str, file_storage: FileStorage) -> Path:
    """Store an uploaded file under a fresh unique subdirectory and return its path.

    Each upload gets its own subdirectory so two files with the same name never
    collide. Raises AttachmentError if the file cannot be written.
    """
    unique_subdirectory = uploads_directory / uuid.uuid4().hex
    safe_name = sanitize_upload_filename(original_filename)
    destination_path = unique_subdirectory / safe_name
    try:
        unique_subdirectory.mkdir(parents=True, exist_ok=True)
        file_storage.save(destination_path)
    except OSError as e:
        raise AttachmentError(f"Could not store uploaded file '{original_filename}'") from e
    return destination_path


def resolve_upload_path(uploads_directory: Path, relative_path: str) -> Path | None:
    """Resolve a client-supplied relative path to a file inside the uploads dir.

    Returns the resolved path only when it stays within the uploads directory
    and points at an existing file; returns None for traversal attempts or
    misses.
    """
    base_directory = uploads_directory.resolve()
    candidate_path = (uploads_directory / relative_path).resolve()
    if not candidate_path.is_relative_to(base_directory):
        return None
    if not candidate_path.is_file():
        return None
    return candidate_path


def delete_upload(uploads_directory: Path, relative_path: str) -> None:
    """Delete a stored upload (and its now-empty subdirectory) if present.

    Confined to the uploads directory; a traversal attempt or a miss is a no-op
    so removal is idempotent.
    """
    resolved_path = resolve_upload_path(uploads_directory, relative_path)
    if resolved_path is None:
        return
    try:
        resolved_path.unlink()
        parent_directory = resolved_path.parent
        if parent_directory != uploads_directory.resolve():
            parent_directory.rmdir()
    except OSError as e:
        # Best-effort cleanup: a concurrent delete or a non-empty subdirectory
        # is not worth failing the request over.
        logger.debug("Skipped attachment cleanup for {}: {}", relative_path, e)
