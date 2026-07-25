"""Точка входа бота.

Сейчас здесь только каркас: подключение к Telegram и проверка, что процесс
живой и видит базу. Сценарии ученика (/book, /my, перенос, отмена) — этап 4,
админские (/today, /block, очередь ручных задач) — этап 5.

Ученику намеренно не отвечает ничего похожего на работающее бронирование:
половина сценария хуже, чем честное «пока не готово».
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from tutorsync import __version__
from tutorsync.config import Settings, get_settings
from tutorsync.db.session import dispose_engine, get_sessionmaker
from tutorsync.gcal.oauth import OAuthNotConfiguredError, build_auth_url, has_credentials
from tutorsync.logging import get_logger

log = get_logger(__name__)


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def build_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("health"), F.from_user.id.in_(settings.admin_ids))
    async def cmd_health(message: Message) -> None:
        google = "не проверено"
        try:
            async with get_sessionmaker()() as session:
                await session.execute(sa.text("SELECT 1"))
                database = "ok"
                google = "подключён" if await has_credentials(session) else "не подключён"
        except Exception as exc:
            log.error("bot.health.db_unavailable", error=str(exc))
            database = f"недоступна: {exc}"

        mode = "включена" if settings.preply_gcal_linked else "выключена"
        await message.answer(
            f"tutorsync {__version__}\n"
            f"База: {database}\n"
            f"Google-аккаунт: {google}\n"
            f"Интеграция Preply ↔ Google Calendar: {mode}\n"
            f"Часовой пояс: {settings.owner_tz}"
        )

    @dp.message(Command("connect_google"), F.from_user.id.in_(settings.admin_ids))
    async def cmd_connect_google(message: Message) -> None:
        """Выдаёт одноразовую ссылку авторизации Google.

        Ссылку выдаёт бот, а не веб: так единственный вход в авторизацию закрыт
        тем же списком админов, что и остальные служебные команды, и публичному
        колбэку не нужен собственный пароль.
        """
        try:
            url = build_auth_url()
        except OAuthNotConfiguredError as exc:
            await message.answer(f"Не готово: {exc}")
            return

        async with get_sessionmaker()() as session:
            already = await has_credentials(session)

        warning = (
            "\n\n⚠️ Аккаунт уже подключён. Повторная авторизация заменит "
            "сохранённый токен — делай это только если доступ сломался."
            if already
            else ""
        )
        await message.answer(
            f"Ссылка действует 10 минут, открывать её должен ты сам:\n{url}"
            "\n\nGoogle покажет «приложение не проверено» — это ожидаемо: "
            "нажми «Дополнительные настройки» → «Перейти на сайт»." + warning,
            disable_web_page_preview=True,
        )

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        if is_admin(message.from_user.id if message.from_user else None, settings):
            await message.answer(
                "Каркас развёрнут. Доступны /health и /connect_google.\n"
                "Бронирование и админ-команды подключаются на следующих этапах."
            )
            return
        await message.answer(
            "Бот пока настраивается — записаться на урок ещё нельзя. "
            "Напишите позже, пожалуйста."
        )

    return dp


async def run_bot() -> None:
    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher(settings)

    me = await bot.get_me()
    log.info("bot.started", username=me.username, admins=len(settings.admin_ids))
    try:
        # Long polling: публичный маршрут боту не нужен, наружу смотрит только web.
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()
        await dispose_engine()
        log.info("bot.stopped")


def main() -> None:
    asyncio.run(run_bot())
