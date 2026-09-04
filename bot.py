import asyncio
import io
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from chip_translation import (
    AzureTranslator,
    TerraClarifier,
    TerraUsage,
    TranslationError,
    protect_text,
    restore_text,
)
from chip_contests import (
    CONTEST_COLLAGE_CELL_SIZE,
    CONTEST_COLLAGE_COLUMNS,
    CONTEST_COLLAGE_GAP,
    CONTEST_ENTRIES_PER_POST,
    ContestEntryRecord,
    ContestError,
    ContestRecord,
    ContestStateError,
    ContestStore,
    ContestTally,
    CONTEST_FINAL_REMINDER_HOURS,
    CONTEST_REMINDER_INTERVAL_HOURS,
    build_contest_collage,
    contest_entry_batches,
)
from chip_chat import (
    ChatError,
    ChatHistoryStore,
    ChatUsage,
    ChipChatClient,
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
CHIP_CHAT_MODEL = (
    os.getenv("CHIP_CHAT_MODEL", "gpt-5.6-luna").strip()
    or "gpt-5.6-luna"
)
TERRA_BUDGET_DEFAULT = max(env_float("TERRA_BUDGET_USD", 4.50), 0.0)
CHIP_CHAT_CHANNEL_DEFAULT = env_int("CHIP_CHAT_CHANNEL_ID")
CHIP_CHAT_COOLDOWN_SECONDS = max(
    env_float("CHIP_CHAT_COOLDOWN_SECONDS", 15.0),
    5.0,
)
CHIP_CHAT_MAX_OUTPUT_TOKENS = max(
    env_int("CHIP_CHAT_MAX_OUTPUT_TOKENS", 500),
    64,
)
CHIP_CHAT_MAX_PROMPT_CHARACTERS = 1500
MAX_RELAY_FILE_BYTES = max(env_int("MAX_RELAY_FILE_MB", 20), 1) * 1024 * 1024
MAX_CONTEST_IMAGE_BYTES = (
    max(env_int("MAX_CONTEST_IMAGE_MB", 10), 1) * 1024 * 1024
)

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
ai_budget_lock = asyncio.Lock()
contest_action_lock = asyncio.Lock()
chip_chat_cooldowns: Dict[Tuple[int, int], float] = {}
contest_store = ContestStore(DB_PATH)
chat_history_store = ChatHistoryStore(DB_PATH)

azure_translator = AzureTranslator(
    key=AZURE_TRANSLATOR_KEY,
    endpoint=AZURE_TRANSLATOR_ENDPOINT,
    region=AZURE_TRANSLATOR_REGION,
)
terra_clarifier = TerraClarifier(
    api_key=OPENAI_API_KEY,
    model=OPENAI_MODEL,
)
chip_chat_client = ChipChatClient(
    api_key=OPENAI_API_KEY,
    model=CHIP_CHAT_MODEL,
    max_output_tokens=CHIP_CHAT_MAX_OUTPUT_TOKENS,
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
            "chip_chat_paused": "0",
            "chip_chat_channel_id": str(CHIP_CHAT_CHANNEL_DEFAULT or 0),
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
    await asyncio.gather(
        contest_store.initialize(),
        chat_history_store.initialize(),
    )


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
    terra_requests: int
    terra_messages: int
    terra_cost_usd: float
    chat_requests: int
    chat_messages: int
    chat_cost_usd: float

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
    request_kind: Optional[str] = None,
    exclude_request_kind: Optional[str] = None,
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
    clauses: List[str] = []
    parameters: List[str] = []
    if since is not None:
        clauses.append("created_at >= ?")
        parameters.append(since)
    if request_kind is not None:
        clauses.append("request_kind = ?")
        parameters.append(request_kind)
    if exclude_request_kind is not None:
        clauses.append("request_kind != ?")
        parameters.append(exclude_request_kind)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    row = connection.execute(query, tuple(parameters)).fetchone()
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
        terra = _usage_aggregate(
            connection,
            exclude_request_kind="chip_chat",
        )
        chat = _usage_aggregate(
            connection,
            request_kind="chip_chat",
        )

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
        terra_requests=terra[0],
        terra_messages=terra[1],
        terra_cost_usd=terra[6],
        chat_requests=chat[0],
        chat_messages=chat[1],
        chat_cost_usd=chat[6],
    )


async def get_terra_status() -> TerraStatusSnapshot:
    async with database_lock:
        return await asyncio.to_thread(_get_terra_status_sync)


AiUsage = Union[TerraUsage, ChatUsage]


def _record_ai_usage_sync(
    usage: AiUsage,
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


async def record_ai_usage(
    usage: AiUsage,
    source_message_id: int,
    request_kind: str,
) -> None:
    async with database_lock:
        await asyncio.to_thread(
            _record_ai_usage_sync,
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
    """Send private AI-cost alerts to an admin destination, never public chat."""

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
            log.warning("CHiP could not DM its application owner with an AI alert.")

    log.warning("ADMIN ALERT | %s", message.replace("\n", " | "))


async def check_ai_budget_alerts(
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
        "\nMember chat is unavailable at the cap, and Spanish messages have "
        "switched to Azure-only fallback."
        if reached >= 100 or force_cutoff
        else ""
    )
    await send_admin_notice(
        "🍪 **CHiP AI Budget Alert**\n"
        f"Combined Terra and member-chat usage has reached **{reached}%** of "
        "CHiP's local cap.\n"
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

    async with ai_budget_lock:
        status = await get_terra_status()
        if status.paused or status.budget_usd <= 0:
            return None

        request_ceiling = terra_clarifier.estimate_request_ceiling(protected_text)
        if status.total_cost_usd + request_ceiling > status.budget_usd:
            await check_ai_budget_alerts(status, force_cutoff=True)
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

        await record_ai_usage(usage, source_message_id, request_kind)
        updated_status = await get_terra_status()
        await check_ai_budget_alerts(updated_status)
        return clarified


class ChipChatUnavailable(RuntimeError):
    """A member-safe reason CHiP chat cannot answer right now."""


async def get_chip_chat_channel_id() -> int:
    raw_value = await get_setting(
        "chip_chat_channel_id",
        str(CHIP_CHAT_CHANNEL_DEFAULT or 0),
    )
    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return 0


def strip_chip_mention(text: str, bot_user_id: int) -> str:
    return re.sub(rf"<@!?{bot_user_id}>", "", text).strip()


def split_discord_text(text: str, limit: int = 1850) -> List[str]:
    """Split a reply without exceeding Discord's 2,000-character limit."""

    remaining = text.strip()
    if not remaining:
        return []
    safe_limit = min(max(limit, 200), 1900)
    chunks: List[str] = []
    while len(remaining) > safe_limit:
        newline_break = remaining.rfind("\n", 0, safe_limit + 1)
        space_break = remaining.rfind(" ", 0, safe_limit + 1)
        split_at = max(newline_break, space_break)
        if split_at < safe_limit // 2:
            split_at = safe_limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def chip_chat_retry_after(
    guild_id: int,
    user_id: int,
    now: Optional[float] = None,
) -> float:
    """Register one member request and return remaining cooldown seconds."""

    current = time.monotonic() if now is None else now
    key = (guild_id, user_id)
    available_at = chip_chat_cooldowns.get(key, 0.0)
    if available_at > current:
        return available_at - current
    chip_chat_cooldowns[key] = current + CHIP_CHAT_COOLDOWN_SECONDS
    if len(chip_chat_cooldowns) > 5000:
        expired = [item for item, expiry in chip_chat_cooldowns.items() if expiry <= current]
        for item in expired:
            chip_chat_cooldowns.pop(item, None)
    return 0.0


async def generate_chip_chat_reply(
    prompt: str,
    guild_id: int,
    user_id: int,
    source_message_id: int,
) -> str:
    """Generate one contextual Luna reply inside CHiP's shared AI cap."""

    clean_prompt = prompt.strip()
    if not chip_chat_client.configured:
        raise ChipChatUnavailable(
            "CHiP chat is not configured yet. Please let a server administrator know."
        )
    if await get_setting("chip_chat_paused", "0") == "1":
        raise ChipChatUnavailable("CHiP chat is currently paused by an administrator.")

    history = await chat_history_store.history(guild_id, user_id)
    async with ai_budget_lock:
        status = await get_terra_status()
        if status.budget_usd <= 0 or status.remaining_usd <= 0:
            await check_ai_budget_alerts(status, force_cutoff=True)
            raise ChipChatUnavailable(
                "CHiP chat has reached its shared AI spending limit and is "
                "temporarily unavailable."
            )

        request_ceiling = chip_chat_client.estimate_request_ceiling(
            clean_prompt,
            history,
        )
        if status.total_cost_usd + request_ceiling > status.budget_usd:
            await check_ai_budget_alerts(status, force_cutoff=True)
            raise ChipChatUnavailable(
                "CHiP chat has reached its shared AI spending limit and is "
                "temporarily unavailable."
            )

        try:
            reply, usage = await chip_chat_client.ask(clean_prompt, history)
        except ChatError as exc:
            log.exception("CHiP member chat request failed.")
            raise ChipChatUnavailable(
                "CHiP had trouble answering just now. Please try again in a moment."
            ) from exc

        await record_ai_usage(usage, source_message_id, "chip_chat")
        try:
            await chat_history_store.append_exchange(
                guild_id,
                user_id,
                clean_prompt,
                reply,
            )
        except (OSError, sqlite3.Error):
            log.exception("Could not save CHiP member-chat history.")
        updated_status = await get_terra_status()
        await check_ai_budget_alerts(updated_status)
        return reply


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

    # Human Spanish still uses Terra for slang and jokes. CHiP's own Spanish
    # is already clean model output, so Azure can translate it directly and
    # avoid charging the shared AI budget a second time for the same reply.
    if source_language == "es" and request_kind != "chip_chat_translation":
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


def translation_group_for_channel(
    channel_id: int,
) -> Optional[Tuple[str, Dict[int, str]]]:
    """Return the translation group containing a Discord channel, if any."""

    for group_name, channels in TRANSLATION_GROUPS.items():
        if channel_id in channels:
            return group_name, channels
    return None


async def relay_text_across_translation_group(
    *,
    guild: discord.Guild,
    group_name: str,
    group_channels: Dict[int, str],
    source_channel_id: int,
    source_message_id: int,
    original_text: str,
    media: RelayMedia,
    username: str,
    avatar_url: str,
    author_id: Optional[int],
    request_kind: str,
    include_source: bool = False,
) -> None:
    """Mirror one human or CHiP message to every other language channel."""

    source_language = group_channels[source_channel_id]
    target_languages = [
        language
        for channel_id, language in group_channels.items()
        if channel_id != source_channel_id
    ]
    translations = (
        await translate_for_targets(
            original_text,
            source_language,
            target_languages,
            source_message_id,
            request_kind,
        )
        if original_text
        else {}
    )
    link_group = uuid.uuid4().hex

    if not include_source:
        await store_message_link(
            source_message_id,
            link_group,
            source_channel_id,
            source_language,
            True,
            author_id,
        )

    for target_channel_id, target_language in group_channels.items():
        if target_channel_id == source_channel_id and not include_source:
            continue

        target_channel = guild.get_channel(target_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            log.error("Cannot find target text channel ID %s", target_channel_id)
            continue

        try:
            webhook = await get_or_create_webhook(target_channel)
            translated_message = await send_webhook_relay(
                webhook=webhook,
                translated_text=(
                    original_text
                    if target_channel_id == source_channel_id
                    else translations.get(target_language, "")
                ),
                media=media,
                username=username,
                avatar_url=avatar_url,
            )
            await store_message_link(
                translated_message.id,
                link_group,
                target_channel_id,
                target_language,
                target_channel_id == source_channel_id,
                author_id,
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


MAINTENANCE_START_NOTICES = {
    "en": (
        "🛠️ **CHiP Is Under Maintenance**\n"
        "CHiP is currently undergoing maintenance for updates or bug fixes. "
        "Translation and other CHiP services may be temporarily unavailable. "
        "We’ll notify everyone when CHiP is back online."
    ),
    "es": (
        "🛠️ **CHiP está en mantenimiento**\n"
        "CHiP está en mantenimiento por actualizaciones o correcciones de errores. "
        "Las traducciones y otros servicios de CHiP podrían no estar disponibles "
        "temporalmente. Avisaremos cuando CHiP vuelva a estar en línea."
    ),
    "pt": (
        "🛠️ **CHiP está em manutenção**\n"
        "CHiP está em manutenção para atualizações ou correções de erros. As "
        "traduções e outros serviços do CHiP podem ficar temporariamente "
        "indisponíveis. Avisaremos quando o CHiP estiver online novamente."
    ),
    "fr": (
        "🛠️ **CHiP est en maintenance**\n"
        "CHiP est actuellement en maintenance pour des mises à jour ou des "
        "corrections de bugs. Les traductions et les autres services de CHiP "
        "peuvent être temporairement indisponibles. Nous vous informerons lorsque "
        "CHiP sera de nouveau en ligne."
    ),
    "de": (
        "🛠️ **CHiP wird gewartet**\n"
        "CHiP wird derzeit für Updates oder Fehlerbehebungen gewartet. "
        "Übersetzungen und andere CHiP-Dienste sind möglicherweise vorübergehend "
        "nicht verfügbar. Wir informieren euch, sobald CHiP wieder online ist."
    ),
    "tr": (
        "🛠️ **CHiP Bakımda**\n"
        "CHiP şu anda güncellemeler veya hata düzeltmeleri için bakımdadır. Çeviri "
        "ve diğer CHiP hizmetleri geçici olarak kullanılamayabilir. CHiP tekrar "
        "çevrimiçi olduğunda herkese haber vereceğiz."
    ),
    "ar": (
        "🛠️ **CHiP قيد الصيانة**\n"
        "يخضع CHiP حاليًا للصيانة لإجراء تحديثات أو إصلاح أخطاء. قد لا تتوفر "
        "الترجمة وخدمات CHiP الأخرى مؤقتًا. سنخبر الجميع عندما يعود CHiP للعمل."
    ),
    "zh-CN": (
        "🛠️ **CHiP 正在维护**\n"
        "CHiP 目前正在进行更新或错误修复。翻译和其他 CHiP 服务可能暂时不可用。"
        "CHiP 恢复上线后，我们会通知大家。"
    ),
}

MAINTENANCE_END_NOTICES = {
    "en": (
        "✅ **CHiP Is Back Online**\n"
        "Maintenance is complete. Translation and other CHiP services are "
        "available again."
    ),
    "es": (
        "✅ **CHiP volvió a estar en línea**\n"
        "El mantenimiento terminó. Las traducciones y los demás servicios de "
        "CHiP están disponibles nuevamente."
    ),
    "pt": (
        "✅ **O CHiP está online novamente**\n"
        "A manutenção foi concluída. As traduções e os outros serviços do CHiP "
        "estão disponíveis novamente."
    ),
    "fr": (
        "✅ **CHiP est de nouveau en ligne**\n"
        "La maintenance est terminée. Les traductions et les autres services de "
        "CHiP sont de nouveau disponibles."
    ),
    "de": (
        "✅ **CHiP ist wieder online**\n"
        "Die Wartung ist abgeschlossen. Übersetzungen und andere CHiP-Dienste "
        "sind wieder verfügbar."
    ),
    "tr": (
        "✅ **CHiP Tekrar Çevrimiçi**\n"
        "Bakım tamamlandı. Çeviri ve diğer CHiP hizmetleri tekrar kullanılabilir."
    ),
    "ar": (
        "✅ **عاد CHiP للعمل**\n"
        "اكتملت الصيانة. الترجمة وخدمات CHiP الأخرى متاحة مجددًا."
    ),
    "zh-CN": (
        "✅ **CHiP 已恢复上线**\n"
        "维护已完成。翻译和其他 CHiP 服务现已恢复。"
    ),
}

CONTEST_NOTICE_TEMPLATES = {
    "en": {
        "open": (
            "🍪 **{title} is open for voting!**\n"
            "View every cookie profile picture and choose your one favorite.\n"
            "[View the contest and vote]({url})\n"
            "Voting closes <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **Voting reminder: {title}**\n"
            "If you haven’t voted yet, view the entries and choose your favorite.\n"
            "[View the contest and vote]({url})\n"
            "Voting closes <t:{closes}:R>."
        ),
        "final": (
            "⏰ **Final 3 hours to vote: {title}!**\n"
            "This is your last chance to choose your favorite cookie profile picture.\n"
            "[View the contest and vote now]({url})\n"
            "Voting closes <t:{closes}:R>."
        ),
    },
    "es": {
        "open": (
            "🍪 **¡La votación de {title} ya está abierta!**\n"
            "Mira todas las fotos de perfil y elige tu favorita.\n"
            "[Ver el concurso y votar]({url})\n"
            "La votación termina <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **Recordatorio de votación: {title}**\n"
            "Si aún no has votado, mira las opciones y elige tu favorita.\n"
            "[Ver el concurso y votar]({url})\n"
            "La votación termina <t:{closes}:R>."
        ),
        "final": (
            "⏰ **¡Últimas 3 horas para votar en {title}!**\n"
            "Esta es tu última oportunidad para elegir tu foto de perfil favorita.\n"
            "[Ver el concurso y votar ahora]({url})\n"
            "La votación termina <t:{closes}:R>."
        ),
    },
    "pt": {
        "open": (
            "🍪 **A votação de {title} está aberta!**\n"
            "Veja todas as fotos de perfil e escolha a sua favorita.\n"
            "[Ver o concurso e votar]({url})\n"
            "A votação termina <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **Lembrete de votação: {title}**\n"
            "Se você ainda não votou, veja as opções e escolha a sua favorita.\n"
            "[Ver o concurso e votar]({url})\n"
            "A votação termina <t:{closes}:R>."
        ),
        "final": (
            "⏰ **Últimas 3 horas para votar em {title}!**\n"
            "Esta é sua última chance de escolher sua foto de perfil favorita.\n"
            "[Ver o concurso e votar agora]({url})\n"
            "A votação termina <t:{closes}:R>."
        ),
    },
    "fr": {
        "open": (
            "🍪 **Le vote pour {title} est ouvert !**\n"
            "Regardez toutes les photos de profil et choisissez votre préférée.\n"
            "[Voir le concours et voter]({url})\n"
            "Le vote se termine <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **Rappel de vote : {title}**\n"
            "Si vous n’avez pas encore voté, regardez les choix et sélectionnez votre préféré.\n"
            "[Voir le concours et voter]({url})\n"
            "Le vote se termine <t:{closes}:R>."
        ),
        "final": (
            "⏰ **Plus que 3 heures pour voter : {title} !**\n"
            "C’est votre dernière chance de choisir votre photo de profil préférée.\n"
            "[Voir le concours et voter maintenant]({url})\n"
            "Le vote se termine <t:{closes}:R>."
        ),
    },
    "de": {
        "open": (
            "🍪 **Die Abstimmung für {title} ist eröffnet!**\n"
            "Seht euch alle Profilbilder an und wählt euren einen Favoriten.\n"
            "[Wettbewerb ansehen und abstimmen]({url})\n"
            "Die Abstimmung endet <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **Abstimmungserinnerung: {title}**\n"
            "Falls ihr noch nicht abgestimmt habt, seht euch die Beiträge an und wählt euren Favoriten.\n"
            "[Wettbewerb ansehen und abstimmen]({url})\n"
            "Die Abstimmung endet <t:{closes}:R>."
        ),
        "final": (
            "⏰ **Noch 3 Stunden für die Abstimmung: {title}!**\n"
            "Das ist eure letzte Chance, euer Lieblingsprofilbild zu wählen.\n"
            "[Jetzt ansehen und abstimmen]({url})\n"
            "Die Abstimmung endet <t:{closes}:R>."
        ),
    },
    "tr": {
        "open": (
            "🍪 **{title} oylaması başladı!**\n"
            "Tüm profil resimlerine bakın ve tek favorinizi seçin.\n"
            "[Yarışmayı görüntüle ve oy ver]({url})\n"
            "Oylama <t:{closes}:R> sona erecek."
        ),
        "daily": (
            "🍪 **Oylama hatırlatması: {title}**\n"
            "Henüz oy vermediyseniz seçeneklere bakın ve favorinizi seçin.\n"
            "[Yarışmayı görüntüle ve oy ver]({url})\n"
            "Oylama <t:{closes}:R> sona erecek."
        ),
        "final": (
            "⏰ **{title} için son 3 saat!**\n"
            "Favori profil resminizi seçmek için son şansınız.\n"
            "[Şimdi görüntüle ve oy ver]({url})\n"
            "Oylama <t:{closes}:R> sona erecek."
        ),
    },
    "ar": {
        "open": (
            "🍪 **بدأ التصويت في {title}!**\n"
            "شاهدوا جميع صور الملف الشخصي واختاروا صورة واحدة مفضلة.\n"
            "[عرض المسابقة والتصويت]({url})\n"
            "ينتهي التصويت <t:{closes}:R>."
        ),
        "daily": (
            "🍪 **تذكير بالتصويت: {title}**\n"
            "إذا لم تصوّت بعد، شاهد المشاركات واختر صورتك المفضلة.\n"
            "[عرض المسابقة والتصويت]({url})\n"
            "ينتهي التصويت <t:{closes}:R>."
        ),
        "final": (
            "⏰ **آخر 3 ساعات للتصويت في {title}!**\n"
            "هذه فرصتك الأخيرة لاختيار صورة الملف الشخصي المفضلة لديك.\n"
            "[عرض المسابقة والتصويت الآن]({url})\n"
            "ينتهي التصويت <t:{closes}:R>."
        ),
    },
    "zh-CN": {
        "open": (
            "🍪 **{title} 投票现已开始！**\n"
            "查看所有头像，并选出你唯一的最爱。\n"
            "[查看比赛并投票]({url})\n"
            "投票将于 <t:{closes}:R> 结束。"
        ),
        "daily": (
            "🍪 **投票提醒：{title}**\n"
            "如果你还没有投票，请查看所有作品并选出你最喜欢的一个。\n"
            "[查看比赛并投票]({url})\n"
            "投票将于 <t:{closes}:R> 结束。"
        ),
        "final": (
            "⏰ **{title} 投票只剩最后 3 小时！**\n"
            "这是选择你最喜欢头像的最后机会。\n"
            "[立即查看并投票]({url})\n"
            "投票将于 <t:{closes}:R> 结束。"
        ),
    },
}


def contest_jump_url(contest: ContestRecord) -> Optional[str]:
    if not contest.channel_id or not contest.intro_message_id:
        return None
    return (
        f"https://discord.com/channels/{contest.guild_id}/"
        f"{contest.channel_id}/{contest.intro_message_id}"
    )


def contest_notice_text(
    contest: ContestRecord,
    language: str,
    notice_kind: str,
) -> Optional[str]:
    url = contest_jump_url(contest)
    if url is None or contest.closes_at is None:
        return None
    closes = int(datetime.fromisoformat(contest.closes_at).timestamp())
    templates = CONTEST_NOTICE_TEMPLATES.get(
        language,
        CONTEST_NOTICE_TEMPLATES["en"],
    )
    template = templates.get(notice_kind, templates["daily"])
    return template.format(
        title=discord.utils.escape_markdown(contest.title),
        url=url,
        closes=closes,
    )


async def broadcast_contest_notice(
    contest: ContestRecord,
    notice_kind: str,
    *,
    exclude_channel_id: Optional[int] = None,
) -> int:
    sent_count = 0
    for channel_id, language in GENERAL_CHANNELS.items():
        if channel_id == exclude_channel_id:
            continue
        channel = await get_text_channel(channel_id)
        if channel is None:
            continue
        notice = contest_notice_text(contest, language, notice_kind)
        if notice is None:
            continue
        try:
            await channel.send(
                notice,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent_count += 1
        except (discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not send contest %s notice to channel %s.",
                notice_kind,
                channel_id,
            )
    return sent_count


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


class ContestManagerRequired(app_commands.CheckFailure):
    """Raised when someone outside the contest team uses a manager command."""


class ContestOwnerRequired(app_commands.CheckFailure):
    """Raised when someone other than the server owner edits contest managers."""


def is_contest_owner(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return interaction.user.id in {
        interaction.guild.owner_id,
        OWNER_USER_ID,
    } - {0}


async def contest_manager_predicate(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        raise ContestManagerRequired()
    if is_contest_owner(interaction):
        return True
    if await contest_store.is_manager(interaction.guild.id, interaction.user.id):
        return True
    raise ContestManagerRequired()


async def contest_owner_predicate(interaction: discord.Interaction) -> bool:
    if is_contest_owner(interaction):
        return True
    raise ContestOwnerRequired()


def contest_manager_only():
    return app_commands.check(contest_manager_predicate)


def contest_owner_only():
    return app_commands.check(contest_owner_predicate)


def safe_contest_filename(filename: str, entry_id: int) -> str:
    original = Path(filename or "entry.png")
    extension = original.suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        extension = ".png"
    safe_stem = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in original.stem
    ).strip("_")
    safe_stem = safe_stem[:40] or "cookie_pfp"
    return f"entry_{entry_id}_{safe_stem}{extension}"


def contest_image_is_supported(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    extension = Path(attachment.filename).suffix.lower()
    return content_type in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    } or extension in {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def contest_file(entry: ContestEntryRecord) -> discord.File:
    return discord.File(
        io.BytesIO(entry.image_data),
        filename=safe_contest_filename(entry.filename, entry.entry_id),
    )


def contest_collage_file(
    contest: ContestRecord,
    entries: Sequence[ContestEntryRecord],
) -> discord.File:
    first_number = entries[0].entry_number
    last_number = entries[-1].entry_number
    collage_data = build_contest_collage(entries)
    return discord.File(
        io.BytesIO(collage_data),
        filename=(
            f"contest_{contest.contest_id}_entries_"
            f"{first_number}_{last_number}.jpg"
        ),
    )


def contest_gallery_embed(
    contest: ContestRecord,
    entries: Sequence[ContestEntryRecord],
    *,
    image_url: str,
    closes_at: Optional[datetime] = None,
    tallies: Optional[Sequence[ContestTally]] = None,
) -> discord.Embed:
    first_number = entries[0].entry_number
    last_number = entries[-1].entry_number
    title = f"🍪 {contest.title} — Entries #{first_number}–#{last_number}"

    if tallies is not None:
        lines = []
        for tally in sorted(tallies, key=lambda item: item.entry.entry_number):
            name = discord.utils.escape_markdown(tally.entry.contestant_name)
            vote_word = "vote" if tally.votes == 1 else "votes"
            lines.append(
                f"**#{tally.entry.entry_number} — {name}:** "
                f"{tally.votes} {vote_word}"
            )
        description = "**Voting is closed.**\n\n" + "\n".join(lines)
        color = discord.Color.gold()
    else:
        if closes_at is None:
            description = (
                "This is a private preview. The published contest will show "
                "numbered vote buttons below this collage."
            )
        else:
            closes_timestamp = int(closes_at.timestamp())
            anonymity_note = (
                "Names and vote totals stay hidden until voting closes."
                if contest.anonymous
                else "Contestant names are visible in the contest details."
            )
            description = (
                "Tap the collage to enlarge it, then tap the matching numbered "
                "button below to vote. You may change your vote before closing.\n\n"
                f"**Voting closes:** <t:{closes_timestamp}:F> "
                f"(<t:{closes_timestamp}:R>)\n"
                f"{anonymity_note}"
            )
        color = discord.Color.blurple()

    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_image(url=image_url)
    embed.set_footer(text="CMON Contest • Powered by CHiP")
    return embed


def contest_entry_embed(
    contest: ContestRecord,
    entry: ContestEntryRecord,
    *,
    image_url: str,
    reveal: bool,
    votes: Optional[int] = None,
    winner: bool = False,
) -> discord.Embed:
    escaped_name = discord.utils.escape_markdown(entry.contestant_name)
    show_name = reveal or not contest.anonymous
    title = f"🍪 Entry #{entry.entry_number}"
    if show_name:
        title += f" — {escaped_name}"

    if reveal:
        vote_word = "vote" if votes == 1 else "votes"
        description = f"**Contestant:** {escaped_name}\n**Votes:** {votes or 0} {vote_word}"
    else:
        description = "Click **Vote** below to choose this cookie profile picture."

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold() if winner else discord.Color.blurple(),
    )
    embed.set_image(url=image_url)
    if not reveal:
        embed.set_footer(text="Vote totals stay hidden until the contest closes.")
    return embed


def contest_intro_embed(contest: ContestRecord, closes_at: datetime) -> discord.Embed:
    closes_timestamp = int(closes_at.timestamp())
    anonymity_note = (
        "Contestant names will be revealed after voting closes."
        if contest.anonymous
        else "Contestant names are displayed with their entries."
    )
    embed = discord.Embed(
        title=f"🍪 {contest.title}",
        description=(
            "Choose your favorite cookie-themed profile picture!\n\n"
            "• Click the **Vote** button under one entry.\n"
            "• You receive one vote and may change it before closing.\n"
            "• Vote totals remain hidden during the contest.\n"
            f"• {anonymity_note}\n\n"
            f"**Voting closes:** <t:{closes_timestamp}:F> "
            f"(<t:{closes_timestamp}:R>)"
        ),
        color=discord.Color.from_rgb(181, 101, 29),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="CMON Contest • Powered by CHiP")
    return embed


async def record_contest_vote(
    interaction: discord.Interaction,
    entry_id: int,
) -> None:
    if interaction.guild is None or interaction.user.bot:
        await interaction.response.send_message(
            "Only human members inside the CMON server can vote.",
            ephemeral=True,
        )
        return

    try:
        outcome = await contest_store.record_vote(
            entry_id,
            interaction.user.id,
        )
    except ContestError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    if outcome.already_selected:
        message = f"🍪 Entry **#{outcome.entry_number}** is already your vote."
    elif outcome.previous_entry_number is not None:
        message = (
            f"✅ Your vote changed from **#{outcome.previous_entry_number}** "
            f"to **#{outcome.entry_number}**."
        )
    else:
        message = f"✅ Your vote for **Entry #{outcome.entry_number}** is recorded!"
    await interaction.response.send_message(message, ephemeral=True)


class ContestVoteButton(discord.ui.Button):
    def __init__(
        self,
        entry_id: int,
        entry_number: int,
        *,
        row: int = 0,
        disabled: bool = False,
        compact: bool = True,
    ):
        self.entry_id = entry_id
        label = f"#{entry_number}" if compact else "Vote"
        super().__init__(
            label=label,
            emoji=None if compact else "🍪",
            style=discord.ButtonStyle.primary,
            custom_id=f"chip_contest_vote:{entry_id}",
            row=row,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await record_contest_vote(interaction, self.entry_id)


class ContestVoteView(discord.ui.View):
    """Legacy single-entry view retained for already-published contests."""

    def __init__(self, entry_id: int, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.entry_id = entry_id
        self.add_item(
            ContestVoteButton(
                entry_id,
                entry_number=entry_id,
                disabled=disabled,
                compact=False,
            )
        )


class ContestGalleryVoteView(discord.ui.View):
    def __init__(
        self,
        entries: Sequence[ContestEntryRecord],
        *,
        disabled: bool = False,
    ):
        if not entries or len(entries) > CONTEST_ENTRIES_PER_POST:
            raise ValueError(
                f"A contest voting post supports 1–{CONTEST_ENTRIES_PER_POST} entries."
            )
        super().__init__(timeout=None)
        for index, entry in enumerate(entries):
            self.add_item(
                ContestVoteButton(
                    entry.entry_id,
                    entry.entry_number,
                    row=index // 5,
                    disabled=disabled,
                )
            )


async def restore_contest_vote_views() -> int:
    entries_by_message: Dict[int, List[ContestEntryRecord]] = {}
    for entry_id, message_id in await contest_store.open_entry_message_ids():
        entry = await contest_store.get_entry(entry_id)
        if entry is not None:
            entries_by_message.setdefault(message_id, []).append(entry)

    for message_id, entries in entries_by_message.items():
        entries.sort(key=lambda item: item.entry_number)
        bot.add_view(ContestGalleryVoteView(entries), message_id=message_id)
    return len(entries_by_message)


def contest_leaderboard_lines(tallies: Sequence[ContestTally]) -> List[str]:
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for index, tally in enumerate(tallies[:3]):
        vote_word = "vote" if tally.votes == 1 else "votes"
        name = discord.utils.escape_markdown(tally.entry.contestant_name)
        lines.append(
            f"{medals[index]} **Entry #{tally.entry.entry_number} — {name}** "
            f"({tally.votes} {vote_word})"
        )
    return lines


async def reveal_and_disable_contest_entries(
    contest: ContestRecord,
    tallies: Sequence[ContestTally],
) -> None:
    if not contest.channel_id:
        return
    channel = await get_text_channel(contest.channel_id)
    if channel is None:
        return
    tallies_by_message: Dict[int, List[ContestTally]] = {}
    for tally in tallies:
        if tally.entry.message_id:
            tallies_by_message.setdefault(tally.entry.message_id, []).append(tally)

    for message_id, message_tallies in tallies_by_message.items():
        try:
            message_tallies.sort(key=lambda item: item.entry.entry_number)
            message = await channel.fetch_message(message_id)
            image_url = (
                message.attachments[0].url
                if message.attachments
                else "https://cdn.discordapp.com/embed/avatars/0.png"
            )
            message_entries = [tally.entry for tally in message_tallies]
            await message.edit(
                embed=contest_gallery_embed(
                    contest,
                    message_entries,
                    image_url=image_url,
                    tallies=message_tallies,
                ),
                view=ContestGalleryVoteView(message_entries, disabled=True),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.exception(
                "Could not close contest entry message %s.",
                message_id,
            )


async def announce_contest_results(
    contest: ContestRecord,
    tallies: Sequence[ContestTally],
) -> bool:
    if not contest.channel_id:
        return False
    channel = await get_text_channel(contest.channel_id)
    if channel is None:
        return False

    total_votes = sum(tally.votes for tally in tallies)
    if not tallies or total_votes == 0:
        embed = discord.Embed(
            title=f"🍪 {contest.title} — Voting Closed",
            description="The contest closed without any votes.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await contest_store.mark_result_message(contest.contest_id, message.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not announce the contest results.")
            return False

    highest_votes = tallies[0].votes
    winners = [tally for tally in tallies if tally.votes == highest_votes]
    leaderboard = "\n".join(contest_leaderboard_lines(tallies))

    if len(winners) == 1:
        winner = winners[0]
        name = discord.utils.escape_markdown(winner.entry.contestant_name)
        embed = discord.Embed(
            title=f"🏆 {contest.title} Winner!",
            description=(
                f"Congratulations to **{name}** with **Entry "
                f"#{winner.entry.entry_number}**!\n\n{leaderboard}\n\n"
                f"**Total voters:** {total_votes}"
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        file = contest_file(winner.entry)
        embed.set_image(url=f"attachment://{file.filename}")
        try:
            message = await channel.send(
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await contest_store.mark_result_message(contest.contest_id, message.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Could not announce the contest winner.")
            return False

    tied_names = "\n".join(
        f"• **Entry #{tally.entry.entry_number} — "
        f"{discord.utils.escape_markdown(tally.entry.contestant_name)}**"
        for tally in winners
    )
    embed = discord.Embed(
        title=f"🏆 {contest.title} — We Have a Tie!",
        description=(
            f"The following entries tied with **{highest_votes} votes** each:\n"
            f"{tied_names}\n\n{leaderboard}\n\n**Total voters:** {total_votes}"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    try:
        message = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await contest_store.mark_result_message(contest.contest_id, message.id)
        for winner in winners[:10]:
            file = contest_file(winner.entry)
            winner_embed = discord.Embed(
                title=(
                    f"Tied Winner — Entry #{winner.entry.entry_number}: "
                    f"{discord.utils.escape_markdown(winner.entry.contestant_name)}"
                ),
                color=discord.Color.gold(),
            )
            winner_embed.set_image(url=f"attachment://{file.filename}")
            await channel.send(
                embed=winner_embed,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.exception("Could not announce the tied contest winners.")
        return False


async def close_contest_and_announce(contest_id: int) -> bool:
    async with contest_action_lock:
        contest, tallies = await contest_store.close_contest(contest_id)
        await reveal_and_disable_contest_entries(contest, tallies)
        return await announce_contest_results(contest, tallies)


@tasks.loop(seconds=30)
async def contest_auto_close_loop() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    expired = await contest_store.expired_open_contests(now_iso)
    for contest in expired:
        try:
            await close_contest_and_announce(contest.contest_id)
        except ContestStateError:
            continue
        except Exception:
            log.exception("Automatic close failed for contest %s.", contest.contest_id)

    for contest, reminder_kind in await contest_store.claim_due_reminders(now_iso):
        try:
            await broadcast_contest_notice(contest, reminder_kind)
        except Exception:
            log.exception(
                "Automatic %s reminder failed for contest %s.",
                reminder_kind,
                contest.contest_id,
            )


contest_group = app_commands.Group(
    name="contest",
    description="Create and manage CHiP image contests.",
)


@contest_group.command(name="create", description="Create a new private contest draft.")
@app_commands.describe(
    title="Contest title members will see.",
    duration_hours="Hours voting stays open after publishing (1-768).",
    anonymous="Hide contestant names until voting closes.",
)
@contest_manager_only()
async def contest_create_command(
    interaction: discord.Interaction,
    title: str,
    duration_hours: int = 72,
    anonymous: bool = True,
) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    clean_title = " ".join(title.split())[:100]
    if len(clean_title) < 3:
        await interaction.response.send_message(
            "The contest title must contain at least three characters.",
            ephemeral=True,
        )
        return
    if duration_hours < 1 or duration_hours > 768:
        await interaction.response.send_message(
            "Choose a voting duration between **1 and 768 hours**.",
            ephemeral=True,
        )
        return
    try:
        contest = await contest_store.create_contest(
            interaction.guild.id,
            clean_title,
            anonymous,
            duration_hours,
            interaction.user.id,
        )
    except ContestError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return

    privacy = "Anonymous until closing" if contest.anonymous else "Names visible"
    await interaction.response.send_message(
        f"🍪 Draft created: **{contest.title}**\n"
        f"Voting duration: **{contest.duration_hours} hours**\n"
        f"Display: **{privacy}**\n\n"
        "Next, use `/contest add` for every approved picture.",
        ephemeral=True,
    )


@contest_group.command(name="add", description="Upload an approved image to the draft.")
@app_commands.describe(
    contestant_name="Name to reveal with this entry when the contest closes.",
    image="PNG, JPG, GIF, or WEBP contest image.",
)
@contest_manager_only()
async def contest_add_command(
    interaction: discord.Interaction,
    contestant_name: str,
    image: discord.Attachment,
) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    clean_name = " ".join(contestant_name.split())[:80]
    if not clean_name:
        await interaction.response.send_message(
            "Enter the contestant's name.",
            ephemeral=True,
        )
        return
    if not contest_image_is_supported(image):
        await interaction.response.send_message(
            "Please upload a **PNG, JPG, GIF, or WEBP** image.",
            ephemeral=True,
        )
        return
    if image.size > MAX_CONTEST_IMAGE_BYTES:
        limit_mb = MAX_CONTEST_IMAGE_BYTES // (1024 * 1024)
        await interaction.response.send_message(
            f"That image is too large. The contest limit is **{limit_mb} MB**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        image_data = await image.read(use_cached=True)
        if not image_data:
            raise ContestStateError("Discord returned an empty image file.")
        entry = await contest_store.add_entry(
            interaction.guild.id,
            clean_name,
            image.filename,
            image.content_type or "application/octet-stream",
            image_data,
            interaction.user.id,
        )
    except (ContestError, discord.HTTPException) as error:
        await interaction.followup.send(
            f"CHiP could not add that entry: {error}",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Added **{clean_name}** as **Entry #{entry.entry_number}**.\n"
        "Keep using `/contest add`, then run `/contest preview`.",
        ephemeral=True,
    )


@contest_group.command(name="remove", description="Remove and renumber a draft entry.")
@app_commands.describe(entry_number="Entry number shown in the private preview.")
@contest_manager_only()
async def contest_remove_command(
    interaction: discord.Interaction,
    entry_number: int,
) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    try:
        removed = await contest_store.remove_entry(
            interaction.guild.id,
            entry_number,
        )
    except ContestError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(
        f"Removed **Entry #{entry_number} — {removed.contestant_name}**. "
        "The remaining entries were renumbered.",
        ephemeral=True,
    )


@contest_group.command(name="preview", description="Privately preview the current draft.")
@contest_manager_only()
async def contest_preview_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    contest = await contest_store.get_active_contest(interaction.guild.id)
    if contest is None or contest.status != "draft":
        await interaction.response.send_message(
            "There is no draft contest to preview.",
            ephemeral=True,
        )
        return
    entries = await contest_store.list_entries(
        contest.contest_id,
        include_image_data=True,
    )
    await interaction.response.send_message(
        f"🍪 **Private preview: {contest.title}**\n"
        f"Entries: **{len(entries)}** • Duration: **{contest.duration_hours} hours**\n"
        "Only you can see these preview messages.",
        ephemeral=True,
    )
    if not entries:
        await interaction.followup.send(
            "No images have been added yet.",
            ephemeral=True,
        )
        return
    try:
        for batch in contest_entry_batches(entries):
            file = await asyncio.to_thread(contest_collage_file, contest, batch)
            embed = contest_gallery_embed(
                contest,
                batch,
                image_url=f"attachment://{file.filename}",
            )
            await interaction.followup.send(
                embed=embed,
                file=file,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except (ContestError, discord.HTTPException) as error:
        await interaction.followup.send(
            f"CHiP could not build the preview: {error}",
            ephemeral=True,
        )


@contest_group.command(name="publish", description="Publish the draft and open voting.")
@app_commands.describe(channel="Channel where CHiP should post the contest.")
@contest_manager_only()
async def contest_publish_command(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    target_channel = channel
    if target_channel is None and isinstance(interaction.channel, discord.TextChannel):
        target_channel = interaction.channel
    if target_channel is None:
        await interaction.response.send_message(
            "Choose a standard text channel for the contest.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None:
        await interaction.response.send_message(
            "CHiP could not verify its channel permissions.",
            ephemeral=True,
        )
        return
    permissions = target_channel.permissions_for(bot_member)
    missing_permissions = [
        label
        for allowed, label in (
            (permissions.view_channel, "View Channel"),
            (permissions.send_messages, "Send Messages"),
            (permissions.embed_links, "Embed Links"),
            (permissions.attach_files, "Attach Files"),
            (permissions.read_message_history, "Read Message History"),
        )
        if not allowed
    ]
    if missing_permissions:
        await interaction.response.send_message(
            "CHiP is missing these permissions in that channel: **"
            + ", ".join(missing_permissions)
            + "**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    created_messages: List[discord.Message] = []
    published_contest: Optional[ContestRecord] = None
    async with contest_action_lock:
        contest = await contest_store.get_active_contest(interaction.guild.id)
        if contest is None or contest.status != "draft":
            await interaction.followup.send(
                "There is no draft contest to publish.",
                ephemeral=True,
            )
            return
        entries = await contest_store.list_entries(
            contest.contest_id,
            include_image_data=True,
        )
        if len(entries) < 2:
            await interaction.followup.send(
                "Add at least **two entries** before publishing.",
                ephemeral=True,
            )
            return

        closes_at = datetime.now(timezone.utc) + timedelta(
            hours=contest.duration_hours
        )
        entry_message_ids: Dict[int, int] = {}
        try:
            intro_message: Optional[discord.Message] = None
            for batch in contest_entry_batches(entries):
                file = await asyncio.to_thread(contest_collage_file, contest, batch)
                view = ContestGalleryVoteView(batch)
                gallery_message = await target_channel.send(
                    embed=contest_gallery_embed(
                        contest,
                        batch,
                        image_url=f"attachment://{file.filename}",
                        closes_at=closes_at,
                    ),
                    file=file,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if intro_message is None:
                    intro_message = gallery_message
                created_messages.append(gallery_message)
                for entry in batch:
                    entry_message_ids[entry.entry_id] = gallery_message.id

            if intro_message is None:
                raise ContestStateError("The contest has no entries to publish.")

            published_contest = await contest_store.publish_contest(
                contest.contest_id,
                target_channel.id,
                intro_message.id,
                closes_at.isoformat(),
                entry_message_ids,
                datetime.now(timezone.utc).isoformat(),
            )
        except (ContestError, discord.Forbidden, discord.HTTPException) as error:
            for message in reversed(created_messages):
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            log.exception("Contest publication failed.")
            await interaction.followup.send(
                f"CHiP could not publish the contest: {error}",
                ephemeral=True,
            )
            return

    if published_contest is not None:
        await broadcast_contest_notice(
            published_contest,
            "open",
            exclude_channel_id=target_channel.id,
        )

    await interaction.followup.send(
        f"✅ **{contest.title}** is live in {target_channel.mention}!\n"
        f"Voting closes <t:{int(closes_at.timestamp())}:R>.\n"
        f"CHiP will remind every general-language chat every **{CONTEST_REMINDER_INTERVAL_HOURS} hours**, "
        f"plus once more during the **final {CONTEST_FINAL_REMINDER_HOURS} hours**.",
        ephemeral=True,
    )


@contest_group.command(
    name="reminder",
    description="Send an extra voting reminder to every general-language chat.",
)
@contest_manager_only()
async def contest_reminder_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()

    contest = await contest_store.get_active_contest(interaction.guild.id)
    if contest is None or contest.status != "open":
        await interaction.response.send_message(
            "There is no open contest to remind members about.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    sent_count = await broadcast_contest_notice(contest, "daily")
    if sent_count == 0:
        await interaction.followup.send(
            "CHiP could not send the reminder to any configured General language channel.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"🍪 Extra contest reminder sent to **{sent_count}** General language "
        "channel(s). The automatic 24-hour timer was not changed.",
        ephemeral=True,
    )


@contest_group.command(name="status", description="Privately check the active contest.")
@contest_manager_only()
async def contest_status_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    contest = await contest_store.get_active_contest(interaction.guild.id)
    if contest is None:
        await interaction.response.send_message(
            "There is no active draft or open contest.",
            ephemeral=True,
        )
        return
    entries = await contest_store.list_entries(contest.contest_id)
    votes = await contest_store.vote_count(contest.contest_id)
    status_text = "📝 Draft" if contest.status == "draft" else "🟢 Voting open"
    closes_text = (
        f"<t:{int(datetime.fromisoformat(contest.closes_at).timestamp())}:F>"
        if contest.closes_at
        else "Not scheduled until publication"
    )
    entry_lines = "\n".join(
        f"#{entry.entry_number} — {discord.utils.escape_markdown(entry.contestant_name)}"
        for entry in entries[:25]
    ) or "No entries yet"
    embed = discord.Embed(
        title=f"🍪 {contest.title}",
        description=(
            f"**Status:** {status_text}\n"
            f"**Entries:** {len(entries)}\n"
            f"**Votes recorded:** {votes}\n"
            f"**Closes:** {closes_text}\n\n"
            f"**Reminders:** Every {CONTEST_REMINDER_INTERVAL_HOURS} hours + final {CONTEST_FINAL_REMINDER_HOURS} hours\n\n"
            f"{entry_lines}"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@contest_group.command(name="close", description="Close voting now and announce the winner.")
@contest_manager_only()
async def contest_close_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    contest = await contest_store.get_active_contest(interaction.guild.id)
    if contest is None or contest.status != "open":
        await interaction.response.send_message(
            "There is no open contest to close.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        announced = await close_contest_and_announce(contest.contest_id)
    except ContestError as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return
    result_note = "and the results were announced" if announced else "but CHiP could not post the result message"
    await interaction.followup.send(
        f"✅ Voting for **{contest.title}** is closed {result_note}.",
        ephemeral=True,
    )


@contest_group.command(name="cancel", description="Cancel the current unpublished draft.")
@contest_manager_only()
async def contest_cancel_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestManagerRequired()
    try:
        contest = await contest_store.cancel_draft(interaction.guild.id)
    except ContestError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.send_message(
        f"Cancelled the unpublished draft **{contest.title}**.",
        ephemeral=True,
    )


@contest_group.command(name="manager-add", description="Allow someone to manage contests.")
@app_commands.describe(member="MsMax or another trusted contest co-manager.")
@contest_owner_only()
async def contest_manager_add_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        raise ContestOwnerRequired()
    if member.bot:
        await interaction.response.send_message(
            "Contest managers must be human members.",
            ephemeral=True,
        )
        return
    await contest_store.add_manager(
        interaction.guild.id,
        member.id,
        interaction.user.id,
    )
    await interaction.response.send_message(
        f"✅ {member.mention} can now manage CHiP contests with you.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@contest_group.command(name="manager-remove", description="Remove a contest co-manager.")
@app_commands.describe(member="Contest co-manager to remove.")
@contest_owner_only()
async def contest_manager_remove_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        raise ContestOwnerRequired()
    removed = await contest_store.remove_manager(interaction.guild.id, member.id)
    message = (
        f"Removed {member.mention} from CHiP's contest managers."
        if removed
        else f"{member.mention} was not a saved contest manager."
    )
    await interaction.response.send_message(
        message,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@contest_group.command(name="manager-list", description="List contest managers.")
@contest_owner_only()
async def contest_manager_list_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        raise ContestOwnerRequired()
    manager_ids = await contest_store.list_managers(interaction.guild.id)
    mentions = [f"<@{interaction.guild.owner_id}> — Server Owner"]
    mentions.extend(f"<@{manager_id}>" for manager_id in manager_ids)
    await interaction.response.send_message(
        "🍪 **CHiP Contest Managers**\n" + "\n".join(mentions),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


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
        "CHiP is currently undergoing maintenance for updates or bug fixes. "
        "Translation and other CHiP services may be temporarily unavailable. "
        "We’ll notify everyone when CHiP is back online."
    )
    if clean_reason:
        description += f"\n\n**Reason:** {clean_reason}"

    announced = await post_chip_announcement(
        "🛠️ CHiP Is Under Maintenance",
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
        "✅ CHiP Is Back Online",
        "Maintenance is complete. Translation and other CHiP services are "
        "available again.",
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
        details = "Maintenance is **OFF**. CHiP services are running normally."
    await interaction.response.send_message(details, ephemeral=True)


chip_group = app_commands.Group(
    name="chip",
    description="Talk to and manage CHiP's member chat.",
)


@chip_group.command(name="chat", description="Ask CHiP a question or continue your conversation.")
@app_commands.describe(message="What you want to say to CHiP.")
async def chip_chat_command(
    interaction: discord.Interaction,
    message: str,
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "CHiP chat is available inside the CMON server, not in DMs.",
            ephemeral=True,
        )
        return

    clean_message = message.strip()
    if not clean_message:
        await interaction.response.send_message(
            "Give CHiP a message or question first. 🍪",
            ephemeral=True,
        )
        return
    if len(clean_message) > CHIP_CHAT_MAX_PROMPT_CHARACTERS:
        await interaction.response.send_message(
            f"Please keep each CHiP message under "
            f"**{CHIP_CHAT_MAX_PROMPT_CHARACTERS:,} characters**.",
            ephemeral=True,
        )
        return

    maintenance_active, _, _ = await maintenance_state()
    if maintenance_active:
        await interaction.response.send_message(
            "🛠️ CHiP is currently under maintenance. Chat will return when "
            "maintenance is complete.",
            ephemeral=True,
        )
        return

    retry_after = chip_chat_retry_after(
        interaction.guild_id,
        interaction.user.id,
    )
    if retry_after > 0:
        await interaction.response.send_message(
            f"CHiP needs a quick cookie break—try again in **{retry_after:.0f} seconds**.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)
    active_group = translation_group_for_channel(interaction.channel_id)
    if active_group is not None and interaction.guild is not None:
        group_name, group_channels = active_group
        await relay_text_across_translation_group(
            guild=interaction.guild,
            group_name=group_name,
            group_channels=group_channels,
            source_channel_id=interaction.channel_id,
            source_message_id=interaction.id,
            original_text=clean_message,
            media=RelayMedia(),
            username=interaction.user.display_name[:80],
            avatar_url=str(interaction.user.display_avatar.url),
            author_id=interaction.user.id,
            request_kind="message",
            include_source=True,
        )
    try:
        reply = await generate_chip_chat_reply(
            clean_message,
            interaction.guild_id,
            interaction.user.id,
            interaction.id,
        )
    except ChipChatUnavailable as exc:
        await interaction.followup.send(
            str(exc),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    chunks = split_discord_text(reply)
    for index, chunk in enumerate(chunks):
        prefix = "🍪 **CHiP:**\n" if index == 0 else ""
        content = prefix + chunk
        sent_reply = await interaction.followup.send(
            content,
            allowed_mentions=discord.AllowedMentions.none(),
            wait=True,
        )
        if (
            active_group is not None
            and interaction.guild is not None
            and bot.user is not None
        ):
            group_name, group_channels = active_group
            await relay_text_across_translation_group(
                guild=interaction.guild,
                group_name=group_name,
                group_channels=group_channels,
                source_channel_id=interaction.channel_id,
                source_message_id=sent_reply.id,
                original_text=content,
                media=RelayMedia(),
                username=bot.user.name[:80],
                avatar_url=str(bot.user.display_avatar.url),
                author_id=bot.user.id,
                request_kind="chip_chat_translation",
            )


@chip_group.command(name="reset", description="Forget your recent conversation with CHiP.")
async def chip_reset_command(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "There is no CMON conversation to reset here.",
            ephemeral=True,
        )
        return
    removed = await chat_history_store.reset(
        interaction.guild_id,
        interaction.user.id,
    )
    message = (
        "CHiP forgot your recent conversation. Fresh cookie slate! 🍪"
        if removed
        else "CHiP did not have any saved conversation for you yet."
    )
    await interaction.response.send_message(message, ephemeral=True)


@chip_group.command(name="status", description="Show CHiP chat and shared AI budget status.")
@app_commands.checks.has_permissions(administrator=True)
async def chip_status_command(interaction: discord.Interaction) -> None:
    status = await get_terra_status()
    paused = await get_setting("chip_chat_paused", "0") == "1"
    channel_id = await get_chip_chat_channel_id()
    if not chip_chat_client.configured:
        mode = "⚪ Not configured"
    elif paused:
        mode = "⏸️ Paused by an administrator"
    elif status.remaining_usd <= 0:
        mode = "🛑 Shared AI cap reached"
    else:
        mode = "🟢 Active"
    channel_text = (
        f"<#{channel_id}>"
        if channel_id
        else "No dedicated channel — `/chip chat` and @mentions still work"
    )
    embed = discord.Embed(
        title="🍪 CHiP Member Chat",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Mode", value=mode, inline=False)
    embed.add_field(name="Dedicated channel", value=channel_text, inline=False)
    embed.add_field(name="Model", value=f"`{CHIP_CHAT_MODEL}`", inline=True)
    embed.add_field(
        name="Chat usage",
        value=f"{status.chat_requests:,} replies · ${status.chat_cost_usd:.4f}",
        inline=True,
    )
    embed.add_field(
        name="Shared AI cap",
        value=(
            f"${status.total_cost_usd:.4f} / ${status.budget_usd:.2f}\n"
            f"${status.remaining_usd:.4f} remaining"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@chip_group.command(name="channel", description="Choose a channel where members can chat normally with CHiP.")
@app_commands.describe(channel="The dedicated text channel for CHiP conversations.")
@app_commands.checks.has_permissions(administrator=True)
async def chip_channel_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    await set_settings({"chip_chat_channel_id": str(channel.id)})
    await interaction.response.send_message(
        f"CHiP's dedicated chat channel is now {channel.mention}. Members can "
        "type there normally, use `/chip chat`, or mention CHiP elsewhere.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@chip_group.command(name="channel-off", description="Turn off CHiP's dedicated chat channel.")
@app_commands.checks.has_permissions(administrator=True)
async def chip_channel_off_command(interaction: discord.Interaction) -> None:
    await set_settings({"chip_chat_channel_id": "0"})
    await interaction.response.send_message(
        "The dedicated CHiP chat channel is off. `/chip chat` and direct "
        "@mentions still work.",
        ephemeral=True,
    )


@chip_group.command(name="pause", description="Pause member chat without pausing Terra translation.")
@app_commands.checks.has_permissions(administrator=True)
async def chip_pause_command(interaction: discord.Interaction) -> None:
    await set_settings({"chip_chat_paused": "1"})
    await interaction.response.send_message(
        "CHiP member chat is **paused**. Translation and contests are unchanged.",
        ephemeral=True,
    )


@chip_group.command(name="resume", description="Resume member chat if shared budget remains.")
@app_commands.checks.has_permissions(administrator=True)
async def chip_resume_command(interaction: discord.Interaction) -> None:
    if not chip_chat_client.configured:
        await interaction.response.send_message(
            "CHiP chat cannot resume because `OPENAI_API_KEY` is not configured "
            "in Railway.",
            ephemeral=True,
        )
        return
    status = await get_terra_status()
    if status.remaining_usd <= 0:
        await interaction.response.send_message(
            "CHiP chat cannot resume because the shared AI cap has been reached. "
            "Add prepaid credit first, then raise `/terra limit`.",
            ephemeral=True,
        )
        return
    await set_settings({"chip_chat_paused": "0"})
    await interaction.response.send_message(
        "CHiP member chat is **active** again. 🍪",
        ephemeral=True,
    )


terra_group = app_commands.Group(
    name="terra",
    description="Track and control CHiP's Terra and shared AI budget.",
)


@terra_group.command(name="status", description="Show Terra, chat, and shared AI budget usage.")
@app_commands.checks.has_permissions(administrator=True)
async def terra_status_command(interaction: discord.Interaction) -> None:
    status = await get_terra_status()
    chat_paused = await get_setting("chip_chat_paused", "0") == "1"
    if not terra_clarifier.configured:
        terra_mode = "⚪ Not configured — Azure-only"
    elif status.paused:
        terra_mode = "⏸️ Paused — Azure-only"
    elif status.remaining_usd <= 0:
        terra_mode = "🛑 Shared cap reached — Azure-only"
    else:
        terra_mode = "🟢 Active for Spanish messages"
    if not chip_chat_client.configured:
        chat_mode = "⚪ Not configured"
    elif chat_paused:
        chat_mode = "⏸️ Paused"
    elif status.remaining_usd <= 0:
        chat_mode = "🛑 Shared cap reached"
    else:
        chat_mode = "🟢 Active"

    embed = discord.Embed(
        title="🍪 CHiP AI Usage & Shared Cap",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Terra translation", value=terra_mode, inline=False)
    embed.add_field(name="Member chat", value=chat_mode, inline=False)
    embed.add_field(
        name="Combined tracked total",
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
        name="Terra usage",
        value=(
            f"{status.terra_messages:,} Spanish messages / "
            f"{status.terra_requests:,} requests\n${status.terra_cost_usd:.4f}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Chat usage",
        value=(
            f"{status.chat_messages:,} member messages / "
            f"{status.chat_requests:,} replies\n${status.chat_cost_usd:.4f}"
        ),
        inline=True,
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


@terra_group.command(name="limit", description="Change CHiP's cumulative shared AI spending cap.")
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
        "Azure-only for Spanish and member chat will remain unavailable until "
        "you raise it."
        if status.total_cost_usd >= status.budget_usd
        else ""
    )
    await interaction.response.send_message(
        f"CHiP's cumulative shared AI cap is now **${amount_usd:.2f}**."
        f"{mode_note}\n"
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


bot.tree.add_command(contest_group)
bot.tree.add_command(maintenance_group)
bot.tree.add_command(chip_group)
bot.tree.add_command(terra_group)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    original_error = getattr(error, "original", error)
    if isinstance(original_error, ContestOwnerRequired):
        message = "Only the server owner can change CHiP's contest managers."
    elif isinstance(original_error, ContestManagerRequired):
        message = "Only the server owner and approved contest managers can use that command."
    elif isinstance(original_error, app_commands.MissingPermissions):
        message = "Only a server administrator can use that CHiP command."
    else:
        message = "CHiP could not complete that command. Check the private bot logs."
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

    if not getattr(bot, "contest_views_added", False):
        restored_views = await restore_contest_vote_views()
        bot.contest_views_added = True
        log.info("Restored %s active contest vote buttons.", restored_views)

    if not contest_auto_close_loop.is_running():
        contest_auto_close_loop.start()

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
    log.info(
        "Member chat | Luna: %s | Chat model: %s | Dedicated channel: %s",
        chip_chat_client.configured,
        CHIP_CHAT_MODEL,
        await get_chip_chat_channel_id() or "not set",
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


async def send_chip_message_reply(
    message: discord.Message,
    reply: str,
) -> List[discord.Message]:
    sent_messages: List[discord.Message] = []
    chunks = split_discord_text(reply)
    for index, chunk in enumerate(chunks):
        content = ("🍪 **CHiP:**\n" if index == 0 else "") + chunk
        if index == 0:
            sent_messages.append(
                await message.reply(
                    content,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
        else:
            sent_messages.append(
                await message.channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
    return sent_messages


async def send_and_mirror_chip_reply(
    message: discord.Message,
    reply: str,
    active_group: Optional[Tuple[str, Dict[int, str]]],
) -> None:
    """Reply locally, then mirror CHiP's answer to linked language channels."""

    sent_replies = await send_chip_message_reply(message, reply)
    if active_group is None or bot.user is None:
        return

    group_name, group_channels = active_group
    reply_chunks = split_discord_text(reply)
    for index, (chunk, sent_reply) in enumerate(zip(reply_chunks, sent_replies)):
        relay_content = ("🍪 **CHiP:**\n" if index == 0 else "") + chunk
        await relay_text_across_translation_group(
            guild=message.guild,
            group_name=group_name,
            group_channels=group_channels,
            source_channel_id=message.channel.id,
            source_message_id=sent_reply.id,
            original_text=relay_content,
            media=RelayMedia(),
            username=bot.user.name[:80],
            avatar_url=str(bot.user.display_avatar.url),
            author_id=bot.user.id,
            request_kind="chip_chat_translation",
        )


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
    chat_channel_id = await get_chip_chat_channel_id()
    bot_user_id = bot.user.id if bot.user is not None else 0
    is_direct_mention = bool(
        bot.user is not None and bot.user in message.mentions
    )
    is_chat_channel = bool(
        chat_channel_id and message.channel.id == chat_channel_id
    )
    if is_direct_mention or is_chat_channel:
        prompt = strip_chip_mention(message.content, bot_user_id)
        if maintenance_active:
            await send_chip_message_reply(
                message,
                "🛠️ I’m currently under maintenance. Member chat will return "
                "when maintenance is complete.",
            )
            return

        active_group = translation_group_for_channel(message.channel.id)
        if active_group is not None:
            group_name, group_channels = active_group
            original_text = message.content.strip()
            media = await collect_relay_media(message)
            if original_text or media.has_media:
                await relay_text_across_translation_group(
                    guild=message.guild,
                    group_name=group_name,
                    group_channels=group_channels,
                    source_channel_id=message.channel.id,
                    source_message_id=message.id,
                    original_text=original_text,
                    media=media,
                    username=message.author.display_name[:80],
                    avatar_url=str(message.author.display_avatar.url),
                    author_id=message.author.id,
                    request_kind="message",
                )

        if not prompt:
            await send_and_mirror_chip_reply(
                message,
                "Ask me a text question and I’ll do my best to help! 🍪",
                active_group,
            )
            return
        if len(prompt) > CHIP_CHAT_MAX_PROMPT_CHARACTERS:
            await send_and_mirror_chip_reply(
                message,
                f"Please keep each message under "
                f"{CHIP_CHAT_MAX_PROMPT_CHARACTERS:,} characters.",
                active_group,
            )
            return

        retry_after = chip_chat_retry_after(
            message.guild.id,
            message.author.id,
        )
        if retry_after > 0:
            await send_and_mirror_chip_reply(
                message,
                f"I need a quick cookie break—try again in "
                f"{retry_after:.0f} seconds. 🍪",
                active_group,
            )
            return

        try:
            async with message.channel.typing():
                reply = await generate_chip_chat_reply(
                    prompt,
                    message.guild.id,
                    message.author.id,
                    message.id,
                )
        except ChipChatUnavailable as exc:
            await send_and_mirror_chip_reply(message, str(exc), active_group)
            return
        await send_and_mirror_chip_reply(message, reply, active_group)
        return

    if maintenance_active:
        return

    active_group = translation_group_for_channel(message.channel.id)

    if active_group is None:
        return

    group_name, group_channels = active_group
    original_text = message.content.strip()
    media = await collect_relay_media(message)

    # Media-only messages bypass both translation providers entirely.
    if not original_text and not media.has_media:
        return

    await relay_text_across_translation_group(
        guild=message.guild,
        group_name=group_name,
        group_channels=group_channels,
        source_channel_id=message.channel.id,
        source_message_id=message.id,
        original_text=original_text,
        media=media,
        username=message.author.display_name[:80],
        avatar_url=str(message.author.display_avatar.url),
        author_id=message.author.id,
        request_kind="message",
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

    original_text = source_message.content.strip()
    media = await collect_relay_media(source_message)
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
