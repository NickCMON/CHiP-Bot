"""Reliable translation helpers for CHiP.

Azure Translator handles the normal language fan-out. GPT-5.6 Terra is used
once for Spanish source messages to turn slang-heavy Spanish into natural
English before Azure translates that clarified English to the other languages.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import aiohttp
from openai import AsyncOpenAI


class TranslationError(RuntimeError):
    """A provider failed without exposing its response body to Discord."""


AZURE_LANGUAGE_CODES: Mapping[str, str] = {
    "en": "en",
    "es": "es",
    "pt": "pt",
    "fr": "fr",
    "de": "de",
    "tr": "tr",
    "ar": "ar",
    "zh-CN": "zh-Hans",
}

# Protect Discord markup, links, and code before any text leaves CHiP. This is
# what keeps custom/animated emojis, mentions, GIF links, and code intact.
SPECIAL_CONTENT_PATTERN = re.compile(
    r"```[\s\S]*?```"
    r"|`[^`\n]+`"
    r"|https?://[^\s<>]+"
    r"|<a?:[A-Za-z0-9_~]+:\d+>"
    r"|<@!?\d+>"
    r"|<@&\d+>"
    r"|<#\d+>"
    r"|<t:\d+(?::[tTdDfFR])?>"
    r"|(?:[\U0001F1E6-\U0001F1FF]{2})"
    r"|(?:[#*0-9]\ufe0f?\u20e3)"
    r"|(?:"
    r"[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2600-\u27BF\U0001F300-\U0001FAFF]"
    r"(?:\ufe0e|\ufe0f)?(?:[\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200d"
    r"[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2600-\u27BF\U0001F300-\U0001FAFF]"
    r"(?:\ufe0e|\ufe0f)?(?:[\U0001F3FB-\U0001F3FF])?)*"
    r")",
    re.IGNORECASE,
)

PLACEHOLDER_PREFIX = "ZXQCHIPTOKEN"
PLACEHOLDER_SUFFIX = "QXZ"
PLACEHOLDER_ID_PATTERN = re.compile(
    rf"{PLACEHOLDER_PREFIX}\s*(\d+)\s*{PLACEHOLDER_SUFFIX}",
    re.IGNORECASE,
)


def protect_text(text: str, protected_terms: Iterable[str]) -> Tuple[str, List[str]]:
    """Replace content translators must not modify with stable placeholders."""

    values: List[str] = []

    def save(value: str) -> str:
        token = f"{PLACEHOLDER_PREFIX}{len(values):04d}{PLACEHOLDER_SUFFIX}"
        values.append(value)
        return token

    result = SPECIAL_CONTENT_PATTERN.sub(lambda match: save(match.group(0)), text)

    # Longest terms first prevents a shorter term (for example, "Den") from
    # consuming part of a longer one ("Den 1"). Preserve the user's exact case.
    for term in sorted(set(protected_terms), key=len, reverse=True):
        if not term:
            continue
        pattern = re.compile(
            rf"(?<!\w){re.escape(term)}(?!\w)",
            re.IGNORECASE,
        )
        result = pattern.sub(lambda match: save(match.group(0)), result)

    return result, values


def restore_text(text: str, values: Sequence[str]) -> str:
    """Restore placeholders even if a provider changed their letter casing."""

    result = text
    for index, original in enumerate(values):
        token_pattern = re.compile(
            rf"{PLACEHOLDER_PREFIX}\s*0*{index}\s*{PLACEHOLDER_SUFFIX}",
            re.IGNORECASE,
        )
        result = token_pattern.sub(lambda _match, value=original: value, result)
    return result


def validate_translation(text: object) -> str:
    """Reject blank output and recognizable HTML error pages."""

    if not isinstance(text, str) or not text.strip():
        raise TranslationError("The translation provider returned no text.")

    cleaned = text.strip()
    lowered = cleaned.lower()
    if (
        "<!doctype html" in lowered
        or re.search(r"<html(?:\s|>)", lowered)
        or "error 500 (server error)!!1" in lowered
        or "that's an error. there was an error. please try again later" in lowered
        or "that’s an error. there was an error. please try again later" in lowered
    ):
        raise TranslationError("The translation provider returned an error page.")
    return cleaned


def ensure_placeholders_preserved(source_text: str, translated_text: str) -> None:
    """Reject a translation that lost, duplicated, or invented protected data."""

    source_ids = Counter(PLACEHOLDER_ID_PATTERN.findall(source_text))
    translated_ids = Counter(PLACEHOLDER_ID_PATTERN.findall(translated_text))
    if source_ids != translated_ids:
        raise TranslationError(
            "The translation provider changed protected Discord content."
        )


class AzureTranslator:
    """Small asynchronous client for Azure Translator REST API v3."""

    RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

    def __init__(
        self,
        key: str,
        endpoint: str,
        region: str = "",
        timeout_seconds: float = 15.0,
        retry_count: int = 3,
    ) -> None:
        self.key = key.strip()
        self.endpoint = endpoint.strip().rstrip("/")
        self.region = region.strip()
        self.timeout_seconds = timeout_seconds
        self.retry_count = max(1, retry_count)

    @property
    def configured(self) -> bool:
        return bool(self.key and self.endpoint)

    @property
    def translate_url(self) -> str:
        if self.endpoint.lower().endswith("/translate"):
            return self.endpoint
        return f"{self.endpoint}/translate"

    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: Sequence[str],
    ) -> Dict[str, str]:
        """Translate one string to several targets in a single Azure request."""

        unique_targets = list(dict.fromkeys(target_languages))
        if not unique_targets:
            return {}
        if not self.configured:
            raise TranslationError("Azure Translator is not configured.")

        try:
            source_code = AZURE_LANGUAGE_CODES[source_language]
            target_codes = [AZURE_LANGUAGE_CODES[target] for target in unique_targets]
        except KeyError as exc:
            raise TranslationError(f"Unsupported language code: {exc.args[0]}") from exc

        params: List[Tuple[str, str]] = [
            ("api-version", "3.0"),
            ("from", source_code),
        ]
        params.extend(("to", code) for code in target_codes)

        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/json",
            "X-ClientTraceId": str(uuid.uuid4()),
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        last_error: Exception | None = None

        for attempt in range(self.retry_count):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self.translate_url,
                        params=params,
                        headers=headers,
                        json=[{"text": text}],
                    ) as response:
                        if response.status != 200:
                            if (
                                response.status in self.RETRYABLE_STATUS_CODES
                                and attempt + 1 < self.retry_count
                            ):
                                retry_after = response.headers.get("Retry-After", "")
                                try:
                                    delay = min(float(retry_after), 3.0)
                                except ValueError:
                                    delay = min(0.5 * (2**attempt), 3.0)
                                await asyncio.sleep(max(delay, 0.25))
                                continue
                            raise TranslationError(
                                f"Azure Translator returned HTTP {response.status}."
                            )

                        try:
                            payload = await response.json(content_type=None)
                        except Exception as exc:
                            raise TranslationError(
                                "Azure Translator returned a non-JSON response."
                            ) from exc

                translations = payload[0]["translations"]
                if len(translations) != len(unique_targets):
                    raise TranslationError(
                        "Azure Translator returned an unexpected number of translations."
                    )

                results: Dict[str, str] = {}
                for target, item in zip(unique_targets, translations):
                    translated_text = validate_translation(item.get("text"))
                    ensure_placeholders_preserved(text, translated_text)
                    results[target] = translated_text
                return results
            except TranslationError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 < self.retry_count:
                    await asyncio.sleep(min(0.5 * (2**attempt), 3.0))
                    continue

        raise TranslationError("Azure Translator could not be reached.") from last_error


TERRA_INSTRUCTIONS = """You are CHiP's Spanish gaming-chat interpreter for a multilingual Discord server.
Translate the user's Spanish or Spanish-English message into natural conversational English.
Preserve the exact meaning, humor, sarcasm, teasing, slang, profanity intensity, and gaming tone.
Do not make jokes more offensive and do not sanitize harmless banter.
Keep usernames, Discord markdown, and every token shaped like ZXQCHIPTOKEN0000QXZ exactly unchanged.
Return only the English translation. Do not explain, label, quote, or summarize it."""


@dataclass(frozen=True)
class TerraUsage:
    request_id: str
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    estimated_cost_usd: float


def _field(value: object, name: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, dict):
        return int(value.get(name, default) or default)
    return int(getattr(value, name, default) or default)


def calculate_terra_cost(
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    input_price_per_million: float = 2.0,
    cached_price_per_million: float = 0.20,
    output_price_per_million: float = 12.0,
) -> float:
    """Estimate GPT-5.6 Terra cost from the Responses API usage object."""

    cached = min(max(cached_input_tokens, 0), max(input_tokens, 0))
    written = min(
        max(cache_write_tokens, 0),
        max(input_tokens - cached, 0),
    )
    ordinary = max(input_tokens - cached - written, 0)
    cache_write_price = input_price_per_million * 1.25

    return (
        ordinary * input_price_per_million
        + cached * cached_price_per_million
        + written * cache_write_price
        + max(output_tokens, 0) * output_price_per_million
    ) / 1_000_000


class TerraClarifier:
    """Spanish-to-English context pass using GPT-5.6 Terra Responses API."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.6-terra",
        input_price_per_million: float = 2.0,
        cached_price_per_million: float = 0.20,
        output_price_per_million: float = 12.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "gpt-5.6-terra"
        self.input_price_per_million = input_price_per_million
        self.cached_price_per_million = cached_price_per_million
        self.output_price_per_million = output_price_per_million
        # Create the network client lazily so an environment-specific HTTP
        # configuration problem cannot prevent the entire Discord bot starting.
        self.client: AsyncOpenAI | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def max_output_tokens_for(text: str) -> int:
        return min(900, max(128, len(text) // 2 + 64))

    def estimate_request_ceiling(self, text: str) -> float:
        """Conservative preflight estimate used to avoid crossing CHiP's cap."""

        # One character per token is intentionally conservative for this safety
        # check, and cache writes use their higher 1.25x price.
        input_token_ceiling = len(TERRA_INSTRUCTIONS) + len(text) + 64
        output_token_ceiling = self.max_output_tokens_for(text)
        return (
            input_token_ceiling * self.input_price_per_million * 1.25
            + output_token_ceiling * self.output_price_per_million
        ) / 1_000_000

    async def clarify_spanish(self, text: str) -> Tuple[str, TerraUsage]:
        if not self.api_key:
            raise TranslationError("GPT-5.6 Terra is not configured.")

        try:
            if self.client is None:
                self.client = AsyncOpenAI(api_key=self.api_key)
            response = await self.client.responses.create(
                model=self.model,
                instructions=TERRA_INSTRUCTIONS,
                input=text,
                reasoning={"effort": "none"},
                max_output_tokens=self.max_output_tokens_for(text),
                prompt_cache_key="chip-spanish-interpreter-v1",
                store=False,
            )
        except Exception as exc:
            raise TranslationError(
                f"GPT-5.6 Terra request failed ({type(exc).__name__})."
            ) from exc

        translated = validate_translation(getattr(response, "output_text", None))
        ensure_placeholders_preserved(text, translated)
        usage_object = getattr(response, "usage", None)
        details = getattr(usage_object, "input_tokens_details", None)
        input_tokens = _field(usage_object, "input_tokens")
        cached_tokens = _field(details, "cached_tokens")
        cache_write_tokens = _field(details, "cache_write_tokens")
        output_tokens = _field(usage_object, "output_tokens")

        # Responses normally include exact usage. If an SDK/provider response
        # ever omits it, record a deliberately conservative estimate rather
        # than allowing an untracked paid call through CHiP's budget guard.
        if input_tokens <= 0:
            input_tokens = len(TERRA_INSTRUCTIONS) + len(text) + 64
        if output_tokens <= 0:
            output_tokens = max(len(translated), 1)

        cost = calculate_terra_cost(
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            input_price_per_million=self.input_price_per_million,
            cached_price_per_million=self.cached_price_per_million,
            output_price_per_million=self.output_price_per_million,
        )
        usage = TerraUsage(
            request_id=str(getattr(response, "id", uuid.uuid4().hex)),
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )
        return translated, usage
