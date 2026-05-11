import { React } from "@vendetta/metro/common";
import { findByProps } from "@vendetta/metro";
import { getAssetIDByName } from "@vendetta/ui/assets";
import { Forms } from "@vendetta/ui/components";
import { showToast } from "@vendetta/ui/toasts";
import { showInputDialog } from "../../lib/utils/AlertDialog";
import { uploadSticker } from "../../lib/utils/uploadSticker";
import { GuildIcon, GuildIconSizes, LazyActionSheet, StickerStore } from "../../modules";

const stickerSlotModule = findByProps("getMaxStickerSlots");
const { FormRow, FormIcon } = Forms;

// Stickers per guild scale with boost level. Default Discord caps:
// level 0: 5, level 1: 15, level 2: 30, level 3: 60.
const STICKER_TIER_CAPS = [5, 15, 30, 60];

function getMaxSlots(guild: any): number {
    const fromGuild = guild.getMaxStickerSlots?.();
    if (typeof fromGuild === "number") return fromGuild;
    const fromModule = stickerSlotModule?.getMaxStickerSlots?.(guild);
    if (typeof fromModule === "number") return fromModule;
    const tier = guild.premiumTier ?? guild.premium_tier ?? 0;
    return STICKER_TIER_CAPS[Math.min(tier, STICKER_TIER_CAPS.length - 1)];
}

function getUsedSlots(guildId: string): number {
    const stickers = StickerStore?.getStickersByGuildId?.(guildId)
        ?? StickerStore?.getGuildStickers?.(guildId)
        ?? [];
    return Array.isArray(stickers) ? stickers.length : 0;
}

export default function AddToServerRow({ guild, sticker }: { guild: any; sticker: StickerNode }) {
    const slotsAvailable = React.useMemo(() => {
        const max = getMaxSlots(guild);
        return getUsedSlots(guild.id) < max;
    }, [guild.id]);

    const onPress = () => {
        showInputDialog({
            title: "Sticker name",
            initialValue: sticker.name,
            placeholder: "sticker",
            confirmText: `Add to ${guild.name}`,
            cancelText: "Cancel",
            onConfirm: async (name) => {
                try {
                    await uploadSticker({
                        guildId: guild.id,
                        sticker,
                        name,
                        description: sticker.description ?? "",
                        tags: sticker.tags ?? sticker.name,
                    });
                    showToast(
                        `Added ${sticker.name}${sticker.name !== name ? ` as ${name}` : ""} to ${guild.name}`,
                        getAssetIDByName("Check"),
                    );
                } catch (e: any) {
                    const msg = e?.body?.message ?? e?.message ?? "Upload failed";
                    showToast(msg, getAssetIDByName("Small"));
                }
            },
        });
        LazyActionSheet.hideActionSheet();
    };

    return (
        <FormRow
            leading={<GuildIcon guild={guild} size={GuildIconSizes.MEDIUM} animate={false} />}
            disabled={!slotsAvailable}
            label={guild.name}
            subLabel={!slotsAvailable ? "No sticker slots available" : undefined}
            trailing={<FormIcon style={{ opacity: 1 }} source={getAssetIDByName("ic_add_24px")} />}
            onPress={onPress}
        />
    );
}
