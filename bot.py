"""
Telegram countdown bot.

Каждый день обновляет закреплённое сообщение в указанном чате
списком вида:

    <Название события №1>
    Осталось N дней

    <Название события №2>
    Осталось M дней
"""

import json
import logging
import os
import re
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
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Asia/Almaty"))
UPDATE_HOUR = int(os.environ.get("UPDATE_HOUR", "9"))   # час ежедневного обновления
UPDATE_MINUTE = int(os.environ.get("UPDATE_MINUTE", "0"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))

SLUG_RE = re.compile(r"^[a-z0-9_-]{1,16}$")

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
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("Не удалось прочитать state-файл: %s", e)
        return {}

    # миграция со старой схемы (одно событие на верхнем уровне)
    if "events" not in state and state.get("target_date") and state.get("name"):
        state["events"] = [{
            "slug": "default",
            "name": state["name"],
            "target_date": state["target_date"],
        }]
        state.pop("name", None)
        state.pop("target_date", None)

    state.setdefault("events", [])
    return state


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ----------------------------- логика обновления -----------------------------

def days_until(target_iso: str) -> int:
    target = date.fromisoformat(target_iso)
    today = datetime.now(TIMEZONE).date()
    return (target - today).days


def format_event_line(name: str, target_iso: str) -> str:
    days = days_until(target_iso)
    if days > 0:
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
        tail = f"Осталось {days} {word}, Работаем!"
    elif days == 0:
        tail = "Соревнования сегодня! 🔥"
    else:
        tail = f"Соревнования прошли {-days} дн. назад"
    return f"<b>{name}</b>\n{tail}"


def build_pinned_text(events: list) -> str:
    sorted_events = sorted(events, key=lambda e: e["target_date"])
    return "\n\n".join(
        format_event_line(e["name"], e["target_date"]) for e in sorted_events
    )


async def refresh_pinned(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт новое сообщение со списком событий и закрепляет его."""
    state = load_state()
    chat_id = state.get("chat_id")
    events = state.get("events", [])
    bot = context.bot

    if not chat_id:
        logger.info("Нет chat_id — пропускаю обновление")
        return

    # если событий нет — снимаем старый закреп и выходим
    if not events:
        old_msg_id = state.get("pinned_message_id")
        if old_msg_id:
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception as e:
                logger.warning("Не удалось открепить старое сообщение: %s", e)
            state.pop("pinned_message_id", None)
            save_state(state)
        logger.info("Список событий пуст — закреп снят")
        return

    text = build_pinned_text(events)

    try:
        msg = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        )

        old_msg_id = state.get("pinned_message_id")
        if old_msg_id:
            try:
                await bot.unpin_chat_message(chat_id=chat_id, message_id=old_msg_id)
            except Exception as e:
                logger.warning("Не удалось открепить старое сообщение: %s", e)

        await bot.pin_chat_message(
            chat_id=chat_id, message_id=msg.message_id, disable_notification=True
        )

        state["pinned_message_id"] = msg.message_id
        save_state(state)
        logger.info("Закреп обновлён: %s", text.replace("\n", " | "))
    except Exception as e:
        logger.exception("Ошибка при обновлении закрепа: %s", e)


# ----------------------------- команды -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else "?"
    chat_id = update.effective_chat.id if update.effective_chat else "?"
    await update.message.reply_text(
        "Привет! Я бот-обратный-отсчёт.\n\n"
        "Команды:\n"
        "• <code>/setup &lt;slug&gt; YYYY-MM-DD Название</code> — добавить или обновить событие\n"
        "• <code>/remove &lt;slug&gt;</code> — удалить событие по ключу\n"
        "• <code>/list</code> — показать все события и пересчитать закреп\n"
        "• <code>/stop</code> — очистить все события\n\n"
        "<code>slug</code> — короткий ключ (a-z, 0-9, _-), до 16 символов. "
        "Пример: <code>/setup kz 2026-09-01 Чемпионат Казахстана</code>\n\n"
        f"Твой user_id: <code>{user_id}</code>\n"
        f"ID этого чата: <code>{chat_id}</code>\n\n"
        "Чтобы бот закреплял сообщения — добавь его в нужный чат и сделай админом "
        "с правом «Закреплять сообщения».",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 3:
        await update.message.reply_text(
            "Использование:\n/setup <slug> YYYY-MM-DD Название\n\n"
            "Пример:\n/setup kz 2026-09-01 Чемпионат Казахстана"
        )
        return

    slug = context.args[0].strip().lower()
    target_str = context.args[1]
    name = " ".join(context.args[2:]).strip()

    if not SLUG_RE.match(slug):
        await update.message.reply_text(
            "Неверный slug. Допустимы a-z, 0-9, _ и -, до 16 символов."
        )
        return

    try:
        target = date.fromisoformat(target_str)
    except ValueError:
        await update.message.reply_text(
            "Неверный формат даты. Нужен YYYY-MM-DD, например 2026-09-01."
        )
        return

    state = load_state()
    state["chat_id"] = update.effective_chat.id

    events = state.get("events", [])
    new_event = {"slug": slug, "name": name, "target_date": target.isoformat()}
    for i, ev in enumerate(events):
        if ev["slug"] == slug:
            events[i] = new_event
            break
    else:
        events.append(new_event)
    state["events"] = events

    save_state(state)

    await update.message.reply_text(
        f"Готово.\nSlug: {slug}\nСоревнования: {name}\nДата: {target.isoformat()}\n"
        f"Сейчас обновлю закреп."
    )
    await refresh_pinned(context)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text(
            "Использование:\n/remove <slug>\n\nПример: /remove kz"
        )
        return

    slug = context.args[0].strip().lower()
    state = load_state()
    events = state.get("events", [])
    new_events = [e for e in events if e["slug"] != slug]

    if len(new_events) == len(events):
        await update.message.reply_text(f"Событие с slug «{slug}» не найдено.")
        return

    state["events"] = new_events
    save_state(state)

    await update.message.reply_text(
        f"Удалил «{slug}». Сейчас обновлю закреп."
    )
    await refresh_pinned(context)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    events = state.get("events", [])
    if not events:
        await update.message.reply_text("Список событий пуст. Используй /setup.")
        return

    sorted_events = sorted(events, key=lambda e: e["target_date"])
    lines = [f"Чат: {state.get('chat_id')}", "События:"]
    for e in sorted_events:
        lines.append(f"  • [{e['slug']}] {e['target_date']} — {e['name']}")
    lines.append("Сейчас обновлю закреп.")
    await update.message.reply_text("\n".join(lines))
    await refresh_pinned(context)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    await update.message.reply_text("Настройка очищена. Обновления отключены.")


# ----------------------------- запуск -----------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задана переменная окружения BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler(["list", "show"], cmd_list))
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
