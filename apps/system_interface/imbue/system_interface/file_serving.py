"""Serve agent-authored files referenced by their absolute on-disk path.

An agent (Claude Code) running in this container can write files and read them
back, but the browser rendering the chat cannot reach the container's
filesystem. Markdown like ``![chart](/mngr/code/runtime/chat-images/chart.png)``
(an inline image) or ``[report](/mngr/code/runtime/chat-files/report.pdf)`` (a
download link) makes the browser issue an HTTP GET for that path; the system
interface runs in the same container as the agent, so it answers the GET by
streaming the file's bytes. The absolute on-disk path therefore doubles as the
URL -- no rewriting, no dedicated directory, no separate server.

This hangs off the single-page-app catch-all (see ``server._index_catch_all``):

- An image file is served inline so it renders in the chat.
- Any other existing file is served as an attachment, so a plain markdown link
  downloads it rather than rendering/executing it in the chat's own origin.
- A path carrying an image extension with no file behind it 404s, so a typo'd
  image renders a broken image rather than the app shell.
- A path that matches no file on disk returns ``None``, so the caller falls
  through to the app shell and client-side routing is unaffected.
"""

from pathlib import Path

from flask import Response
from flask import send_file

# Long-lived caching for inline images served from this direct-path route,
# which remains for non-chat fetches (e.g. opening an image URL in a tab):
# agents are instructed (see the show-files-in-chat skill) to give each image
# a unique filename, so a one-year max-age plus ``immutable`` lets the browser
# skip revalidation. Chat markdown images are rewritten by the frontend to the
# change-detecting route (see ``chat_image_timestamps``), which serves with
# caching disabled instead.
_IMAGE_CACHE_MAX_AGE_SECONDS = 31_536_000

IMMUTABLE_IMAGE_CACHE_CONTROL = f"public, max-age={_IMAGE_CACHE_MAX_AGE_SECONDS}, immutable"

# Image extensions served inline, each mapped to an explicit Content-Type so the
# wire result does not depend on the host's mimetypes registry (macOS and Linux
# disagree on, e.g., webp). Suffixes are matched case-insensitively.
_IMAGE_EXTENSION_TO_MIME_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

_SVG_EXTENSION = ".svg"

# An SVG loaded via a chat ``<img>`` never executes its scripts, but a user who
# opens the image URL directly in a tab would render it as a document. Lock that
# path down: no scripts, objects, or external loads; inline styles only. Paired
# with nosniff so the declared type is honored.
_SVG_CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'"


def image_mime_type_for_path(url_path: str) -> str | None:
    """Return the image Content-Type for ``url_path``, or None if it is not an image path."""
    suffix = Path(url_path).suffix.lower()
    return _IMAGE_EXTENSION_TO_MIME_TYPE.get(suffix)


def serve_inline_image(
    file_path: Path, mime_type: str, cache_control: str = IMMUTABLE_IMAGE_CACHE_CONTROL
) -> Response:
    """Stream an image so it renders inline in the chat.

    Public because the chat-image change-detection endpoint (``server``)
    serves the same files with the same hardening but caching disabled, so a
    message's image is refetched -- and re-verified -- on every render.
    """
    response = send_file(file_path, mimetype=mime_type)
    response.headers["Cache-Control"] = cache_control
    if file_path.suffix.lower() == _SVG_EXTENSION:
        response.headers["Content-Security-Policy"] = _SVG_CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# HTTP status the chat-image change-detection endpoint returns when a referenced
# file has changed after its message was posted. Deliberately NOT an image: the
# frontend catches the resulting <img> load error, re-fetches to read this
# status, and swaps the image for a plain text notice. Serving an image
# placeholder instead would make the notice itself an openable/downloadable
# image, which it must never be.
CHANGED_FILE_STATUS = 409

# The user-facing notice. Returned as the 409 body so the frontend can render
# the backend's exact wording (single source of truth) rather than duplicating
# it, falling back to its own copy only if the body is empty.
CHANGED_FILE_MESSAGE = "This file has been changed. Please revert your workspace or ask your agent to recover it."


def serve_changed_file_notice() -> Response:
    """Return the non-image response marking a chat file that has changed.

    Carries ``CHANGED_FILE_STATUS`` and a plain-text body, never image bytes, so
    the changed state can only ever render as a non-interactive notice.
    """
    response = Response(CHANGED_FILE_MESSAGE, status=CHANGED_FILE_STATUS, mimetype="text/plain")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def serve_download(file_path: Path, cache_control: str | None = None) -> Response:
    """Stream a non-image file as a download rather than rendering it.

    ``Content-Disposition: attachment`` makes the browser save the file instead
    of interpreting it in the chat's own origin, and ``octet-stream`` + nosniff
    stop content-type sniffing that could re-enable inline execution (e.g. a
    ``.html`` or scripted file). ``send_file`` derives the download filename from
    the path's basename.

    ``cache_control`` is set when given; the change-detection endpoint passes
    ``no-store`` so every click re-runs the change check on the live file.
    """
    response = send_file(file_path, mimetype="application/octet-stream", as_attachment=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if cache_control is not None:
        response.headers["Cache-Control"] = cache_control
    return response


def try_serve_file(url_path: str) -> Response | None:
    """Serve the on-disk file addressed by a chat markdown URL.

    ``url_path`` is the catch-all's path component (the request path with its
    leading slash stripped and percent-escapes already decoded). The leading
    slash is restored to recover the absolute on-disk path the agent emitted.

    An image file is streamed inline so it renders; any other existing file is
    streamed as an attachment (a download). A path carrying an image extension
    but no file yields a 404, so a typo'd image renders a broken image rather
    than the app shell. A path with no image extension that matches no file
    yields ``None``, so the caller falls through to the single-page-app catch-all
    and client-side routing is unaffected.
    """
    image_mime_type = image_mime_type_for_path(url_path)
    file_path = Path("/" + url_path)

    if image_mime_type is not None:
        if not file_path.is_file():
            return Response(status=404)
        return serve_inline_image(file_path, image_mime_type)

    if file_path.is_file():
        return serve_download(file_path)
    return None
