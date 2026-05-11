import { RestAPI, StickerActions } from "../../modules";
import { fetchStickerBlob, stickerFilename, stickerMime } from "./fetchSticker";

interface UploadArgs {
    guildId: string;
    sticker: StickerNode;
    name: string;
    description?: string;
    tags?: string;
}

// Upload a sticker to a guild. Tries Discord's internal sticker action first
// (analogous to Emojis.uploadEmoji); falls back to a multipart POST against
// the guild stickers endpoint if the action module isn't present on this
// client build.
export async function uploadSticker({ guildId, sticker, name, description, tags }: UploadArgs): Promise<void> {
    const safeName = name.trim().slice(0, 30);
    const safeDesc = (description ?? "").trim().slice(0, 100);
    const safeTags = (tags ?? sticker.tags ?? sticker.name).trim().slice(0, 200) || "smile";

    const blob = await fetchStickerBlob(sticker);
    const filename = stickerFilename(sticker);
    const mime = stickerMime(sticker);

    if (StickerActions?.createGuildSticker) {
        await StickerActions.createGuildSticker({
            guildId,
            name: safeName,
            description: safeDesc,
            tags: safeTags,
            file: new File([blob], filename, { type: mime }),
        });
        return;
    }

    if (StickerActions?.uploadSticker) {
        await StickerActions.uploadSticker({
            guildId,
            name: safeName,
            description: safeDesc,
            tags: safeTags,
            file: new File([blob], filename, { type: mime }),
        });
        return;
    }

    // Fallback: hit the REST endpoint directly. RestAPI handles auth.
    const form = new FormData();
    form.append("name", safeName);
    form.append("description", safeDesc);
    form.append("tags", safeTags);
    form.append("file", new File([blob], filename, { type: mime }));

    if (!RestAPI?.post) throw new Error("No sticker upload path available on this client");

    await RestAPI.post({
        url: `/guilds/${guildId}/stickers`,
        body: form,
    });
}
