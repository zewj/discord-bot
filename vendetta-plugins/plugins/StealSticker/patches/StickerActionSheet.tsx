import { React } from "@vendetta/metro/common";
import { after, before } from "@vendetta/patcher";
import { findInReactTree } from "@vendetta/utils";
import { LazyActionSheet } from "../modules";
import StealButtons from "../ui/components/StealButtons";

// The sticker preview / long-press sheet has gone by a few names across Discord
// versions. Match on substring rather than equality so the patch survives
// renames like StickerActionSheet -> MessageStickerActionSheet.
const STICKER_SHEET_KEYS = ["StickerActionSheet", "MessageStickerActionSheet", "StickerPickerActionSheet"];

function looksLikeStickerSheet(key: string): boolean {
    if (!key) return false;
    if (STICKER_SHEET_KEYS.includes(key)) return true;
    return /sticker/i.test(key) && /actionsheet/i.test(key);
}

// Walk the tree for a sticker node — the prop shape varies between versions.
function extractSticker(args: any[]): StickerNode | null {
    const [props] = args ?? [];
    if (!props) return null;
    const candidates = [
        props.sticker,
        props.stickerItem,
        props.item,
        props.stickerNode,
        props.message?.stickerItems?.[0],
        props.message?.stickers?.[0],
    ];
    for (const c of candidates) {
        if (c?.id && typeof c.format_type === "number") return c as StickerNode;
    }
    return null;
}

export default () => {
    const cleanups: Array<() => void> = [];

    const unpatchLazy = before("openLazy", LazyActionSheet, ([sheetPromise, key]: any) => {
        if (!looksLikeStickerSheet(key)) return;

        sheetPromise.then((module: any) => {
            const unpatchDefault = after("default", module, (args: any[], res: any) => {
                React.useEffect(() => () => unpatchDefault(), []);

                const sticker = extractSticker(args);
                if (!sticker) return;

                const view = res?.props?.children?.props?.children ?? res?.props?.children;
                if (!view) return;

                const unpatchView = after("type", view, (_: any, component: any) => {
                    React.useEffect(() => unpatchView, []);
                    injectButtons(component, sticker);
                });
            });
            cleanups.push(unpatchDefault);
        });
    });

    cleanups.push(unpatchLazy);

    return () => cleanups.forEach(fn => { try { fn(); } catch {} });
};

function injectButtons(component: any, sticker: StickerNode) {
    // Prefer to drop our buttons next to the existing button row (matching
    // Stealmoji's UX). Otherwise append at the bottom.
    const isButton = (c: any) => c?.type?.name === "Button" || c?.type?.displayName === "Button";
    const buttonsContainer = findInReactTree(component, (c: any) => c?.find?.(isButton));
    const lastButtonIdx = buttonsContainer?.findLastIndex?.(isButton) ?? -1;

    if (lastButtonIdx >= 0) {
        buttonsContainer.splice(lastButtonIdx + 1, 0, <StealButtons sticker={sticker} />);
    } else if (Array.isArray(component?.props?.children)) {
        component.props.children.push(<StealButtons sticker={sticker} />);
    }
}
