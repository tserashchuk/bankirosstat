from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.config import get_settings
from app.models import RagStatus
from app.services.llm.prompts import (
    _milestone_rows,
    build_portfolio_summary_user_prompt,
    build_user_prompt,
    get_portfolio_summary_system,
    get_system_prompt,
    is_milestone_report_format,
    normalize_report_format,
)

TEMPERATURE = 0.2
MAX_OUTPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS_PRO = 4096
# R1/reasoner: CoT и ответ делят один max_tokens — иначе content часто пустой.
MAX_OUTPUT_TOKENS_REASONER = 32768
GEMINI_FLASH_MODEL = "gemini-2.5-flash"

MODEL_MAP = {
    "gemini-2.5-pro": ("gemini", "gemini-2.5-pro"),
    "gemini-2.5-flash": ("gemini", "gemini-2.5-flash"),
    "gemini-1.5-pro": ("gemini", "gemini-2.5-pro"),
    "claude-3-5-sonnet": ("claude", "claude-3-5-sonnet-20241022"),
    "deepseek-v3": ("deepseek", "deepseek-chat"),
    "deepseek-r1": ("deepseek", "deepseek-reasoner"),
}

DEFAULT_GEMINI_MODEL_KEY = "gemini-2.5-pro"

logger = logging.getLogger(__name__)

_gemini_clients: dict[int, Any] = {}


def _resolve_api_model(model_key: str) -> tuple[str, str]:
    return MODEL_MAP[model_key]


def _llm_timeout_sec() -> float:
    raw = get_settings().llm_timeout_seconds
    try:
        return max(120.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


def _llm_retry_count() -> int:
    try:
        return max(1, int(get_settings().llm_retry_count))
    except (TypeError, ValueError):
        return 3


def _max_output_tokens(model_id: str) -> int:
    if "pro" in model_id.lower():
        return MAX_OUTPUT_TOKENS_PRO
    return MAX_OUTPUT_TOKENS


def format_llm_error(exc: BaseException) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    low = msg.lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in low or "timed out" in low:
        return (
            f"ИИ не ответил за отведённое время ({_llm_timeout_sec():.0f} с). "
            "Попробуйте Gemini 2.5 Flash или отчёт по одному проекту."
        )
    if "disconnect" in low or "connection reset" in low or "server disconnected" in low:
        return (
            "Соединение с Google Gemini оборвалось во время генерации. "
            "Повторите запуск; при повторных сбоях выберите Gemini 2.5 Flash."
        )
    if "location is not supported" in low or "failed_precondition" in low:
        return (
            "Google Gemini недоступен из вашего региона (запрос идёт с IP сервера/Docker). "
            "Выберите DeepSeek V3 или Claude в списке моделей, либо задайте прокси "
            "(GOOGLE_GEMINI_BASE_URL в .env)."
        )
    return msg


def _is_transient_error(exc: BaseException) -> bool:
    transient_types = ("RemoteProtocolError", "ReadTimeout", "ConnectError", "ConnectTimeout")
    if type(exc).__name__ in transient_types:
        return True
    low = str(exc).lower()
    return any(
        token in low
        for token in (
            "disconnect",
            "connection reset",
            "temporarily unavailable",
            "service unavailable",
            " 502",
            " 503",
            " 429",
        )
    )


def _get_gemini_client():
    from google import genai

    timeout_sec = _llm_timeout_sec()
    timeout_ms = int(timeout_sec * 1000)
    cached = _gemini_clients.get(timeout_ms)
    if cached is not None:
        return cached

    settings = get_settings()
    httpx_timeout = httpx.Timeout(
        timeout=timeout_sec,
        connect=60.0,
        read=timeout_sec,
        write=120.0,
        pool=60.0,
    )
    http_options: dict[str, Any] = {
        "timeout": timeout_ms,
        "async_client_args": {"timeout": httpx_timeout},
    }
    if settings.gemini_base_url:
        http_options["base_url"] = settings.gemini_base_url.rstrip("/")
    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options=http_options,
    )
    _gemini_clients[timeout_ms] = client
    return client


def _gemini_config(system_prompt: str, model_id: str) -> dict[str, Any]:
    return {
        "system_instruction": system_prompt,
        "temperature": TEMPERATURE,
        "max_output_tokens": _max_output_tokens(model_id),
        "automatic_function_calling": {"disable": True},
    }


async def _gemini_generate_stream(model_id: str, user_prompt: str, system_prompt: str) -> str:
    """Стриминг держит соединение живым — без него Pro часто обрывается ~30 с."""
    client = _get_gemini_client()
    stream = await client.aio.models.generate_content_stream(
        model=model_id,
        contents=user_prompt,
        config=_gemini_config(system_prompt, model_id),
    )
    parts: list[str] = []
    async for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            parts.append(text)
    result = "".join(parts).strip()
    if not result:
        raise RuntimeError("Gemini вернул пустой ответ")
    return result


async def _gemini_generate_once(model_id: str, user_prompt: str, system_prompt: str) -> str:
    return await asyncio.wait_for(
        _gemini_generate_stream(model_id, user_prompt, system_prompt),
        timeout=_llm_timeout_sec(),
    )


async def _call_gemini_with_retries(
    model_id: str,
    user_prompt: str,
    system_prompt: str,
) -> str:
    prompt_chars = len(user_prompt) + len(system_prompt)
    retries = _llm_retry_count()
    last_exc: BaseException | None = None

    logger.info(
        "Gemini request model=%s prompt_chars=%s retries=%s (stream)",
        model_id,
        prompt_chars,
        retries,
    )

    for attempt in range(1, retries + 1):
        try:
            return await _gemini_generate_once(model_id, user_prompt, system_prompt)
        except asyncio.TimeoutError as exc:
            last_exc = exc
            logger.warning(
                "Gemini timeout model=%s attempt=%s/%s",
                model_id,
                attempt,
                retries,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_transient_error(exc) or attempt >= retries:
                logger.exception(
                    "Gemini request failed model=%s attempt=%s/%s prompt_chars=%s",
                    model_id,
                    attempt,
                    retries,
                    prompt_chars,
                )
                break
            delay = min(30, 2**attempt * 2)
            logger.warning(
                "Gemini transient error, retry %s/%s in %ss: %s",
                attempt,
                retries,
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    if isinstance(last_exc, asyncio.TimeoutError):
        raise TimeoutError(format_llm_error(last_exc)) from last_exc
    if last_exc is not None:
        raise RuntimeError(format_llm_error(last_exc)) from last_exc
    raise RuntimeError("Gemini: неизвестная ошибка")


async def _call_gemini(model_id: str, user_prompt: str, system_prompt: str) -> str:
    try:
        return await _call_gemini_with_retries(model_id, user_prompt, system_prompt)
    except (TimeoutError, RuntimeError) as exc:
        settings = get_settings()
        if (
            model_id == "gemini-2.5-pro"
            and settings.gemini_fallback_to_flash
        ):
            logger.warning(
                "Gemini Pro недоступен (%s), повтор через %s",
                exc,
                GEMINI_FLASH_MODEL,
            )
            try:
                return await _call_gemini_with_retries(
                    GEMINI_FLASH_MODEL, user_prompt, system_prompt
                )
            except (TimeoutError, RuntimeError) as flash_exc:
                raise RuntimeError(
                    f"{format_llm_error(exc)} (Flash тоже не ответил: {format_llm_error(flash_exc)})"
                ) from flash_exc
        raise


async def _call_claude(model_id: str, user_prompt: str, system_prompt: str) -> str:
    from anthropic import AsyncAnthropic

    settings = get_settings()
    timeout = _llm_timeout_sec()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=timeout)
    try:
        message = await client.messages.create(
            model=model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=TEMPERATURE,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        logger.exception("Claude request failed model=%s", model_id)
        raise RuntimeError(format_llm_error(exc)) from exc
    parts = [b.text for b in message.content if hasattr(b, "text")]
    return "\n".join(parts)


async def _call_deepseek(model_id: str, user_prompt: str, system_prompt: str) -> str:
    from openai import AsyncOpenAI

    settings = get_settings()
    timeout = _llm_timeout_sec()
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=timeout,
    )
    is_reasoner = "reasoner" in model_id.lower() or model_id.endswith("-r1")
    # У reasoner system часто слабо держит формат — дублируем инструкции в user.
    if is_reasoner:
        messages = [
            {
                "role": "user",
                "content": (
                    f"{system_prompt.strip()}\n\n"
                    "———\n"
                    "Ниже данные и задание. Следуй формату из инструкций выше.\n"
                    "———\n\n"
                    f"{user_prompt}"
                ),
            }
        ]
        create_kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": MAX_OUTPUT_TOKENS_REASONER,
            "messages": messages,
        }
    else:
        create_kwargs = {
            "model": model_id,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    try:
        resp = await client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        logger.exception("DeepSeek request failed model=%s", model_id)
        raise RuntimeError(format_llm_error(exc)) from exc

    choice = resp.choices[0] if resp.choices else None
    message = choice.message if choice else None
    content = (getattr(message, "content", None) or "").strip()
    reasoning = (getattr(message, "reasoning_content", None) or "").strip()
    finish = getattr(choice, "finish_reason", None) if choice else None
    usage = getattr(resp, "usage", None)
    logger.info(
        "DeepSeek done model=%s finish=%s content_chars=%s reasoning_chars=%s usage=%s",
        model_id,
        finish,
        len(content),
        len(reasoning),
        usage,
    )
    if content:
        return content
    if is_reasoner and finish == "length":
        raise RuntimeError(
            "DeepSeek R1 исчерпал лимит токенов на рассуждениях и не вернул отчёт. "
            "Повторите запрос или выберите DeepSeek V3."
        )
    if is_reasoner and reasoning:
        raise RuntimeError(
            "DeepSeek R1 вернул только reasoning без финального текста. "
            "Повторите запрос или выберите DeepSeek V3."
        )
    raise RuntimeError("DeepSeek вернул пустой ответ")


async def complete_text(model_key: str, system_prompt: str, user_prompt: str) -> str:
    if model_key not in MODEL_MAP:
        raise ValueError(f"Unknown model: {model_key}")
    provider, model_id = _resolve_api_model(model_key)
    if provider == "gemini":
        return await _call_gemini(model_id, user_prompt, system_prompt)
    if provider == "claude":
        return await _call_claude(model_id, user_prompt, system_prompt)
    return await _call_deepseek(model_id, user_prompt, system_prompt)


def infer_rag_status(text: str) -> RagStatus:
    m = re.search(r"RAG_STATUS:\s*(GREEN|AMBER|RED)", text, re.IGNORECASE)
    if m:
        return RagStatus(m.group(1).upper())
    if "🔴" in text or "RED" in text.upper():
        return RagStatus.RED
    if "🟡" in text or "AMBER" in text.upper():
        return RagStatus.AMBER
    # Пустой/короткий ответ без явного статуса — не GREEN (частый глюк R1).
    if len((text or "").strip()) < 40:
        return RagStatus.AMBER
    return RagStatus.GREEN


def _strip_rag_line(text: str) -> str:
    return re.sub(r"\n?RAG_STATUS:\s*(GREEN|AMBER|RED)\s*\n?", "", text, flags=re.IGNORECASE).strip()


async def generate_report(
    model_key: str,
    project_name: str,
    snapshot: dict[str, Any],
    report_format: str | None = None,
    project_description: str | None = None,
    previous_reports: list[dict[str, Any]] | None = None,
) -> tuple[str, RagStatus]:
    if model_key not in MODEL_MAP:
        raise ValueError(f"Unknown model: {model_key}")

    fmt = normalize_report_format(report_format)
    if is_milestone_report_format(fmt) and not _milestone_rows(snapshot):
        return (
            "Milestones для этого проекта не заданы. "
            "Добавьте их на странице «Проекты» в карточке проекта.",
            RagStatus.AMBER,
        )

    system_prompt = get_system_prompt(fmt)
    user_prompt = build_user_prompt(
        project_name,
        snapshot,
        fmt,
        project_description,
        previous_reports,
    )

    raw = await complete_text(model_key, system_prompt, user_prompt)
    rag = infer_rag_status(raw)
    text = _strip_rag_line(raw)
    return text, rag


async def generate_portfolio_summary(
    model_key: str,
    items: list[dict[str, Any]],
    report_format: str | None = None,
) -> str:
    if not items:
        return ""
    user_prompt = build_portfolio_summary_user_prompt(items)
    system_prompt = get_portfolio_summary_system(report_format)
    raw = await complete_text(model_key, system_prompt, user_prompt)
    return _strip_rag_line(raw)
