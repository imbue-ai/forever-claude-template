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

# Long-lived caching for inline images. Chat markdown files are rewritten by
# the frontend to the per-message snapshot route (see ``chat_file_snapshots``),
# whose URLs are immutable by construction; this direct-path route remains for
# non-chat fetches (e.g. opening an image URL in a tab), where agents are
# instructed (see the show-files-in-chat skill) to give each image a unique
# filename. A one-year max-age plus ``immutable`` lets the browser skip
# revalidation entirely while a conversation is re-rendered.
_IMAGE_CACHE_MAX_AGE_SECONDS = 31_536_000

# Cache-Control for content that can never change behind its URL -- a per-message
# snapshot blob. Reused by the snapshot endpoint for both inline images and
# downloads so a re-render (or re-click) never re-fetches.
IMMUTABLE_CACHE_CONTROL = f"public, max-age={_IMAGE_CACHE_MAX_AGE_SECONDS}, immutable"

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


def serve_inline_image(file_path: Path, mime_type: str) -> Response:
    """Stream an image so it renders inline in the chat.

    Public because the chat-file snapshot endpoint (``server``) serves its
    frozen image copies with exactly the same headers -- and for snapshots the
    ``immutable`` cache policy is true by construction, not by convention.
    """
    response = send_file(file_path, mimetype=mime_type)
    # send_file's default cache policy is conservative; override it so the
    # browser caches aggressively (image filenames are unique by convention).
    response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
    if file_path.suffix.lower() == _SVG_EXTENSION:
        response.headers["Content-Security-Policy"] = _SVG_CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def serve_download(file_path: Path, download_name: str | None = None, cache_control: str | None = None) -> Response:
    """Stream a non-image file as a download rather than rendering it.

    ``Content-Disposition: attachment`` makes the browser save the file instead
    of interpreting it in the chat's own origin, and ``octet-stream`` + nosniff
    stop content-type sniffing that could re-enable inline execution (e.g. a
    ``.html`` or scripted file).

    ``download_name`` sets the saved filename; when omitted ``send_file`` derives
    it from the path's basename (used by the direct-path route). The snapshot
    endpoint passes the original basename so a content-hashed blob still
    downloads under the name the agent referenced, and ``cache_control`` so the
    frozen blob is cached immutably.
    """
    response = send_file(
        file_path, mimetype="application/octet-stream", as_attachment=True, download_name=download_name
    )
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
