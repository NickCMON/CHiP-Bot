import asyncio
import io
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from chip_translation import (
    AzureTranslator,
    TerraClarifier,
    TerraUsage,
    TranslationError,
    protect_text,
    restore_text,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cookie-translator")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()


def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        return default


# Mount a Railway volume at /data (or set CHIP_DATA_DIR) to keep usage totals,
# maintenance state, and message links across deployments.
DATA_DIR = Path(
    os.getenv("CHIP_DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or Path(__file__).parent
)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "message_links.db"

ANNOUNCEMENT_CHANNEL_ID = env_int(
    "ANNOUNCEMENT_CHANNEL_ID",
    1529511999891964015,
)
ADMIN_LOG_CHANNEL_ID = env_int("ADMIN_LOG_CHANNEL_ID")
OWNER_USER_ID = env_int("OWNER_USER_ID")

AZURE_TRANSLATOR_KEY = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
AZURE_TRANSLATOR_ENDPOINT = os.getenv(
    "AZURE_TRANSLATOR_ENDPOINT",
    "https://api.cognitive.microsofttranslator.com",
).strip()
AZURE_TRANSLATOR_REGION = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra").strip()
TERRA_BUDGET_DEFAULT = max(env_float("TERRA_BUDGET_USD", 4.50), 0.0)
MAX_RELAY_FILE_BYTES = max(env_int("MAX_RELAY_FILE_MB", 20), 1) * 1024 * 1024

GENERAL_CHANNELS: Dict[int, str] = {
    env_int("ENGLISH_CHANNEL_ID"): "en",
    env_int("SPANISH_CHANNEL_ID"): "es",
    env_int("PORTUGUESE_CHANNEL_ID"): "pt",
    env_int("FRENCH_CHANNEL_ID"): "fr",
    env_int("GERMAN_CHANNEL_ID"): "de",
    env_int("TURKISH_CHANNEL_ID"): "tr",
    env_int("ARABIC_CHANNEL_ID"): "ar",
    env_int("CHINESE_CHANNEL_ID"): "zh-CN",
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
    "R42", "Beta Arietis", "Orbital Dash", "Cybervault",
    "Felina", "Vypher", "Grunt", "Sancia", "Astraeus", "CHiP", "BiSCOFF",
]

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
webhook_cache: Dict[int, discord.Webhook] = {}
database_lock = asyncio.Lock()
terra_lock = asyncio.Lock()

azure_translator = AzureTranslator(
    key=AZURE_TRANSLATOR_KEY,
    endpoint=AZURE_TRANSLATOR_ENDPOINT,
    region=AZURE_TRANSLATOR_REGION,
)
terra_clarifier = TerraClarifier(
    api_key=OPENAI_API_KEY,
    model=OPENAI_MODEL,
)



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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS terra_usage (
                request_id TEXT PRIMARY KEY,
                source_message_id INTEGER NOT NULL,
                request_kind TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_terra_usage_created_at
            ON terra_usage(created_at)
            """
        )
        defaults = {
            "terra_budget_usd": f"{TERRA_BUDGET_DEFAULT:.6f}",
            "terra_paused": "0",
            "terra_alert_level": "0",
            "maintenance_active": "0",
            "maintenance_started_at": "",
            "maintenance_reason": "",
        }
        connection.executemany(
            """
            INSERT OR IGNORE INTO bot_settings (setting_key, setting_value)
            VALUES (?, ?)
            """,
            defaults.items(),
        )
        connection.commit()


async def initialize_database() -> None:
    await asyncio.to_thread(_initialize_database_sync)


def _get_setting_sync(setting_key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            "SELECT setting_value FROM bot_settings WHERE setting_key = ?",
            (setting_key,),
        ).fetchone()
    return str(row[0]) if row else default


async def get_setting(setting_key: str, default: str = "") -> str:
    async with database_lock:
        return await asyncio.to_thread(_get_setting_sync, setting_key, default)


def _set_settings_sync(values: Dict[str, str]) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.executemany(
            """
            INSERT INTO bot_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            [(key, value) for key, value in values.items()],
        )
        connection.commit()


async def set_settings(values: Dict[str, str]) -> None:
    async with database_lock:
        await asyncio.to_thread(_set_settings_sync, values)


@dataclass(frozen=True)
class TerraStatusSnapshot:
    budget_usd: float
    paused: bool
    alert_level: int
    total_cost_usd: float
    month_cost_usd: float
    total_requests: int
    total_messages: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int

    @property
    def remaining_usd(self) -> float:
        return max(self.budget_usd - self.total_cost_usd, 0.0)

    @property
    def percent_used(self) -> float:
        if self.budget_usd <= 0:
            return 100.0
        return min((self.total_cost_usd / self.budget_usd) * 100.0, 100.0)


def _usage_aggregate(
    connection: sqlite3.Connection,
    since: Optional[str] = None,
) -> Tuple[int, int, int, int, int, int, float]:
    query = """
        SELECT
            COUNT(*),
            COUNT(DISTINCT source_message_id),
            COALESCE(SUM(input_tokens), 0),
            COALESCE(SUM(cached_input_tokens), 0),
            COALESCE(SUM(cache_write_tokens), 0),
            COALESCE(SUM(output_tokens), 0),
            COALESCE(SUM(estimated_cost_usd), 0)
        FROM terra_usage
    """
    parameters: Tuple[str, ...] = ()
    if since is not None:
        query += " WHERE created_at >= ?"
        parameters = (since,)
    row = connection.execute(query, parameters).fetchone()
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        int(row[5]),
        float(row[6]),
    )


def _get_terra_status_sync() -> TerraStatusSnapshot:
    with sqlite3.connect(DB_PATH) as connection:
        settings = dict(
            connection.execute(
                """
                SELECT setting_key, setting_value
                FROM bot_settings
                WHERE setting_key IN (
                    'terra_budget_usd', 'terra_paused', 'terra_alert_level'
                )
                """
            ).fetchall()
        )
        total = _usage_aggregate(connection)
        now = datetime.now(timezone.utc)
        month_start = now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat()
        month = _usage_aggregate(connection, month_start)

    try:
        budget = max(float(settings.get("terra_budget_usd", TERRA_BUDGET_DEFAULT)), 0.0)
    except (TypeError, ValueError):
        budget = TERRA_BUDGET_DEFAULT
    try:
        alert_level = int(settings.get("terra_alert_level", "0"))
    except (TypeError, ValueError):
        alert_level = 0

    return TerraStatusSnapshot(
        budget_usd=budget,
        paused=settings.get("terra_paused", "0") == "1",
        alert_level=alert_level,
        total_cost_usd=total[6],
        month_cost_usd=month[6],
        total_requests=total[0],
        total_messages=total[1],
        input_tokens=total[2],
        cached_input_tokens=total[3],
        cache_write_tokens=total[4],
        output_tokens=total[5],
    )


async def get_terra_status() -> TerraStatusSnapshot:
    async with database_lock:
        return await asyncio.to_thread(_get_terra_status_sync)


def _record_terra_usage_sync(
    usage: TerraUsage,
    source_message_id: int,
    request_kind: str,
) -> None:
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO terra_usage (
                request_id,
                source_message_id,
                request_kind,
                input_tokens,
                cached_input_tokens,
                cache_write_tokens,
                output_tokens,
                estimated_cost_usd,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usage.request_id,
                source_message_id,
                request_kind,
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.cache_write_tokens,
                usage.output_tokens,
                usage.estimated_cost_usd,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()


async def record_terra_usage(
    usage: TerraUsage,
    source_message_id: int,
    request_kind: str,
) -> None:
    async with database_lock:
        await asyncio.to_thread(
            _record_terra_usage_sync,
            usage,
            source_message_id,
            request_kind,
        )


async def maintenance_state() -> Tuple[bool, str, str]:
    active = await get_setting("maintenance_active", "0") == "1"
    started_at = await get_setting("maintenance_started_at", "")
    reason = await get_setting("maintenance_reason", "")
    return active, started_at, reason


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
FRENCH_ROLE_ID = 1532391441148547245
GERMAN_ROLE_ID = 1532392352721932428
TURKISH_ROLE_ID = 1532392461291487232
ARABIC_ROLE_ID = 1532392586658971678
CHINESE_ROLE_ID = 1532392671266607164
LANGUAGE_ROLE_IDS = {
    ENGLISH_ROLE_ID,
    SPANISH_ROLE_ID,
    PORTUGUESE_ROLE_ID,
    FRENCH_ROLE_ID,
    GERMAN_ROLE_ID,
    TURKISH_ROLE_ID,
    ARABIC_ROLE_ID,
    CHINESE_ROLE_ID
}


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

    @discord.ui.button(
        label="Français",
        emoji="🇫🇷",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_french",
   )
    async def french_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, FRENCH_ROLE_ID, "Français")

    @discord.ui.button(
        label="Deutsch",
        emoji="🇩🇪",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_german",
   )
    async def german_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, GERMAN_ROLE_ID, "Deutsch")


    @discord.ui.button(
        label="Türkçe",
        emoji="🇹🇷",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_turkish",
   )
    async def turkish_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, TURKISH_ROLE_ID, "Türkçe")


    @discord.ui.button(
        label="العربية",
        emoji="🇸🇦",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_arabic",
   )
    async def arabic_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, ARABIC_ROLE_ID, "العربية")


    @discord.ui.button(
        label="中文",
        emoji="🇨🇳",
        style=discord.ButtonStyle.primary,
        custom_id="cmon_language_chinese",
   )
    async def chinese_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_language_role(interaction, CHINESE_ROLE_ID, "中文")


    @discord.ui.button(
        label="Clear All Languages",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        custom_id="cmon_language_clear",
   )
    async def clear_languages_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user

        roles_to_remove = [
            ENGLISH_ROLE_ID,
            SPANISH_ROLE_ID,
            PORTUGUESE_ROLE_ID,
            FRENCH_ROLE_ID,
            GERMAN_ROLE_ID,
            TURKISH_ROLE_ID,
            ARABIC_ROLE_ID,
            CHINESE_ROLE_ID,
        ]

        removed = []

        for role_id in roles_to_remove:
            role = interaction.guild.get_role(role_id)
            if role and role in member.roles:
                await member.remove_roles(role)
                removed.append(role.name)

        if removed:
            await interaction.response.send_message(
                "🗑️ All language roles have been removed.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You don't currently have any language roles.",
                 ephemeral=True,
            )


# Cosmetic cookie color roles. These roles should have no permissions enabled.
RED_VELVET_ROLE_ID = 1536150463429222471
COTTON_CANDY_ROLE_ID = 1536153800648302653
STRAWBERRY_CHEESECAKE_ROLE_ID = 1536158096752115794
LEMON_CRINKLE_ROLE_ID = 1536167299017347103
BLUEBERRY_MUFFIN_ROLE_ID = 1536169060251869284
CHOCOLATE_MINT_ROLE_ID = 1536169257610641408
MATCHA_ROLE_ID = 1536169515841360012
OATMEAL_RAISIN_ROLE_ID = 1536169728194908301
CARAMEL_PROTEIN_ROLE_ID = 1536169774180999168
PEANUT_BUTTER_ROLE_ID = 1536170400579457144
WHITE_CHOCOLATE_MACADAMIA_ROLE_ID = 1536170954202415236
OREO_ROLE_ID = 1536508913057533992
CHOCOLATE_CHIP_ROLE_ID = 1536509835439378462
VANILLA_BEAN_ROLE_ID = 1536510082769231902

COOKIE_COLOR_ROLE_IDS = {
    RED_VELVET_ROLE_ID,
    COTTON_CANDY_ROLE_ID,
    STRAWBERRY_CHEESECAKE_ROLE_ID,
    LEMON_CRINKLE_ROLE_ID,
    BLUEBERRY_MUFFIN_ROLE_ID,
    CHOCOLATE_MINT_ROLE_ID,
    MATCHA_ROLE_ID,
    OATMEAL_RAISIN_ROLE_ID,
    CARAMEL_PROTEIN_ROLE_ID,
    PEANUT_BUTTER_ROLE_ID,
    WHITE_CHOCOLATE_MACADAMIA_ROLE_ID,
    OREO_ROLE_ID,
    CHOCOLATE_CHIP_ROLE_ID,
    VANILLA_BEAN_ROLE_ID,
}


async def set_cookie_color_role(
    interaction: discord.Interaction,
    role_id: int,
    cookie_name: str,
):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This button can only be used inside the CMON server.",
            ephemeral=True,
        )
        return

    selected_role = interaction.guild.get_role(role_id)
    if selected_role is None:
        await interaction.response.send_message(
            "I could not find that cookie role. Please tell an administrator.",
            ephemeral=True,
        )
        return

    roles_to_remove = [
        role
        for role in interaction.user.roles
        if role.id in COOKIE_COLOR_ROLE_IDS and role.id != role_id
    ]

    try:
        if roles_to_remove:
            await interaction.user.remove_roles(
                *roles_to_remove,
                reason="Member changed cosmetic cookie color",
            )

        if selected_role not in interaction.user.roles:
            await interaction.user.add_roles(
                selected_role,
                reason=f"Member selected {cookie_name}",
            )

        await interaction.response.send_message(
            f"🍪 Your cookie color is now **{cookie_name}**!",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I cannot assign that role. Make sure CHiP's bot role is above all cookie color roles and has Manage Roles enabled.",
            ephemeral=True,
        )


async def clear_cookie_color_role(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This button can only be used inside the CMON server.",
            ephemeral=True,
        )
        return

    roles_to_remove = [
        role for role in interaction.user.roles if role.id in COOKIE_COLOR_ROLE_IDS
    ]

    try:
        if roles_to_remove:
            await interaction.user.remove_roles(
                *roles_to_remove,
                reason="Member returned to default Cookie color",
            )
            await interaction.response.send_message(
                "🍪 Your cosmetic cookie color was removed. You're back to the default Cookie color.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You're already using the default Cookie color.",
                ephemeral=True,
            )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I cannot remove that role. Make sure CHiP's bot role is above all cookie color roles and has Manage Roles enabled.",
            ephemeral=True,
        )


class CookieColorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Red Velvet", emoji="❤️", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_red_velvet", row=0)
    async def red_velvet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, RED_VELVET_ROLE_ID, "Red Velvet Cookie")

    @discord.ui.button(label="Cotton Candy", emoji="🍬", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_cotton_candy", row=0)
    async def cotton_candy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, COTTON_CANDY_ROLE_ID, "Cotton Candy Cookie")

    @discord.ui.button(label="Strawberry Cheesecake", emoji="🍓", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_strawberry_cheesecake", row=0)
    async def strawberry_cheesecake(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, STRAWBERRY_CHEESECAKE_ROLE_ID, "Strawberry Cheesecake Cookie")

    @discord.ui.button(label="Lemon Crinkle", emoji="🍋", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_lemon_crinkle", row=1)
    async def lemon_crinkle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, LEMON_CRINKLE_ROLE_ID, "Lemon Crinkle Cookie")

    @discord.ui.button(label="Blueberry Muffin", emoji="🫐", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_blueberry_muffin", row=1)
    async def blueberry_muffin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, BLUEBERRY_MUFFIN_ROLE_ID, "Blueberry Muffin Cookie")

    @discord.ui.button(label="Chocolate Mint", emoji="🌿", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_chocolate_mint", row=1)
    async def chocolate_mint(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, CHOCOLATE_MINT_ROLE_ID, "Chocolate Mint Cookie")

    @discord.ui.button(label="Matcha", emoji="🍵", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_matcha", row=2)
    async def matcha(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, MATCHA_ROLE_ID, "Matcha Cookie")

    @discord.ui.button(label="Oatmeal Raisin", emoji="🍇", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_oatmeal_raisin", row=2)
    async def oatmeal_raisin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, OATMEAL_RAISIN_ROLE_ID, "Oatmeal Raisin Cookie")

    @discord.ui.button(label="Caramel Protein", emoji="💪", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_caramel_protein", row=2)
    async def caramel_protein(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, CARAMEL_PROTEIN_ROLE_ID, "Caramel Protein Cookie")

    @discord.ui.button(label="Peanut Butter", emoji="🥜", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_peanut_butter", row=3)
    async def peanut_butter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, PEANUT_BUTTER_ROLE_ID, "Peanut Butter Cookie")

    @discord.ui.button(label="White Choco Macadamia", emoji="🤍", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_white_choco_macadamia", row=3)
    async def white_choco_macadamia(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, WHITE_CHOCOLATE_MACADAMIA_ROLE_ID, "White Chocolate Macadamia Cookie")

    @discord.ui.button(label="Oreo", emoji="⚫", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_oreo", row=3)
    async def oreo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, OREO_ROLE_ID, "Oreo Cookie")

    @discord.ui.button(label="Chocolate Chip", emoji="🍫", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_chocolate_chip", row=4)
    async def chocolate_chip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, CHOCOLATE_CHIP_ROLE_ID, "Chocolate Chip Cookie")

    @discord.ui.button(label="Vanilla Bean", emoji="🤍", style=discord.ButtonStyle.secondary, custom_id="cmon_cookie_vanilla_bean", row=4)
    async def vanilla_bean(self, interaction: discord.Interaction, button: discord.ui.Button):
        await set_cookie_color_role(interaction, VANILLA_BEAN_ROLE_ID, "Vanilla Bean Cookie")

    @discord.ui.button(label="Default Cookie", emoji="🍪", style=discord.ButtonStyle.danger, custom_id="cmon_cookie_default", row=4)
    async def default_cookie(self, interaction: discord.Interaction, button: discord.ui.Button):
        await clear_cookie_color_role(interaction)

async def send_admin_notice(message: str) -> None:
    """Send private Terra alerts to an admin destination, never public chat."""

    if ADMIN_LOG_CHANNEL_ID:
        channel = await get_text_channel(ADMIN_LOG_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                log.exception("Could not send CHiP's private admin alert.")

    owner: Optional[discord.abc.User] = None
    if OWNER_USER_ID:
        owner = bot.get_user(OWNER_USER_ID)
        if owner is None:
            try:
                owner = await bot.fetch_user(OWNER_USER_ID)
            except (discord.NotFound, discord.HTTPException):
                owner = None
    else:
        try:
            app_info = await bot.application_info()
            if isinstance(app_info.owner, (discord.User, discord.ClientUser)):
                owner = app_info.owner
        except discord.HTTPException:
            owner = None

    if owner is not None:
        try:
            await owner.send(message)
            return
        except (discord.Forbidden, discord.HTTPException):
            log.warning("CHiP could not DM its application owner with a Terra alert.")

    log.warning("ADMIN ALERT | %s", message.replace("\n", " | "))


async def check_terra_alerts(
    status: TerraStatusSnapshot,
    force_cutoff: bool = False,
) -> None:
    percent = status.percent_used
    reached = 100 if force_cutoff else 0
    for threshold in (50, 80, 95, 100):
        if percent >= threshold:
            reached = threshold

    if reached <= status.alert_level:
        return

    await set_settings({"terra_alert_level": str(reached)})
    cutoff_line = (
        "\nCHiP has switched Spanish messages to Azure-only fallback."
        if reached >= 100 or force_cutoff
        else ""
    )
    await send_admin_notice(
        "🍪 **CHiP Terra Budget Alert**\n"
        f"Usage has reached **{reached}%** of CHiP's local cap.\n"
        f"Tracked: **${status.total_cost_usd:.4f}** / "
        f"**${status.budget_usd:.2f}**{cutoff_line}"
    )


async def maybe_clarify_spanish(
    protected_text: str,
    source_message_id: int,
    request_kind: str,
) -> Optional[str]:
    """Use Terra once when configured and safely inside the local cost cap."""

    if not terra_clarifier.configured:
        return None

    async with terra_lock:
        status = await get_terra_status()
        if status.paused or status.budget_usd <= 0:
            return None

        request_ceiling = terra_clarifier.estimate_request_ceiling(protected_text)
        if status.total_cost_usd + request_ceiling > status.budget_usd:
            await check_terra_alerts(status, force_cutoff=True)
            log.info(
                "Terra skipped for budget safety: $%.4f tracked of $%.2f cap",
                status.total_cost_usd,
                status.budget_usd,
            )
            return None

        try:
            clarified, usage = await terra_clarifier.clarify_spanish(protected_text)
        except TranslationError:
            log.exception("Terra failed; CHiP is falling back to Azure for this message.")
            return None

        await record_terra_usage(usage, source_message_id, request_kind)
        updated_status = await get_terra_status()
        await check_terra_alerts(updated_status)
        return clarified


async def translate_for_targets(
    text: str,
    source_language: str,
    target_languages: Sequence[str],
    source_message_id: int,
    request_kind: str = "message",
) -> Dict[str, str]:
    """Translate once per source message and fan the results out by language."""

    protected_text, protected_values = protect_text(text, PROTECTED_TERMS)
    working_text = protected_text
    working_language = source_language

    if source_language == "es":
        clarified = await maybe_clarify_spanish(
            protected_text,
            source_message_id,
            request_kind,
        )
        if clarified:
            working_text = clarified
            working_language = "en"

    unique_targets = list(dict.fromkeys(target_languages))
    translated: Dict[str, str] = {}

    # If Terra already produced English, do not pay Azure to translate English
    # into English. Everything else is translated in one Azure request.
    for target in unique_targets:
        if target == working_language:
            translated[target] = working_text

    azure_targets = [
        target for target in unique_targets if target != working_language
    ]
    if azure_targets:
        try:
            translated.update(
                await azure_translator.translate_many(
                    working_text,
                    working_language,
                    azure_targets,
                )
            )
        except TranslationError:
            # Relay the original human message rather than an API error page or
            # silently dropping it. Details remain in private Railway logs.
            log.exception(
                "Azure translation failed from %s to %s; relaying original text.",
                working_language,
                ", ".join(azure_targets),
            )

    results: Dict[str, str] = {}
    for target in unique_targets:
        candidate = translated.get(target, protected_text)
        results[target] = restore_text(candidate, protected_values)
    return results


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


@dataclass(frozen=True)
class RelayFileData:
    data: bytes
    filename: str
    description: Optional[str] = None
    spoiler: bool = False


@dataclass
class RelayMedia:
    files: List[RelayFileData] = field(default_factory=list)
    fallback_urls: List[str] = field(default_factory=list)
    all_urls: List[str] = field(default_factory=list)
    consumed_text_tokens: List[str] = field(default_factory=list)

    @property
    def has_media(self) -> bool:
        return bool(self.files or self.fallback_urls)

    def discord_files(self) -> List[discord.File]:
        return [
            discord.File(
                io.BytesIO(item.data),
                filename=item.filename,
                description=item.description,
                spoiler=item.spoiler,
            )
            for item in self.files
        ]


def safe_sticker_filename(name: str, extension: str) -> str:
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in name
    ).strip("_")
    return f"{safe_name[:60] or 'sticker'}.{extension}"


CUSTOM_EMOJI_PATTERN = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_~]+):(?P<id>\d+)>"
)


def strip_relayed_custom_emojis(text: str, tokens: Sequence[str]) -> str:
    """Remove custom emoji markup that CHiP re-uploaded as image files."""

    cleaned = text
    for token in tokens:
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    return cleaned.strip()


async def collect_relay_media(message: discord.Message) -> RelayMedia:
    """Download uploadable media once; retain CDN URLs as a safe fallback."""

    media = RelayMedia()
    guild_limit = getattr(message.guild, "filesize_limit", MAX_RELAY_FILE_BYTES)
    upload_limit = min(int(guild_limit), MAX_RELAY_FILE_BYTES)

    for attachment in message.attachments:
        url = str(attachment.url)
        media.all_urls.append(url)
        if len(media.files) >= 10 or attachment.size > upload_limit:
            media.fallback_urls.append(url)
            continue
        try:
            try:
                data = await attachment.read()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                data = await attachment.read(use_cached=True)
            media.files.append(
                RelayFileData(
                    data=data,
                    filename=attachment.filename,
                    description=attachment.description,
                    spoiler=attachment.is_spoiler(),
                )
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.exception("Could not download attachment %s for relay.", attachment.id)
            media.fallback_urls.append(url)

    sticker_extensions = {
        "png": "png",
        "apng": "png",
        "gif": "gif",
        "lottie": "json",
    }
    for sticker in message.stickers:
        url = str(sticker.url)
        media.all_urls.append(url)
        format_name = getattr(sticker.format, "name", "png").lower()
        extension = sticker_extensions.get(format_name, "png")

        # Lottie sticker JSON is not useful as an uploaded Discord image, so its
        # CDN link is used instead. PNG/APNG/GIF stickers are re-uploaded.
        if format_name == "lottie" or len(media.files) >= 10:
            media.fallback_urls.append(url)
            continue
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise TranslationError(
                            f"Discord sticker CDN returned HTTP {response.status}."
                        )
                    data = await response.read()
            if len(data) > upload_limit:
                media.fallback_urls.append(url)
                continue
            media.files.append(
                RelayFileData(
                    data=data,
                    filename=safe_sticker_filename(sticker.name, extension),
                    description=f"Sticker: {sticker.name}",
                )
            )
        except (
            TranslationError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            discord.NotFound,
            discord.HTTPException,
        ):
            log.exception("Could not download sticker %s for relay.", sticker.id)
            media.fallback_urls.append(url)

    # Webhooks cannot reliably reuse animated or external custom emojis. When
    # Discord receives markup such as <a:relaxing:123>, it can degrade to the
    # literal text :relaxing:. Download the emoji from Discord's CDN and
    # re-upload it as a GIF/PNG so its animation and artwork survive the relay.
    seen_emoji_ids = set()
    for match in CUSTOM_EMOJI_PATTERN.finditer(getattr(message, "content", "") or ""):
        emoji_id = match.group("id")
        if emoji_id in seen_emoji_ids:
            continue
        seen_emoji_ids.add(emoji_id)

        token = match.group(0)
        animated = bool(match.group("animated"))
        extension = "gif" if animated else "png"
        name = match.group("name")
        url = (
            f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}"
            "?quality=lossless"
        )
        media.all_urls.append(url)
        media.consumed_text_tokens.append(token)

        if len(media.files) >= 10:
            media.fallback_urls.append(url)
            continue

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise TranslationError(
                            f"Discord emoji CDN returned HTTP {response.status}."
                        )
                    data = await response.read()
            if len(data) > upload_limit:
                media.fallback_urls.append(url)
                continue
            media.files.append(
                RelayFileData(
                    data=data,
                    filename=safe_sticker_filename(
                        f"emoji_{name}_{emoji_id}", extension
                    ),
                    description=f"Emoji: {name}",
                )
            )
        except (
            TranslationError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ):
            log.exception("Could not download custom emoji %s for relay.", emoji_id)
            media.fallback_urls.append(url)

    return media


def compose_relay_content(text: str, urls: Sequence[str]) -> Optional[str]:
    """Keep media URLs whole while staying inside Discord's 2,000-char limit."""

    clean_text = text.strip()
    clean_urls = [url.strip() for url in urls if url.strip()]
    suffix = "\n".join(clean_urls)

    if not suffix:
        return clean_text[:2000] or None
    if not clean_text:
        return suffix[:2000]

    available = 2000 - len(suffix) - 1
    if available <= 0:
        return suffix[:2000]
    if len(clean_text) > available:
        clean_text = clean_text[: max(available - 1, 0)].rstrip() + "…"
    return f"{clean_text}\n{suffix}"


async def send_webhook_relay(
    webhook: discord.Webhook,
    translated_text: str,
    media: RelayMedia,
    username: str,
    avatar_url: str,
) -> discord.WebhookMessage:
    content = compose_relay_content(translated_text, media.fallback_urls)
    files = media.discord_files()
    common_arguments = {
        "username": username,
        "avatar_url": avatar_url,
        "allowed_mentions": discord.AllowedMentions.none(),
        "wait": True,
    }
    try:
        if files:
            return await webhook.send(content=content, files=files, **common_arguments)
        return await webhook.send(content=content, **common_arguments)
    except discord.Forbidden:
        raise
    except discord.HTTPException:
        if not files:
            raise
        log.exception("Media upload failed; retrying the relay with safe CDN links.")
        fallback_content = compose_relay_content(translated_text, media.all_urls)
        return await webhook.send(content=fallback_content, **common_arguments)


async def edit_webhook_relay(
    webhook: discord.Webhook,
    message_id: int,
    translated_text: str,
    media: RelayMedia,
) -> None:
    content = compose_relay_content(translated_text, media.fallback_urls)
    files = media.discord_files()
    try:
        await webhook.edit_message(
            message_id,
            content=content,
            attachments=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        raise
    except discord.HTTPException:
        if not files:
            raise
        log.exception("Edited media upload failed; using safe CDN links instead.")
        fallback_content = compose_relay_content(translated_text, media.all_urls)
        await webhook.edit_message(
            message_id,
            content=fallback_content,
            attachments=[],
            allowed_mentions=discord.AllowedMentions.none(),
        )


MAINTENANCE_START_NOTICES = {
    "en": (
        "🛠️ **CHiP Maintenance Notice**\n"
        "CHiP is being updated to improve translations and the sharing of "
        "emojis, stickers, GIFs, and attachments. Translation may be temporarily "
        "unavailable. We’ll let you know when CHiP is back online!"
    ),
    "es": (
        "🛠️ **Aviso de mantenimiento de CHiP**\n"
        "CHiP se está actualizando para mejorar las traducciones y el envío de "
        "emojis, stickers, GIFs y archivos adjuntos. La traducción puede dejar de "
        "estar disponible temporalmente. ¡Les avisaremos cuando CHiP vuelva!"
    ),
    "pt": (
        "🛠️ **Aviso de manutenção do CHiP**\n"
        "O CHiP está sendo atualizado para melhorar as traduções e o envio de "
        "emojis, stickers, GIFs e anexos. A tradução pode ficar temporariamente "
        "indisponível. Avisaremos quando o CHiP voltar!"
    ),
    "fr": (
        "🛠️ **Avis de maintenance de CHiP**\n"
        "CHiP est en cours de mise à jour afin d’améliorer les traductions et le "
        "partage des émojis, stickers, GIF et pièces jointes. La traduction peut "
        "être temporairement indisponible. Nous vous préviendrons dès son retour !"
    ),
    "de": (
        "🛠️ **CHiP-Wartungshinweis**\n"
        "CHiP wird aktualisiert, um Übersetzungen sowie das Teilen von Emojis, "
        "Stickern, GIFs und Anhängen zu verbessern. Übersetzungen können "
        "vorübergehend nicht verfügbar sein. Wir melden uns, sobald CHiP zurück ist!"
    ),
    "tr": (
        "🛠️ **CHiP Bakım Duyurusu**\n"
        "Çevirileri ve emoji, çıkartma, GIF ile dosya paylaşımını iyileştirmek için "
        "CHiP güncelleniyor. Çeviri geçici olarak kullanılamayabilir. CHiP tekrar "
        "çevrimiçi olduğunda size haber vereceğiz!"
    ),
    "ar": (
        "🛠️ **إشعار صيانة CHiP**\n"
        "يتم تحديث CHiP لتحسين الترجمة ومشاركة الرموز التعبيرية والملصقات وصور "
        "GIF والمرفقات. قد تتوقف الترجمة مؤقتًا. سنخبركم عندما يعود CHiP للعمل!"
    ),
    "zh-CN": (
        "🛠️ **CHiP 维护通知**\n"
        "CHiP 正在更新，以改进翻译以及表情、贴纸、GIF 和附件的转发功能。"
        "翻译服务可能暂时不可用。CHiP 恢复后我们会通知大家！"
    ),
}

MAINTENANCE_END_NOTICES = {
    "en": "✅ **CHiP is back online!** Translation and media relay have resumed.",
    "es": "✅ **¡CHiP volvió a estar en línea!** Las traducciones y el envío de archivos se reanudaron.",
    "pt": "✅ **O CHiP está online novamente!** As traduções e o envio de arquivos foram retomados.",
    "fr": "✅ **CHiP est de nouveau en ligne !** Les traductions et le partage de fichiers ont repris.",
    "de": "✅ **CHiP ist wieder online!** Übersetzungen und die Medienweiterleitung wurden fortgesetzt.",
    "tr": "✅ **CHiP tekrar çevrimiçi!** Çeviri ve medya aktarımı yeniden başladı.",
    "ar": "✅ **عاد CHiP للعمل!** تم استئناف الترجمة وإرسال الوسائط.",
    "zh-CN": "✅ **CHiP 已恢复上线！** 翻译和媒体转发已恢复。",
}


async def post_chip_announcement(
    title: str,
    description: str,
    color: discord.Color,
) -> bool:
    channel = await get_text_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        log.error(
            "Cannot access CHiP announcement channel ID %s.",
            ANNOUNCEMENT_CHANNEL_ID,
        )
        return False

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not post in CHiP's announcement channel.")
        return False


async def broadcast_language_notice(notices: Dict[str, str]) -> int:
    sent_channel_ids = set()
    sent_count = 0
    for channels in TRANSLATION_GROUPS.values():
        for channel_id, language in channels.items():
            if channel_id in sent_channel_ids:
                continue
            sent_channel_ids.add(channel_id)
            channel = await get_text_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(
                    notices.get(language, notices["en"]),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent_count += 1
            except (discord.Forbidden, discord.HTTPException):
                log.exception(
                    "Could not send maintenance notice to channel %s.",
                    channel_id,
                )
    return sent_count


maintenance_group = app_commands.Group(
    name="maintenance",
    description="Control CHiP maintenance mode.",
)


@maintenance_group.command(name="start", description="Announce and start maintenance mode.")
@app_commands.describe(reason="Optional reason members should see in the CHiP announcement.")
@app_commands.checks.has_permissions(administrator=True)
async def maintenance_start(
    interaction: discord.Interaction,
    reason: str = "",
) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    active, started_at, _ = await maintenance_state()
    if active:
        started_text = f" since {started_at}" if started_at else ""
        await interaction.followup.send(
            f"CHiP is already in maintenance mode{started_text}.",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    clean_reason = reason.strip()[:800]
    await set_settings(
        {
            "maintenance_active": "1",
            "maintenance_started_at": now.isoformat(),
            "maintenance_reason": clean_reason,
        }
    )
    await bot.change_presence(
        status=discord.Status.dnd,
        activity=discord.Game(name="🛠️ Under Maintenance"),
    )

    description = (
        "CHiP is undergoing updates. Translation and media relay may be "
        "temporarily unavailable."
    )
    if clean_reason:
        description += f"\n\n**Reason:** {clean_reason}"

    announced = await post_chip_announcement(
        "🛠️ CHiP Maintenance Started",
        description,
        discord.Color.orange(),
    )
    language_count = await broadcast_language_notice(MAINTENANCE_START_NOTICES)
    announcement_result = "posted" if announced else "could not be posted"
    await interaction.followup.send(
        "Maintenance mode is now **ON**. "
        f"The CHiP announcement {announcement_result}, and localized notices "
        f"were sent to **{language_count}** translation channels.",
        ephemeral=True,
    )


@maintenance_group.command(name="end", description="End maintenance and announce CHiP's return.")
@app_commands.checks.has_permissions(administrator=True)
async def maintenance_end(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    active, _, _ = await maintenance_state()
    if not active:
        await interaction.followup.send(
            "CHiP is not currently in maintenance mode.",
            ephemeral=True,
        )
        return

    await set_settings(
        {
            "maintenance_active": "0",
            "maintenance_started_at": "",
            "maintenance_reason": "",
        }
    )
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name="🍪 Translating CMON"),
    )
    announced = await post_chip_announcement(
        "✅ CHiP Maintenance Complete",
        "CHiP is back online. Translation and media relay have resumed.",
        discord.Color.green(),
    )
    language_count = await broadcast_language_notice(MAINTENANCE_END_NOTICES)
    announcement_result = "posted" if announced else "could not be posted"
    await interaction.followup.send(
        "Maintenance mode is now **OFF**. "
        f"The CHiP announcement {announcement_result}, and return notices were "
        f"sent to **{language_count}** translation channels.",
        ephemeral=True,
    )


@maintenance_group.command(name="status", description="Check CHiP's maintenance status.")
@app_commands.checks.has_permissions(administrator=True)
async def maintenance_status(interaction: discord.Interaction) -> None:
    active, started_at, reason = await maintenance_state()
    if active:
        details = f"Maintenance is **ON**.\nStarted: `{started_at or 'Unknown'}`"
        if reason:
            details += f"\nReason: {reason}"
    else:
        details = "Maintenance is **OFF**. CHiP is translating normally."
    await interaction.response.send_message(details, ephemeral=True)


terra_group = app_commands.Group(
    name="terra",
    description="Track and control CHiP's Terra usage.",
)


@terra_group.command(name="status", description="Show Terra usage and remaining local budget.")
@app_commands.checks.has_permissions(administrator=True)
async def terra_status_command(interaction: discord.Interaction) -> None:
    status = await get_terra_status()
    if not terra_clarifier.configured:
        mode = "⚪ Not configured — Azure-only"
    elif status.paused:
        mode = "⏸️ Paused — Azure-only"
    elif status.remaining_usd <= 0:
        mode = "🛑 Local cap reached — Azure-only"
    else:
        mode = "🟢 Active for Spanish messages"

    embed = discord.Embed(
        title="🍪 CHiP Terra Usage",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Mode", value=mode, inline=False)
    embed.add_field(
        name="Tracked total",
        value=f"${status.total_cost_usd:.4f} / ${status.budget_usd:.2f}",
        inline=True,
    )
    embed.add_field(
        name="Remaining",
        value=f"${status.remaining_usd:.4f}",
        inline=True,
    )
    embed.add_field(
        name="This month",
        value=f"${status.month_cost_usd:.4f}",
        inline=True,
    )
    embed.add_field(
        name="Spanish handled",
        value=f"{status.total_messages} messages / {status.total_requests} requests",
        inline=False,
    )
    embed.add_field(
        name="Tokens",
        value=(
            f"Input: {status.input_tokens:,}\n"
            f"Cached input: {status.cached_input_tokens:,}\n"
            f"Cache writes: {status.cache_write_tokens:,}\n"
            f"Output: {status.output_tokens:,}"
        ),
        inline=False,
    )
    embed.set_footer(
        text="CHiP's estimate; the OpenAI Usage dashboard is the billing source of truth."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@terra_group.command(name="limit", description="Change CHiP's cumulative Terra spending cap.")
@app_commands.describe(amount_usd="New cumulative cap in US dollars, such as 4.50 or 9.50.")
@app_commands.checks.has_permissions(administrator=True)
async def terra_limit_command(
    interaction: discord.Interaction,
    amount_usd: float,
) -> None:
    if amount_usd < 0.10 or amount_usd > 1000:
        await interaction.response.send_message(
            "Choose a limit between **$0.10 and $1,000.00**.",
            ephemeral=True,
        )
        return

    await set_settings(
        {
            "terra_budget_usd": f"{amount_usd:.6f}",
            "terra_alert_level": "0",
        }
    )
    status = await get_terra_status()
    mode_note = (
        " The new cap is already at or below tracked usage, so CHiP will use "
        "Azure-only until you raise it."
        if status.total_cost_usd >= status.budget_usd
        else ""
    )
    await interaction.response.send_message(
        f"CHiP's cumulative Terra cap is now **${amount_usd:.2f}**.{mode_note}\n"
        "This command does **not** add OpenAI credit or charge your card.",
        ephemeral=True,
    )


@terra_group.command(name="pause", description="Pause Terra and use Azure for all languages.")
@app_commands.checks.has_permissions(administrator=True)
async def terra_pause_command(interaction: discord.Interaction) -> None:
    await set_settings({"terra_paused": "1"})
    await interaction.response.send_message(
        "Terra is **paused**. Spanish messages will use Azure-only fallback.",
        ephemeral=True,
    )


@terra_group.command(name="resume", description="Resume Terra for Spanish messages.")
@app_commands.checks.has_permissions(administrator=True)
async def terra_resume_command(interaction: discord.Interaction) -> None:
    if not terra_clarifier.configured:
        await interaction.response.send_message(
            "Terra cannot resume because `OPENAI_API_KEY` is not configured in Railway.",
            ephemeral=True,
        )
        return
    status = await get_terra_status()
    if status.remaining_usd <= 0:
        await interaction.response.send_message(
            "Terra cannot resume because CHiP's local cap has been reached. "
            "Add prepaid credit first, then raise `/terra limit`.",
            ephemeral=True,
        )
        return
    await set_settings({"terra_paused": "0"})
    await interaction.response.send_message(
        "Terra is **active** again for Spanish messages.",
        ephemeral=True,
    )


bot.tree.add_command(maintenance_group)
bot.tree.add_command(terra_group)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Only a server administrator can use that CHiP command."
    else:
        message = "CHiP could not complete that command. Check the private bot logs."
        original_error = getattr(error, "original", error)
        log.error(
            "Slash command failed: %s",
            original_error,
            exc_info=(
                type(original_error),
                original_error,
                original_error.__traceback__,
            ),
        )

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def sync_application_commands() -> None:
    if getattr(bot, "application_commands_synced", False):
        return

    announcement_channel = await get_text_channel(ANNOUNCEMENT_CHANNEL_ID)
    if announcement_channel is not None:
        guild_object = discord.Object(id=announcement_channel.guild.id)
        bot.tree.copy_global_to(guild=guild_object)
        synced = await bot.tree.sync(guild=guild_object)
        log.info(
            "Synced %s slash commands to guild %s.",
            len(synced),
            announcement_channel.guild.id,
        )
    else:
        synced = await bot.tree.sync()
        log.info("Synced %s global slash commands.", len(synced))
    bot.application_commands_synced = True


@bot.event
async def on_ready():
    await initialize_database()

    if not getattr(bot, "language_view_added", False):
        bot.add_view(LanguageView())
        bot.language_view_added = True

    if not getattr(bot, "cookie_color_view_added", False):
        bot.add_view(CookieColorView())
        bot.cookie_color_view_added = True

    maintenance_active, _, _ = await maintenance_state()
    if maintenance_active:
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Game(name="🛠️ Under Maintenance"),
        )
    else:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game(name="🍪 Translating CMON"),
        )

    try:
        await sync_application_commands()
    except discord.HTTPException:
        log.exception("Could not sync CHiP's slash commands.")

    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Configured translation groups: %s", TRANSLATION_GROUPS)
    log.info(
        "Translation providers | Azure: %s | Terra: %s | Terra model: %s",
        azure_translator.configured,
        terra_clarifier.configured,
        OPENAI_MODEL,
    )
    print("\nCOOKIE TRANSLATOR IS ONLINE")
    print(f"Logged in as: {bot.user}")
    print("CHiP is ready.\n")


@bot.command(name="languagepanel")
@commands.has_permissions(administrator=True)
async def language_panel(ctx: commands.Context):
        message = (
            "🌍 **Choose Your Language**\n\n"
            "Welcome to CMON! Choose one or more languages below.\n"
            "You can select multiple languages and change them at any time.\n\n"
            "🇺🇸 English\n"
            "🇪🇸 Español\n"
            "🇵🇹 Português\n"
            "🇫🇷 Français\n"
            "🇩🇪 Deutsch\n"
            "🇹🇷 Türkçe\n"
            "🇸🇦 العربية\n"
            "🇨🇳 中文\n\n"
            "🗑️ Clear All Languages"
        )
        
        await ctx.send(message, view=LanguageView())


@language_panel.error
async def language_panel_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Only a server administrator can post the language panel.")
    else:
        raise error


@bot.command(name="cookiepanel")
@commands.has_permissions(administrator=True)
async def cookie_panel(ctx: commands.Context):
    message = (
        "🍪 **Choose Your Cookie Color!**\n\n"
        "Pick your favorite cookie below to customize your name color in CMON.\n"
        "These roles are cosmetic only and do **not** change your permissions or channel access.\n"
        "Choosing a new cookie automatically replaces your previous cookie color.\n\n"
        "❤️ Red Velvet • 🍬 Cotton Candy • 🍓 Strawberry Cheesecake\n"
        "🍋 Lemon Crinkle • 🫐 Blueberry Muffin • 🌿 Chocolate Mint\n"
        "🍵 Matcha • 🍇 Oatmeal Raisin • 💪 Caramel Protein\n"
        "🥜 Peanut Butter • 🤍 White Chocolate Macadamia • ⚫ Oreo\n"
        "🍫 Chocolate Chip • 🤍 Vanilla Bean\n\n"
        "🍪 **Default Cookie** removes your cosmetic color and returns you to the normal Cookie role color."
    )
    await ctx.send(message, view=CookieColorView())


@cookie_panel.error
async def cookie_panel_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Only a server administrator can post the cookie color panel.")
    else:
        raise error

@bot.command(name="cookieupdate")
@commands.has_permissions(administrator=True)
async def cookie_update(ctx: commands.Context):
    message = (
        "@everyone\n\n"
        "🍪 **CMON COOKIE UPDATE!** 🍪\n\n"
        "New cookie colors are officially live!\n\n"
        "Head over to **#choose-your-cookie-flavor** and pick your favorite "
        "cookie to customize your name color.\n\n"
        "❤️ Red Velvet • 🍬 Cotton Candy • 🍓 Strawberry Cheesecake\n"
        "🍋 Lemon Crinkle • 🫐 Blueberry Muffin • 🌿 Chocolate Mint\n"
        "🍵 Matcha • 🍇 Oatmeal Raisin • 💪 Caramel Protein\n"
        "🥜 Peanut Butter • 🤍 White Chocolate Macadamia • ⚫ Oreo\n"
        "🍫 Chocolate Chip • 🤍 Vanilla Bean\n\n"
        "Your cookie flavor is completely cosmetic and will **not change "
        "your permissions or server access**. You can change it whenever you want.\n\n"
        "🍪 **Choose your flavor and show it off!**"
    )

    await ctx.send(
        message,
        allowed_mentions=discord.AllowedMentions(everyone=True)
    )


@cookie_update.error
async def cookie_update_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            "Only a server administrator can send CMON update announcements."
        )
    else:
        raise error
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id is not None:
        return

    # Preserve CHiP's existing !commands without relaying successful commands
    # into every translation channel.
    context = await bot.get_context(message)
    if context.valid:
        await bot.invoke(context)
        return

    # Ignore DMs and channels outside the translation system.
    if message.guild is None:
        return

    maintenance_active, _, _ = await maintenance_state()
    if maintenance_active:
        return

    active_group = None
    for group_name, channels in TRANSLATION_GROUPS.items():
        if message.channel.id in channels:
            active_group = (group_name, channels)
            break

    if active_group is None:
        return

    group_name, group_channels = active_group
    media = await collect_relay_media(message)
    original_text = strip_relayed_custom_emojis(
        message.content,
        media.consumed_text_tokens,
    )

    # Media-only messages bypass both translation providers entirely.
    if not original_text and not media.has_media:
        return

    source_language = group_channels[message.channel.id]
    username = message.author.display_name[:80]
    avatar_url = str(message.author.display_avatar.url)
    link_group = uuid.uuid4().hex

    target_languages = [
        language
        for channel_id, language in group_channels.items()
        if channel_id != message.channel.id
    ]
    translations = (
        await translate_for_targets(
            original_text,
            source_language,
            target_languages,
            message.id,
            "message",
        )
        if original_text
        else {}
    )

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
            webhook = await get_or_create_webhook(target_channel)
            translated_message = await send_webhook_relay(
                webhook=webhook,
                translated_text=translations.get(target_language, ""),
                media=media,
                username=username,
                avatar_url=avatar_url,
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

    # Discord also emits MESSAGE_UPDATE when a GIF/link embed finishes loading.
    # Ignore those metadata-only updates so they do not create extra Terra calls.
    if "content" not in payload.data:
        return

    maintenance_active, _, _ = await maintenance_state()
    if maintenance_active:
        return

    link = await get_message_link(payload.message_id)
    if link is None:
        return

    link_group, source_channel_id, source_language, is_original = link
    if not is_original:
        return

    source_message = await fetch_linked_message(source_channel_id, payload.message_id)
    if source_message is None or source_message.author.bot or source_message.webhook_id is not None:
        return

    media = await collect_relay_media(source_message)
    original_text = strip_relayed_custom_emojis(
        source_message.content,
        media.consumed_text_tokens,
    )
    if not original_text and not media.has_media:
        return

    linked_messages = await get_linked_messages(link_group)
    target_languages = [
        language
        for _, _, language, target_is_original in linked_messages
        if not target_is_original
    ]
    translations = (
        await translate_for_targets(
            original_text,
            source_language,
            target_languages,
            source_message.id,
            "edit",
        )
        if original_text
        else {}
    )

    for message_id, channel_id, target_language, target_is_original in linked_messages:
        if target_is_original:
            continue

        target_channel = await get_text_channel(channel_id)
        if target_channel is None:
            continue

        try:
            webhook = await get_or_create_webhook(target_channel)
            await edit_webhook_relay(
                webhook=webhook,
                message_id=message_id,
                translated_text=translations.get(target_language, ""),
                media=media,
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
    if not GENERAL_CHANNELS:
        missing.append("general channel IDs")

    if missing:
        print("\nSETUP IS INCOMPLETE")
        print("Missing:", ", ".join(missing))
        print("Open the .env file and fill in every value, then run START_BOT.bat again.\n")
        input("Press Enter to close...")
        raise SystemExit(1)


if __name__ == "__main__":
    validate_settings()
    bot.run(TOKEN) 
