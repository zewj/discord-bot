import { stickerMime, stickerExtension, buildStickerURL } from "./stickerUrl";

// Fetch the sticker as a binary Blob for upload. Lottie stickers come down as
// JSON; the caller decides what to do with the bytes.
export async function fetchStickerBlob(sticker: StickerNode): Promise<Blob> {
    const url = buildStickerURL(sticker);
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Sticker fetch failed: HTTP ${res.status}`);
    return await res.blob();
}

// data: URL for clipboard / preview use cases.
export function fetchStickerAsDataURL(sticker: StickerNode, cb: (dataUrl: string) => void): void {
    fetch(buildStickerURL(sticker))
        .then(r => r.blob())
        .then(blob => {
            const reader = new FileReader();
            reader.onloadend = () => cb(reader.result as string);
            reader.readAsDataURL(blob);
        });
}

export function stickerFilename(sticker: StickerNode): string {
    const safe = sticker.name.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 30) || "sticker";
    return `${safe}.${stickerExtension(sticker)}`;
}

export { stickerMime, stickerExtension, buildStickerURL };
