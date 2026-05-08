"""
Telegram countdown bot.

Каждый день обновляет закреплённое сообщение в указанном чате
текстом вида: "<Название соревнований>\nОсталось N дней".
"""

import json
import logging
import os
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ----------------------------- настройки -----------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # твой Telegram user_id
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Almaty"))
UPDATE_HOUR = int(os.environ.get("UPDATE_HOUR", "9"))   # час ежедневного обновления
UPDATE_MINUTE = int(os.environ.get("UPDATE_MINUTE", "0"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ----------------------------- хранение состояния -----------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("Не удалось прочитать state-файл: %s", e)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ----------------------------- логика обновления -----------------------------

def days_until(target_iso: str) -> int:
    target = date.fromisoformat(target_iso)
    today = datetime.now(TIMEZONE).date()
    return (target - today).days


def format_message(name: str, target_iso: str) -> str:
    days = days_until(target_iso)
    if days > 0:
        # склонение слова "день"
        n = days % 100
        if 11 <= n <= 14:
            word = "дней"
        else:
            n = days % 10
            if n == 1:
                word = "день"
            elif 2 <= n <= 4:
                word = "дня"
            else:
                word = "дней"
        tail = f"Осталось {days} {word}"
    elif days == 0:
        tail = "Соревнования сегодня! 🔥"
    else:
        tail = f"Соревнования прошли {-days} дн. назад"
    return f"<b>{name}</b>\n{tail}"


async def refresh_pinned(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт новое сообщение и закрепляет его, открепляя предыдущее."""
    state = load_state()
    chat_id = state.get("chat_id")
    name = state.get("name")
    target = state.get("target_date")
    if not (chat_id and name and target):
        logger.info("Нет настройки — пропускаю обновление")
        return

    text = format_message(name, target)

    bot = context.bot
    try:
        # отправляем новое сообщение
        msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        )

        # открепляем старое, если было
        old_msg_id = state.get("pinned_message_id")
        if old_msg_id:
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception as e:
                logger.warning("Не удалось открепить старое сообщение: %s", e)

        # закрепляем новое (без уведомления, чтобы не спамить)
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=msg.message_id, disable_notification=True
        )

        # удаляем системное сообщение "бот закрепил..."
        # Telegram отдаёт его как событие, у нас его id-шника нет — просто игнорируем.

        state["pinned_message_id"] = msg.message_id
        save_state(state)
        logger.info("Закреп обновлён: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.exception("Ошибка при обновлении закрепа: %s", e)


# ----------------------------- команды -----------------------------

def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else "?"
    chat_id = update.effective_chat.id if update.effective_chat else "?"
    await update.message.reply_text(
        "Привет! Я бот-обратный-отсчёт.\n\n"
        "Команды (только для админа):\n"
        "• <code>/setup YYYY-MM-DD Название соревнований</code> — задать целевую дату\n"
        "• <code>/show</code> — показать текущую настройку и пересчитать закреп\n"
        "• <code>/stop</code> — отключить обновления\n\n"
        f"Твой user_id: <code>{user_id}</code>\n"
        f"ID этого чата: <code>{chat_id}</code>\n\n"
        "Чтобы бот закреплял сообщения — добавь его в нужный чат и сделай админом "
        "с правом «Закреплять сообщения».",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Эта команда доступна только админу.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование:\n/setup YYYY-MM-DD Название соревнований\n\n"
            "Пример:\n/setup 2026-09-01 Чемпионат Казахстана"
        )
        return

    target_str = context.args[0]
    name = " ".join(context.args[1:]).strip()

    try:
        target = date.fromisoformat(target_str)
    except ValueError:
        await update.message.reply_text(
            "Неверный формат даты. Нужен YYYY-MM-DD, например 2026-09-01."
        )
        return

    state = load_state()
    state["chat_id"] = update.effective_chat.id
    state["target_date"] = target.isoformat()
    state["name"] = name
    # старый закреп больше не наш, забываем его
    state.pop("pinned_message_id", None)
    save_state(state)

    await update.message.reply_text(
        f"Готово.\nСоревнования: {name}\nДата: {target.isoformat()}\n"
        f"Сейчас обновлю закреп."
    )
    await refresh_pinned(context)


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state = load_state()
    if not state.get("target_date"):
        await update.message.reply_text("Настройка пуста. Используй /setup.")
        return
    await update.message.reply_text(
        f"Чат: {state.get('chat_id')}\n"
        f"Соревнования: {state.get('name')}\n"
        f"Дата: {state.get('target_date')}\n"
        f"Сейчас обновлю закреп."
    )
    await refresh_pinned(context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    await update.message.reply_text("Настройка очищена. Обновления отключены.")


# ----------------------------- запуск -----------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")
    if not ADMIN_ID:
        raise SystemExit("Не задана переменная окружения ADMIN_ID")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # ежедневная задача
    app.job_queue.run_daily(
        refresh_pinned,
        time=dtime(hour=UPDATE_HOUR, minute=UPDATE_MINUTE, tzinfo=TIMEZONE),
        name="daily_refresh",
    )
    # и один раз через 5 секунд после старта — чтобы сразу обновить после деплоя
    app.job_queue.run_once(refresh_pinned, when=5, name="initial_refresh")

    logger.info(
        "Бот запущен. Ежедневное обновление в %02d:%02d (%s)",
        UPDATE_HOUR, UPDATE_MINUTE, TIMEZONE,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
