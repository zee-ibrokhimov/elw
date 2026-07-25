"""Отправка сообщений в Telegram из процессов, которые ботом не являются.

Воркеру нужно уметь написать админу — про исчерпанные ретраи, про молчащий
канал, про нераспознанное письмо. Поднимать ради этого второй polling нельзя:
Telegram отдаёт обновления только одному потребителю, и второй polling отобрал
бы их у настоящего бота. Отправка же через HTTP API ни с кем не конфликтует.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from tutorsync.config import get_settings
from tutorsync.logging import get_logger

log = get_logger(__name__)

_bot: Bot | None = None


def _get_bot() -> Bot | None:
    global _bot
    if _bot is not None:
        return _bot
    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    _bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    return _bot


async def send_admin(text: str) -> int:
    """Шлёт текст всем админам. Возвращает число доставленных сообщений.

    Ошибка доставки не пробрасывается: алерт — это следствие уже случившейся
    проблемы, и падение на попытке о ней сообщить только заменит одну проблему
    другой, менее понятной.
    """
    bot = _get_bot()
    settings = get_settings()
    if bot is None or not settings.admin_ids:
        log.warning("notify.no_admins", text=text[:120])
        return 0

    delivered = 0
    for admin_id in sorted(settings.admin_ids):
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
            delivered += 1
        except TelegramAPIError as exc:
            log.error("notify.failed", admin_id=admin_id, error=str(exc))
    return delivered


async def close() -> None:
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
