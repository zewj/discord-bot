import { find, findByProps, findByStoreName } from "@vendetta/metro";

export const LazyActionSheet = findByProps("hideActionSheet");
export const MediaModalUtils = findByProps("openMediaModal");

export const ActionSheet = findByProps("ActionSheet")?.ActionSheet
    ?? find(m => m.render?.name === "ActionSheet");

export const {
    ActionSheetTitleHeader,
    ActionSheetCloseButton
} = findByProps("ActionSheetTitleHeader");

export const {
    BottomSheetFlatList
} = findByProps("BottomSheetScrollView");

export const GuildStore = findByStoreName("GuildStore");
export const StickerStore = findByStoreName("StickersStore") ?? findByStoreName("StickerStore");
export const PermissionsStore = findByStoreName("PermissionStore");

export const {
    default: GuildIcon,
    GuildIconSizes
} = findByProps("GuildIconSizes");

export const { downloadMediaAsset } = findByProps("downloadMediaAsset") ?? {};

// Discord exposes guild sticker upload through a few different shapes depending
// on client version; grab whatever's available and call it from the upload util.
export const StickerActions =
    findByProps("createGuildSticker")
    ?? findByProps("uploadSticker")
    ?? findByProps("createSticker");

export const RestAPI = findByProps("getAPIBaseURL", "post") ?? findByProps("HTTP", "post");
