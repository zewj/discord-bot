import { React } from "@vendetta/metro/common";
import { findByProps } from "@vendetta/metro";
import { after, before } from "@vendetta/patcher";
import { findInReactTree } from "@vendetta/utils";
import { getAssetIDByName } from "@vendetta/ui/assets";
import { LazyActionSheet } from "../modules";
import { showAddToServerActionSheet } from "../ui/sheets/AddToServerActionSheet";

// Fallback path: if no dedicated sticker action sheet fires (older clients, or
// when the user long-presses the message rather than the sticker itself), add
// our entry to the message long-press sheet whenever the message has stickers.
//
// We inject a "Steal Sticker" row near the top of the message actions; tapping
// it opens the same AddToServer sheet as the sticker-action-sheet path.

const ButtonModule = findByProps("ActionSheetRow") ?? findByProps("Button");

function getMessageStickers(props: any): StickerNode[] {
    const msg = props?.message ?? props?.params?.message;
    if (!msg) return [];
    const items: any[] = msg.stickerItems ?? msg.sticker_items ?? msg.stickers ?? [];
    return items
        .filter((s) => s?.id && typeof s.format_type === "number")
        .map((s) => ({
            id: String(s.id),
            name: s.name,
            format_type: s.format_type,
            description: s.description,
            tags: s.tags,
        }));
}

export default () => {
    const cleanups: Array<() => void> = [];

    const unpatchLazy = before("openLazy", LazyActionSheet, (args: any[]) => {
        const [sheetPromise, key, msgProps] = args;
        if (key !== "MessageLongPressActionSheet") return;

        const stickers = getMessageStickers(msgProps);
        if (!stickers.length) return;

        sheetPromise.then((module: any) => {
            const unpatchDefault = after("default", module, (_: any[], res: any) => {
                React.useEffect(() => () => unpatchDefault(), []);
                injectStealOption(res, stickers);
            });
            cleanups.push(unpatchDefault);
        });
    });

    cleanups.push(unpatchLazy);
    return () => cleanups.forEach(fn => { try { fn(); } catch {} });
};

function injectStealOption(res: any, stickers: StickerNode[]) {
    const sticker = stickers[0]; // Discord allows up to 3 per message; offer the first.
    const ActionSheetRow = ButtonModule?.ActionSheetRow;
    if (!ActionSheetRow) return;

    const open = () => {
        LazyActionSheet.hideActionSheet();
        showAddToServerActionSheet(sticker);
    };

    const row = (
        <ActionSheetRow
            key="steal-sticker"
            label={stickers.length > 1 ? `Steal Sticker (${sticker.name})` : "Steal Sticker"}
            icon={<ActionSheetRow.Icon source={getAssetIDByName("ic_sticker_24px")} />}
            onPress={open}
        />
    );

    // The action sheet's body is typically an array of ActionSheetRow children.
    // Find the array and push our row in.
    const container = findInReactTree(res, (c: any) =>
        Array.isArray(c?.props?.children)
        && c.props.children.some((x: any) => x?.type === ActionSheetRow || x?.props?.label)
    );
    if (container && Array.isArray(container.props.children)) {
        container.props.children.unshift(row);
    }
}
