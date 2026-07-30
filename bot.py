import asyncio
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cookie-translator")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DB_PATH = Path(__file__).with_name("message_links.db")

GENERAL_CHANNELS: Dict[int, str] = {
    int(os.getenv("ENGLISH_CHANNEL_ID", "0")): "en",
    int(os.getenv("SPANISH_CHANNEL_ID", "0")): "es",
    int(os.getenv("PORTUGUESE_CHANNEL_ID", "0")): "pt",
    int(os.getenv("FRENCH_CHANNEL_ID", "0")): "fr",
    int(os.getenv("GERMAN_CHANNEL_ID", "0")): "de",
    int(os.getenv("TURKISH_CHANNEL_ID", "0")): "tr",
    int(os.getenv("ARABIC_CHANNEL_ID", "0")): "ar",
    int(os.getenv("CHINESE_CHANNEL_ID", "0")): "zh-CN",
}
GENERAL_CHANNELS = {
    channel_id: language
    for channel_id, language in GENERAL_CHANNELS.items()
    if channel_id
}

# Private R4 officer translation channels.
R4_CHANNELS: Dict[int, str] = {
    1398969555958628413: "en",  # r4-cookies-chat
    1529548928218042458: "es",  # Spanish R4 Cookies chat
}

# Each message is translated only to channels inside its own group.
TRANSLATION_GROUPS = {
    "general": GENERAL_CHANNELS,
    "r4_officers": R4_CHANNELS,
}

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish (Latin America)",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "tr": "Turkish",
    "ar": "Arabic",
    "zh-CN": "Chinese (Simplified)",
}

# These game words will be protected from translation.
PROTECTED_TERMS = [
    "CMON", "Mecha Fire", "Den", "Den 1", "Den 2", "SVS", "Nexus",
    "SSR", "SR", "R4", "R5", "R7", "NAP", "Omega", "Photon",
    "Felina", "Vypher", "Grunt", "Sancia", "Astraeus",
]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
webhook_cache: Dict[int, discord.Webhook] = {}
database_lock = asyncio.Lock()



def _initialize_database_sync() -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_links (
                message_id INTEGER PRIMARY KEY,
                link_group TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                language TEXT NOT NULL,
                is_original INTEGER NOT NULL DEFAULT 0,
                author_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_links_group
            ON message_links(link_group)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reaction_sources (
                link_group TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                emoji_key TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id, emoji_key)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reaction_sources_group_emoji
            ON reaction_sources(link_group, emoji_key)
            """
        )
        connection.commit()


async def initialize_database() -> None:
    await asyncio.to_thread(_initialize_database_sync)


def _store_message_link_sync(
    message_id: int,
    link_group: str,
    channel_id: int,
    language: str,
    is_original: bool,
    author_id: Optional[int],
) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO message_links
                (message_id, link_group, channel_id, language, is_original, author_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, link_group, channel_id, language, int(is_original), author_id),
        )
        connection.commit()


async def store_message_link(
    message_id: int,
    link_group: str,
    channel_id: int,
    language: str,
    is_original: bool,
    author_id: Optional[int],
) -> None:
    async with database_lock:
        await asyncio.to_thread(
            _store_message_link_sync,
            message_id,
            link_group,
            channel_id,
            language,
            is_original,
            author_id,
        )


def _get_message_link_sync(message_id: int) -> Optional[Tuple[str, int, str, bool]]:
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT link_group, channel_id, language, is_original
            FROM message_links
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    if row is None:
        return None
    return row[0], int(row[1]), str(row[2]), bool(row[3])


async def get_message_link(message_id: int) -> Optional[Tuple[str, int, str, bool]]:
    async with database_lock:
        return await asyncio.to_thread(_get_message_link_sync, message_id)


def _get_linked_messages_sync(link_group: str) -> List[Tuple[int, int, str, bool]]:
    with sqlite3.connect(DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT message_id, channel_id, language, is_original
            FROM message_links
            WHERE link_group = ?
            ORDER BY is_original DESC, created_at ASC
            """,
            (link_group,),
        ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2]), bool(r[3])) for r in rows]


async def get_linked_messages(link_group: str) -> List[Tuple[int, int, str, bool]]:
    async with database_lock:
        return await asyncio.to_thread(_get_linked_messages_sync, link_group)


def _delete_link_group_sync(link_group: str) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute("DELETE FROM reaction_sources WHERE link_group = ?", (link_group,))
        connection.execute("DELETE FROM message_links WHERE link_group = ?", (link_group,))
        connection.commit()


async def delete_link_group(link_group: str) -> None:
    async with database_lock:
        await asyncio.to_thread(_delete_link_group_sync, link_group)


def emoji_database_key(emoji: discord.PartialEmoji) -> str:
    if emoji.id is not None:
        return f"custom:{emoji.id}"
    return f"unicode:{emoji.name}"


def _add_reaction_source_sync(
    link_group: str,
    message_id: int,
    user_id: int,
    emoji_key: str,
) -> int:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO reaction_sources
                (link_group, message_id, user_id, emoji_key)
            VALUES (?, ?, ?, ?)
            """,
            (link_group, message_id, user_id, emoji_key),
        )
        count = connection.execute(
            """
            SELECT COUNT(*) FROM reaction_sources
            WHERE link_group = ? AND emoji_key = ?
            """,
            (link_group, emoji_key),
        ).fetchone()[0]
        connection.commit()
    return int(count)


async def add_reaction_source(
    link_group: str,
    message_id: int,
    user_id: int,
    emoji_key: str,
) -> int:
    async with database_lock:
        return await asyncio.to_thread(
            _add_reaction_source_sync,
            link_group,
            message_id,
            user_id,
            emoji_key,
        )


def _remove_reaction_source_sync(
    link_group: str,
    message_id: int,
    user_id: int,
    emoji_key: str,
) -> int:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            DELETE FROM reaction_sources
            WHERE message_id = ? AND user_id = ? AND emoji_key = ?
            """,
            (message_id, user_id, emoji_key),
        )
        count = connection.execute(
            """
            SELECT COUNT(*) FROM reaction_sources
            WHERE link_group = ? AND emoji_key = ?
            """,
            (link_group, emoji_key),
        ).fetchone()[0]
        connection.commit()
    return int(count)


async def remove_reaction_source(
    link_group: str,
    message_id: int,
    user_id: int,
    emoji_key: str,
) -> int:
    async with database_lock:
        return await asyncio.to_thread(
            _remove_reaction_source_sync,
            link_group,
            message_id,
            user_id,
            emoji_key,
        )


async def get_text_channel(channel_id: int) -> Optional[discord.TextChannel]:
    channel = bot.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    try:
        fetched = await bot.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return fetched if isinstance(fetched, discord.TextChannel) else None


async def fetch_linked_message(
    channel_id: int,
    message_id: int,
) -> Optional[discord.Message]:
    channel = await get_text_channel(channel_id)
    if channel is None:
        return None
    try:
        return await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


ENGLISH_ROLE_ID = 1529497300903395580
SPANISH_ROLE_ID = 1529497832266928189
PORTUGUESE_ROLE_ID = 1529498212476653649
LANGUAGE_ROLE_IDS = {ENGLISH_ROLE_ID, SPANISH_ROLE_ID, PORTUGUESE_ROLE_ID}


async def set_language_role(interaction: discord.Interaction, role_id: int, language_name: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This button can only be used inside the CMON server.",
            ephemeral=True,
        )
        return

    selected_role = interaction.guild.get_role(role_id)
    if selected_role is None:
        await interaction.response.send_message(
            "I could not find that language role. Please tell an administrator.",
            ephemeral=True,
        )
        return

    roles_to_remove = [
        role for role in interaction.user.roles
        if role.id in LANGUAGE_ROLE_IDS and role.id != role_id
    ]

    try:
        if roles_to_remove:
            await interaction.user.remove_roles(
                *roles_to_remove,
                reason="Member changed language selection",
            )

        if selected_role not in interaction.user.roles:
            await interaction.user.add_roles(
                selected_role,
                reason=f"Member selected {language_name}",
            )

        await interaction.response.send_message(
            f"Your language has been set to **{language_name}**.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I cannot assign that role. Make sure the Cookie Translator role is above the language roles and has Manage Roles enabled.",
            ephemeral=True,
        )


class LanguageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="English",
        emoji="🇺🇸",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_english",
    )
    async def english_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, ENGLISH_ROLE_ID, "English")

    @discord.ui.button(
        label="Español",
        emoji="🇪🇸",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_spanish",
    )
    async def spanish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, SPANISH_ROLE_ID, "Español")

    @discord.ui.button(
        label="Português",
        emoji="🇵🇹",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_portuguese",
    )
    async def portuguese_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, PORTUGUESE_ROLE_ID, "Português")



def protect_terms(text: str):
    protected = {}
    result = text

    # Longest terms first prevents "Den" from partially matching "Den 1".
    for index, term in enumerate(sorted(PROTECTED_TERMS, key=len, reverse=True)):
        placeholder = f"ZXQTERM{index}QXZ"
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        if pattern.search(result):
            protected[placeholder] = term
            result = pattern.sub(placeholder, result)

    return result, protected


def restore_terms(text: str, protected: dict):
    result = text
    for placeholder, original in protected.items():
        result = result.replace(placeholder, original)
    return result


# Google Translate exposes general Spanish ("es"), not a separate Latin-American
# target through deep-translator. These substitutions gently normalize common
# Spain-specific wording into neutral Latin-American Spanish.
LATAM_SPANISH_REPLACEMENTS = {
    r"\bvosotros\b": "ustedes",
    r"\bvosotras\b": "ustedes",
    r"\bvuestro\b": "su",
    r"\bvuestra\b": "su",
    r"\bvuestros\b": "sus",
    r"\bvuestras\b": "sus",
    r"\bordenador\b": "computadora",
    r"\bordenadores\b": "computadoras",
    r"\bmóvil\b": "celular",
    r"\bmóviles\b": "celulares",
    r"\bconducir\b": "manejar",
    r"\bvale\b": "de acuerdo",
}


def preserve_case(replacement: str, matched_text: str) -> str:
    if matched_text.isupper():
        return replacement.upper()
    if matched_text[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def normalize_latam_spanish(text: str) -> str:
    result = text
    for pattern, replacement in LATAM_SPANISH_REPLACEMENTS.items():
        result = re.sub(
            pattern,
            lambda match: preserve_case(replacement, match.group(0)),
            result,
            flags=re.IGNORECASE,
        )
    return result


def translate_sync(text: str, target_language: str) -> str:
    protected_text, protected = protect_terms(text)
    translated = GoogleTranslator(source="auto", target=target_language).translate(protected_text)

    if target_language == "es":
        translated = normalize_latam_spanish(translated)

    return restore_terms(translated, protected)


async def translate_text(text: str, target_language: str) -> str:
    # Translation is blocking, so run it away from Discord's event loop.
    return await asyncio.to_thread(translate_sync, text, target_language)


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    cached = webhook_cache.get(channel.id)
    if cached:
        return cached

    hooks = await channel.webhooks()
    for hook in hooks:
        if hook.name == "Cookie Translator Relay":
            webhook_cache[channel.id] = hook
            return hook

    hook = await channel.create_webhook(
        name="Cookie Translator Relay",
        reason="Mirror translated chat messages",
    )
    webhook_cache[channel.id] = hook
    return hook


def attachment_lines(message: discord.Message) -> str:
    if not message.attachments:
        return ""
    return "\n" + "\n".join(attachment.url for attachment in message.attachments)


@bot.event
async def on_ready():
    await initialize_database()

    if not getattr(bot, "language_view_added", False):
        bot.add_view(LanguageView())
        bot.language_view_added = True

    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Configured translation groups: %s", TRANSLATION_GROUPS)
    print("\nCOOKIE TRANSLATOR IS ONLINE")
    print(f"Logged in as: {bot.user}")
    print("Keep this window open while the bot is running.\n")


@bot.command(name="languagepanel")
@commands.has_permissions(administrator=True)
async def language_panel(ctx: commands.Context):
    message = (
        "🌍 **Choose Your Language**\n\n"
        "Welcome to CMON! Choose your preferred language below. "
        "You can change it at any time by clicking a different button.\n\n"
        "🇺🇸 English\n"
        "🇪🇸 Español\n"
        "🇵🇹 Português"
    )
    await ctx.send(message, view=LanguageView())


@language_panel.error
async def language_panel_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Only a server administrator can post the language panel.")
    else:
        raise error


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id is not None:
        return

    await bot.process_commands(message)

    # Ignore DMs and channels outside the translation system.
    if message.guild is None:
        return

    active_group = None
    for group_name, channels in TRANSLATION_GROUPS.items():
        if message.channel.id in channels:
            active_group = (group_name, channels)
            break

    if active_group is None:
        return

    group_name, group_channels = active_group
    original_text = message.content.strip()
    attachments = attachment_lines(message)

    # Still mirror image-only messages.
    if not original_text and not attachments:
        return

    source_language = group_channels[message.channel.id]
    username = message.author.display_name[:80]
    avatar_url = message.author.display_avatar.url
    link_group = uuid.uuid4().hex

    await store_message_link(
        message.id,
        link_group,
        message.channel.id,
        source_language,
        True,
        message.author.id,
    )

    for target_channel_id, target_language in group_channels.items():
        if target_channel_id == message.channel.id:
            continue

        target_channel = message.guild.get_channel(target_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            log.error("Cannot find target text channel ID %s", target_channel_id)
            continue

        try:
            if original_text:
                translated = await translate_text(original_text, target_language)
            else:
                translated = ""

            output = (translated + attachments).strip()
            if not output:
                continue

            webhook = await get_or_create_webhook(target_channel)
            translated_message = await webhook.send(
                content=output[:2000],
                username=username,
                avatar_url=avatar_url,
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
            )

            await store_message_link(
                translated_message.id,
                link_group,
                target_channel_id,
                target_language,
                False,
                message.author.id,
            )

            log.info(
                "Translated in group %s: %s -> %s for %s",
                group_name,
                LANGUAGE_NAMES[source_language],
                LANGUAGE_NAMES[target_language],
                username,
            )

        except discord.Forbidden:
            log.exception(
                "Missing permission in #%s. The bot needs View Channel, "
                "Send Messages, Read Message History, and Manage Webhooks.",
                target_channel.name,
            )
        except Exception:
            log.exception(
                "Translation failed from %s to %s",
                source_language,
                target_language,
            )



@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user is None or payload.user_id == bot.user.id or payload.guild_id is None:
        return

    link = await get_message_link(payload.message_id)
    if link is None:
        return

    link_group, _, _, _ = link
    emoji_key = emoji_database_key(payload.emoji)
    await add_reaction_source(
        link_group,
        payload.message_id,
        payload.user_id,
        emoji_key,
    )

    for message_id, channel_id, _, _ in await get_linked_messages(link_group):
        if message_id == payload.message_id:
            continue
        linked_message = await fetch_linked_message(channel_id, message_id)
        if linked_message is None:
            continue
        try:
            await linked_message.add_reaction(payload.emoji)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception(
                "Could not mirror reaction %s to message %s",
                payload.emoji,
                message_id,
            )


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if bot.user is None or payload.user_id == bot.user.id or payload.guild_id is None:
        return

    link = await get_message_link(payload.message_id)
    if link is None:
        return

    link_group, _, _, _ = link
    emoji_key = emoji_database_key(payload.emoji)
    remaining = await remove_reaction_source(
        link_group,
        payload.message_id,
        payload.user_id,
        emoji_key,
    )

    # Keep CHiP's mirrored reaction while at least one human reaction remains
    # anywhere inside the linked message group.
    if remaining > 0:
        return

    for message_id, channel_id, _, _ in await get_linked_messages(link_group):
        linked_message = await fetch_linked_message(channel_id, message_id)
        if linked_message is None:
            continue
        try:
            await linked_message.remove_reaction(payload.emoji, bot.user)
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not remove mirrored reaction %s from message %s",
                payload.emoji,
                message_id,
            )


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.guild_id is None:
        return

    link = await get_message_link(payload.message_id)
    if link is None:
        return

    link_group, source_channel_id, _, is_original = link
    if not is_original:
        return

    source_message = await fetch_linked_message(source_channel_id, payload.message_id)
    if source_message is None or source_message.author.bot or source_message.webhook_id is not None:
        return

    original_text = source_message.content.strip()
    attachments = attachment_lines(source_message)
    if not original_text and not attachments:
        return

    for message_id, channel_id, target_language, target_is_original in await get_linked_messages(link_group):
        if target_is_original:
            continue

        target_channel = await get_text_channel(channel_id)
        if target_channel is None:
            continue

        try:
            translated = await translate_text(original_text, target_language) if original_text else ""
            output = (translated + attachments).strip()
            webhook = await get_or_create_webhook(target_channel)
            await webhook.edit_message(
                message_id,
                content=output[:2000],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            log.warning("A translated message was already deleted: %s", message_id)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not synchronize edit to message %s", message_id)
        except Exception:
            log.exception("Could not translate edited message %s", payload.message_id)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return

    link = await get_message_link(payload.message_id)
    if link is None:
        return

    link_group, _, _, is_original = link
    if not is_original:
        return

    linked_messages = await get_linked_messages(link_group)
    # Remove database records first so the bot-triggered deletions below do not
    # recursively attempt to synchronize themselves.
    await delete_link_group(link_group)

    for message_id, channel_id, _, target_is_original in linked_messages:
        if target_is_original:
            continue

        target_channel = await get_text_channel(channel_id)
        if target_channel is None:
            continue

        try:
            webhook = await get_or_create_webhook(target_channel)
            await webhook.delete_message(message_id)
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not delete translated message %s", message_id)


def validate_settings():
    missing = []
    if not TOKEN:
        missing.append("DISCORD_BOT_TOKEN")
    if len(GENERAL_CHANNELS) != 3:
        missing.append("all three general channel IDs")

    if missing:
        print("\nSETUP IS INCOMPLETE")
        print("Missing:", ", ".join(missing))
        print("Open the .env file and fill in every value, then run START_BOT.bat again.\n")
        input("Press Enter to close...")
        raise SystemExit(1)


if __name__ == "__main__":
    validate_settings()
    bot.run(TOKEN) 
