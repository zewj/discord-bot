import patchStickerActionSheet from "./patches/StickerActionSheet";
import patchMessageLongPressActionSheet from "./patches/MessageLongPressActionSheet";

let patches: Array<() => void> = [];

export const onLoad = () => {
    if (patches.length) onUnload();
    patches.push(patchStickerActionSheet());
    patches.push(patchMessageLongPressActionSheet());
};

export const onUnload = () => {
    for (const unpatch of patches) {
        try { unpatch?.(); } catch {}
    }
    patches = [];
};

export default { onLoad, onUnload };
