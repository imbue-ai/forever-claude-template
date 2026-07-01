---
name: show-image-in-chat
description: Show an image (chart, plot, screenshot, diagram, rendered figure, photo) inline in your chat reply so it renders for the user instead of being described or linked. Use whenever you have generated or have an image file on disk and want it displayed in the conversation, or want to embed an image that already lives at a public URL.
---

# Showing an image in chat

Your chat replies are rendered as markdown, so a standard markdown image renders
inline in the conversation -- there is no upload step and no external hosting.
The system interface serves an image file at its absolute on-disk path, so the
path you write in the markdown doubles as the URL the user's browser fetches.

## Show an image you generated on this machine

1. Write the image to disk under `runtime/chat-images/` (create it once with
   `mkdir -p runtime/chat-images` if it does not exist). That directory is
   gitignored and is backed up along with the rest of `runtime/`.

   Give each image a unique, descriptive filename, e.g.
   `revenue-by-quarter-2026.png`. Served image URLs are cached immutably, so
   reusing a filename would leave the user looking at the stale image.

2. Reference it by its **absolute** on-disk path with markdown image syntax:

   ```
   ![Revenue by quarter](/mngr/code/runtime/chat-images/revenue-by-quarter-2026.png)
   ```

   The path must be absolute (start with `/`). A relative path such as
   `![x](runtime/chat-images/x.png)` will not render.

Supported formats: `.png`, `.jpg` / `.jpeg`, `.gif`, `.webp`, `.svg`.

## Embed an image from a public URL

If the image already lives at a public URL, embed that URL directly -- no local
file needed:

```
![alt text](https://example.com/image.png)
```

Use the public-URL form for images you are referencing from the web, and the
local absolute-path form for images you generated on this machine.

## Notes

- If the image does not appear (a broken-image icon shows instead), the most
  common causes are a relative or mistyped path, or a filename whose extension
  is not one of the supported formats above.
- Only the file at the exact path you reference is served; there is no
  directory listing.
