import { findByProps } from "@vendetta/metro";
import { clipboard, ReactNative } from "@vendetta/metro/common";
import { getAssetIDByName } from "@vendetta/ui/assets";
import { showToast } from "@vendetta/ui/toasts";
import { fetchStickerAsDataURL, buildStickerURL } from "../../lib/utils/fetchSticker";
import { downloadMediaAsset, LazyActionSheet } from "../../modules";
import { showAddToServerActionSheet } from "../sheets/AddToServerActionSheet";

const { Button } = findByProps("TableRow", "Button") ?? findByProps("Button");

export default function StealButtons({ sticker }: { sticker: StickerNode }) {
    const url = buildStickerURL(sticker);
    const isLottie = sticker.format_type === 3;

    const buttons: Array<{ text: string; callback: () => void }> = [
        {
            text: "Add to Server",
            callback: () => showAddToServerActionSheet(sticker),
        },
        {
            text: "Copy URL to clipboard",
            callback: () => {
                clipboard.setString(url);
                LazyActionSheet.hideActionSheet();
                showToast(`Copied ${sticker.name}'s URL to clipboard`, getAssetIDByName("ic_copy_message_link"));
            },
        },
    ];

    if (ReactNative.Platform.OS === "ios" && !isLottie) {
        buttons.push({
            text: "Copy image to clipboard",
            callback: () => fetchStickerAsDataURL(sticker, (dataUrl) => {
                clipboard.setImage(dataUrl.split(",")[1]);
                LazyActionSheet.hideActionSheet();
                showToast(`Copied ${sticker.name}'s image to clipboard`, getAssetIDByName("ic_message_copy"));
            }),
        });
    }

    if (downloadMediaAsset) {
        const where = ReactNative.Platform.select({ android: "Downloads", default: "Camera Roll" });
        buttons.push({
            text: `Save to ${where}`,
            callback: () => {
                const animated = sticker.format_type !== 1 ? 1 : 0;
                downloadMediaAsset(url, animated);
                LazyActionSheet.hideActionSheet();
                showToast(`Saved ${sticker.name} to ${where}`, getAssetIDByName("toast_image_saved"));
            },
        });
    }

    return (
        <>
            {buttons.map(({ text, callback }) => (
                <Button
                    color={Button?.Colors?.BRAND}
                    text={text}
                    size={Button?.Sizes?.SMALL}
                    onPress={callback}
                    style={{ marginTop: ReactNative.Platform.select({ android: 12, default: 16 }) }}
                />
            ))}
        </>
    );
}
