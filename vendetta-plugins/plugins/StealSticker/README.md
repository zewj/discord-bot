# StealSticker

Steal stickers on mobile, the same way [Stealmoji](https://aliernfrog.github.io/vd-plugins/Stealmoji/) does for emojis.

## What it does

Adds these options to the sticker action sheet (long-press a sticker — or long-press the message that contains it):

- **Add to Server** — pick one of your servers (where you have Create Expressions permission) and upload the sticker. You can rename it during upload.
- **Copy URL to clipboard** — the CDN URL for the sticker.
- **Copy image to clipboard** (iOS only, non-Lottie) — the raw image bytes.
- **Save to Downloads / Camera Roll** — saves the sticker file locally.

## Notes

- **Sticker formats**: PNG, APNG, and GIF stickers upload as-is. Lottie stickers (the animated vector kind) are JSON — Discord accepts them, but most servers won't have a use for them.
- **Slot limits**: the upload button is disabled if the destination server is out of sticker slots (5/15/30/60 depending on boost tier).
- **Permissions**: you need the *Create Expressions* (or legacy *Manage Emojis and Stickers*) permission on the destination server. The plugin filters the picker to only show servers where you have it.
- **Compatibility**: the patch tries two paths — the dedicated sticker action sheet (newer clients) and the regular message long-press sheet (universal fallback). At least one should fire on any recent Revenge/Vendetta build.

## Install

This plugin is built like any other Vendetta/Revenge plugin. From the `vendetta-plugins/` directory:

```bash
npm install
npm run build
```

The built plugin lands in `dist/StealSticker/`. Host the `dist/` directory anywhere (GitHub Pages, a static file server, etc.) and install with the URL `https://your-host/StealSticker/`.

## License

GPLv3 — same as the Stealmoji plugin this is modeled on.
