import { constants } from "@vendetta/metro/common";
import { ErrorBoundary, Forms } from "@vendetta/ui/components";
import AddToServerRow from "../components/AddToServerRow";
import { buildStickerURL } from "../../lib/utils/stickerUrl";
import {
    ActionSheet,
    ActionSheetTitleHeader,
    ActionSheetCloseButton,
    BottomSheetFlatList,
    GuildStore,
    LazyActionSheet,
    PermissionsStore,
} from "../../modules";

const { FormDivider, FormIcon } = Forms;

export function showAddToServerActionSheet(sticker: StickerNode) {
    const element = (
        <ActionSheet scrollable>
            <ErrorBoundary>
                <AddToServer sticker={sticker} />
            </ErrorBoundary>
        </ActionSheet>
    );

    LazyActionSheet.openLazy(
        Promise.resolve({ default: () => element }),
        "AddStickerToServerActionSheet",
    );
}

function AddToServer({ sticker }: { sticker: StickerNode }) {
    // Either CREATE_GUILD_EXPRESSIONS (newer) or MANAGE_EMOJIS_AND_STICKERS (older).
    const perms = constants.Permissions ?? {};
    const can = (guild: any) =>
        (perms.CREATE_GUILD_EXPRESSIONS && PermissionsStore.can(perms.CREATE_GUILD_EXPRESSIONS, guild))
        || (perms.MANAGE_EMOJIS_AND_STICKERS && PermissionsStore.can(perms.MANAGE_EMOJIS_AND_STICKERS, guild));

    const guilds = Object.values(GuildStore.getGuilds())
        .filter(can)
        .sort((a: any, b: any) => a.name?.localeCompare?.(b.name));

    const previewUrl = sticker.format_type === 3 ? undefined : buildStickerURL(sticker, 64);

    return (
        <>
            <ActionSheetTitleHeader
                title={`Stealing ${sticker.name}`}
                leading={previewUrl ? (
                    <FormIcon
                        style={{ marginRight: 12, opacity: 1 }}
                        source={{ uri: previewUrl }}
                        disableColor
                    />
                ) : undefined}
                trailing={<ActionSheetCloseButton onPress={() => LazyActionSheet.hideActionSheet()} />}
            />
            <BottomSheetFlatList
                style={{ flex: 1 }}
                contentContainerStyle={{ paddingBottom: 24 }}
                data={guilds}
                renderItem={({ item }: { item: any }) => (
                    <AddToServerRow guild={item} sticker={sticker} />
                )}
                ItemSeparatorComponent={FormDivider}
                keyExtractor={(x: any) => x.id}
            />
        </>
    );
}
