// Discord sticker CDN. Lottie stickers (format_type 3) are JSON, not images.
export function buildStickerURL(sticker: StickerNode, size = 320): string {
    const ext = stickerExtension(sticker);
    const host = sticker.format_type === 4
        ? "https://media.discordapp.net"
        : "https://cdn.discordapp.com";
    const qs = ext === "json" ? "" : `?size=${size}`;
    return `${host}/stickers/${sticker.id}.${ext}${qs}`;
}

export function stickerExtension(sticker: StickerNode): "png" | "json" | "gif" {
    if (sticker.format_type === 3) return "json"; // LOTTIE
    if (sticker.format_type === 4) return "gif";
    return "png"; // PNG and APNG both use .png
}

export function stickerMime(sticker: StickerNode): string {
    if (sticker.format_type === 3) return "application/json";
    if (sticker.format_type === 4) return "image/gif";
    return "image/png";
}

export function isAnimated(sticker: StickerNode): boolean {
    return sticker.format_type !== 1;
}
