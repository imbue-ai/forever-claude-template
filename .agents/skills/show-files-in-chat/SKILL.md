---
name: show-files-in-chat
description: Show a file to the user in chat. Display an image inline (chart, plot, screenshot, diagram, rendered figure, photo), or offer any other file (PDF, CSV, log, zip, spreadsheet, ...) as a download link. Use whenever you have a file on disk you want the user to see or download, or want to embed an image from a public URL.
---

# Showing a file in chat

Your chat replies are rendered as markdown, so you can put a file in front of the
user directly -- there is no upload step and no external hosting. The system
interface serves a file at its absolute on-disk path, so the path you write in
the markdown doubles as the URL the user's browser fetches. Images render inline;
any other file is offered as a download.

## Show an image inline

1. Write the image to disk under `runtime/chat-images/` (create it once with
   `mkdir -p runtime/chat-images` if it does not exist). That directory is
   gitignored and persists with the rest of `runtime/` (covered by the opt-in
   GitHub sync when the user has enabled the `github-sync` skill).

   Give each image a unique, descriptive filename, e.g.
   `revenue-by-quarter-2026.png`. The chat freezes a per-message copy of each
   referenced image, so reusing a filename cannot corrupt an already-posted
   message -- but distinct names still make every image identifiable in the
   lightbox title and as a download.

2. Reference it by its **absolute** on-disk path with markdown image syntax:

   ```
   ![Revenue by quarter](/mngr/code/runtime/chat-images/revenue-by-quarter-2026.png)
   ```

   The path must be absolute (start with `/`). A relative path such as
   `![x](runtime/chat-images/x.png)` will not render.

Supported inline image formats: `.png`, `.jpg` / `.jpeg`, `.gif`, `.webp`, `.avif`, `.bmp`, `.ico`, `.svg`.

## Offer a file for download

For anything that is not an image -- a PDF, CSV, log, zip, spreadsheet, etc. --
write the file to disk (a `runtime/chat-files/` directory is a good convention,
though any path works) and reference its **absolute** path with an ordinary
markdown link (not image syntax):

```
[Q4 report (PDF)](/mngr/code/runtime/chat-files/q4-report.pdf)
```

Clicking the link downloads the file. There is nothing else to do -- the system
interface serves non-image files with a download disposition, so a plain link
becomes a download. Use a clear label that says what the file is.

## Embed an image from a public URL

If the image already lives at a public URL, embed that URL directly -- no local
file needed:

```
![alt text](https://example.com/image.png)
```

Use the public-URL form for images you are referencing from the web, and the
local absolute-path form for files you produced on this machine.

## Notes

- Both images and download links require an **absolute** path (starting with
  `/`) that points at a file that actually exists on disk.
- If an image shows a broken-image icon, the usual cause is a relative or
  mistyped path, or an extension that is not one of the inline image formats
  above (a non-image extension is treated as a download, not an inline image).
- Only the exact file you reference is served; there is no directory listing.
