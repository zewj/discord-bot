// Discord sticker formats: 1=PNG, 2=APNG, 3=LOTTIE, 4=GIF
declare interface StickerNode {
    id: string;
    name: string;
    format_type: 1 | 2 | 3 | 4;
    description?: string;
    tags?: string;
    guild_id?: string;
    available?: boolean;
}
