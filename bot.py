import asyncio
import os
import random
import re
import time
import traceback
from itertools import product
from html import escape
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import find_dotenv, load_dotenv

from database import Database

# Если .env не подхватывается в Pydroid 3, можно вставить токен прямо сюда:
# MANUAL_BOT_TOKEN = "123456789:ABCDEF..."
MANUAL_BOT_TOKEN = ""


def load_environment():
    """
    В обычном запуске python-dotenv сам находит .env.
    В Pydroid 3 файл иногда запускается через exec(...), поэтому .env может не находиться.
    Загружаем .env несколькими способами и оставляем ручной fallback выше.
    """
    candidates: list[Path] = []

    # Текущая рабочая папка.
    candidates.append(Path.cwd() / ".env")

    # Папка файла, если __file__ доступен.
    try:
        candidates.append(Path(__file__).resolve().parent / ".env")
    except Exception:
        pass

    # Поиск python-dotenv от текущей папки вверх.
    try:
        found = find_dotenv(filename=".env", usecwd=True)
        if found:
            candidates.append(Path(found))
    except Exception:
        pass

    loaded = False
    for env_path in candidates:
        try:
            if env_path.exists():
                load_dotenv(env_path, override=True)
                loaded = True
        except Exception:
            pass

    if not loaded:
        # Последняя попытка стандартным способом.
        load_dotenv()


load_environment()

BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or MANUAL_BOT_TOKEN).strip()
DB_PATH = os.getenv("DB_PATH", "casino_bot.sqlite3")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "7858855414").split(",")
    if x.strip().isdigit()
}

BONUS_AMOUNT = int(os.getenv("BONUS_AMOUNT", "10"))
BONUS_COOLDOWN_SECONDS = int(os.getenv("BONUS_COOLDOWN_SECONDS", "300"))
FREE_SPIN_COOLDOWN_SECONDS = int(os.getenv("FREE_SPIN_COOLDOWN_SECONDS", "86400"))
DEFAULT_GLOBAL_WIN_CHANCE = float(os.getenv("DEFAULT_GLOBAL_WIN_CHANCE", "45"))

router = Router()
rng = random.SystemRandom()

# Активные партии Блек Джека хранятся в памяти до завершения игры.
# Ключ: (chat_id, user_id)
BLACKJACK_GAMES: dict[tuple[int, int], dict] = {}

# Дуэли Блек Джека: приглашения и активные игры. Хранятся до перезапуска бота.
BLACKJACK_DUEL_INVITES: dict[int, dict] = {}
BLACKJACK_DUELS: dict[int, dict] = {}
BLACKJACK_DUEL_NEXT_ID = 1

# Дуэли в крестики-нолики: приглашения и активные игры до перезапуска бота.
TICTACTOE_INVITES: dict[int, dict] = {}
TICTACTOE_DUELS: dict[int, dict] = {}
TICTACTOE_NEXT_ID = 1

# Куда отправлять ответ админа: (admin_id, message_id_в_личке_админа) -> user_id
# Работает до перезапуска бота.
ADMIN_REPLY_TARGETS: dict[tuple[int, int], int] = {}

# Telegram может временно ограничить отправку в слишком активный чат.
# Держим лёгкую очередь по chat_id и повторяем запрос после RetryAfter.
CHAT_SEND_LOCKS: dict[int, asyncio.Lock] = {}
CHAT_NEXT_SEND_AT: dict[int, float] = {}
CHAT_SEND_DELAY_SECONDS = float(os.getenv("CHAT_SEND_DELAY_SECONDS", "0.35"))
MAX_RETRY_AFTER_SECONDS = int(os.getenv("MAX_RETRY_AFTER_SECONDS", "60"))


class AdminStates(StatesGroup):
    waiting_global_chance = State()
    waiting_player_lookup = State()
    waiting_set_balance = State()
    waiting_add_balance = State()
    waiting_player_chance = State()
    waiting_broadcast = State()


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def fmt_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return f"{minutes} мин. {sec:02d} сек."
    return f"{sec} сек."


def parse_percent(text: str) -> Optional[float]:
    raw = text.strip().replace("%", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if 0 <= value <= 100:
        return round(value, 2)
    return None


def parse_int(text: str) -> Optional[int]:
    try:
        return int(text.strip().replace(" ", ""))
    except ValueError:
        return None


def player_name(player: dict) -> str:
    username = player.get("username") or ""
    first_name = player.get("first_name") or "Игрок"
    if username:
        label = "@" + username
    else:
        label = first_name
    return escape(label)


def player_link(player: dict) -> str:
    label = player_name(player)
    return f'<a href="tg://user?id={int(player["user_id"])}">{label}</a>'


def admin_signature(user) -> str:
    if not user:
        return "👤 Админ: неизвестно"
    label = escape(user.full_name or str(user.id))
    username = f" @{escape(user.username)}" if user.username else ""
    return f'👤 Админ: <a href="tg://user?id={user.id}">{label}</a>{username}'


def chance_label(player: dict, global_chance: Optional[float] = None) -> str:
    personal = player.get("personal_chance")
    if personal is None:
        if global_chance is None:
            return "общий шанс"
        return f"общий {global_chance:g}%"
    return f"личный {float(personal):g}%"


async def ensure_player(message: Message, db: Database):
    if not message.from_user:
        return
    await db.register_or_update_player(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await db.touch_chat(chat_id=message.chat.id, user_id=message.from_user.id)


def admin_main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="🎯 Общий шанс", callback_data="admin:global_chance")
    kb.button(text="👤 Найти игрока", callback_data="admin:find_player")
    kb.button(text="🏆 Топ игроков", callback_data="admin:top")
    kb.button(text="📣 Бродкаст", callback_data="admin:broadcast")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def admin_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    return kb.as_markup()


async def safe_edit_text(message: Message, text: str, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return None
        raise


async def flood_retry_request_middleware(make_request, bot: Bot, method):
    chat_id = getattr(method, "chat_id", None)
    if chat_id is None:
        return await make_request(bot, method)

    try:
        chat_key = int(chat_id)
    except (TypeError, ValueError):
        return await make_request(bot, method)

    lock = CHAT_SEND_LOCKS.setdefault(chat_key, asyncio.Lock())
    async with lock:
        wait_for = CHAT_NEXT_SEND_AT.get(chat_key, 0) - time.monotonic()
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        try:
            result = await make_request(bot, method)
        except TelegramRetryAfter as exc:
            retry_after = int(getattr(exc, "retry_after", 1))
            if retry_after > MAX_RETRY_AFTER_SECONDS:
                raise
            await asyncio.sleep(retry_after + 1)
            result = await make_request(bot, method)

        CHAT_NEXT_SEND_AT[chat_key] = time.monotonic() + CHAT_SEND_DELAY_SECONDS
        return result


def player_admin_kb(player: dict) -> InlineKeyboardMarkup:
    uid = int(player["user_id"])
    banned = bool(player.get("is_banned"))
    muted = bool(player.get("is_muted"))
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Установить баланс", callback_data=f"admin:p:setbal:{uid}")
    kb.button(text="➕ Добавить/списать", callback_data=f"admin:p:addbal:{uid}")
    kb.button(text="🎯 Личный шанс", callback_data=f"admin:p:chance:{uid}")
    kb.button(text="♻️ Убрать личный шанс", callback_data=f"admin:p:chanceoff:{uid}")
    kb.button(text="🔓 Разбанить" if banned else "🔒 Забанить", callback_data=f"admin:p:ban:{uid}:{0 if banned else 1}")
    kb.button(text="🔈 Размутить ЛС" if muted else "🔇 Мут ЛС", callback_data=f"admin:p:mute:{uid}:{0 if muted else 1}")
    kb.button(text="⬅️ Меню", callback_data="admin:menu")
    kb.adjust(1, 1, 2, 1, 1, 1)
    return kb.as_markup()


async def player_card_text(db: Database, player: dict) -> str:
    global_chance = await db.get_global_chance()
    status = "забанен 🔒" if int(player.get("is_banned") or 0) else "активен ✅"
    mute_status = "мут ЛС 🔇" if int(player.get("is_muted") or 0) else "нет"
    username = "@" + escape(player["username"]) if player.get("username") else "—"
    return (
        "👤 <b>Игрок</b>\n"
        f"ID: <code>{int(player['user_id'])}</code>\n"
        f"Имя: {escape(player.get('first_name') or '—')}\n"
        f"Username: {username}\n"
        f"Баланс: <b>{int(player['balance'])}</b> 🪙\n"
        f"Шанс: <b>{chance_label(player, global_chance)}</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"Мут: <b>{mute_status}</b>"
    )


def help_text(is_private: bool = True) -> str:
    group_note = "\n\n👥 <b>В группе</b>: бот отвечает только на команды. /top показывает топ этой группы, /top global — общий топ."
    text = (
        "<b>казик</b>\n"
        "В боте нет реальных денег, депозитов и вывода — только игровые монеты.\n\n"
        "<b>Команды игрока</b>:\n"
        "/balance — баланс\n"
        "/bonus — получить +10 🪙 раз в 5 минут\n"
        "/freespin — бесплатный прокрут с бонусом раз в день\n"
        "/give @username 10 — передать монеты другому игроку\n"
        "/casino 10 — простая ставка с настраиваемым шансом\n"
        "/slots 10 — слоты\n"
        "/coin орел 10 — монетка, выигрыш x2\n"
        "/crash 10 2.0 — crash: ставка и цель, выплата по цели\n"
        "/blackjack 100 — Блек Джек\n"
        "/bj duel 100 — дуэль по Блек Джеку ответом на сообщение игрока\n"
        "/ttt 100 — дуэль в крестики-нолики ответом на сообщение игрока\n"
        "/roulette red 10 — рулетка: red/black или красное/черное\n"
        "/top — топ игроков\n"
        "/help — помощь\n\n"
    )
    if not is_private:
        text += group_note
    return text


def is_command_like_message(message: Message) -> bool:
    """True, если сообщение/подпись начинается с Telegram-команды."""
    text = message.text or message.caption or ""
    if text.strip().startswith("/"):
        return True

    for entity in (message.entities or []) + (message.caption_entities or []):
        entity_type = getattr(entity.type, "value", entity.type)
        if entity.offset == 0 and entity_type == "bot_command":
            return True

    return False


def user_info_text(message: Message) -> str:
    user = message.from_user
    chat = message.chat

    if user:
        username = f"@{escape(user.username)}" if user.username else "—"
        user_block = (
            f"👤 <b>Пользователь</b>: "
            f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>\n'
            f"ID: <code>{user.id}</code>\n"
            f"Username: {username}"
        )
    else:
        user_block = "👤 <b>Пользователь</b>: —"

    if chat.type == ChatType.PRIVATE:
        chat_block = "💬 Чат: личные сообщения"
    else:
        title = escape(chat.title or "Без названия")
        chat_block = f"💬 Чат: <b>{title}</b>\nChat ID: <code>{chat.id}</code>"

    return "📩 <b>Новое сообщение боту</b>\n" + user_block + "\n" + chat_block


def extract_user_id_from_admin_message(message: Optional[Message]) -> Optional[int]:
    if not message:
        return None
    text = message.text or message.caption or ""
    match = re.search(r"\bID:\s*(\d{5,20})\b", text)
    if match:
        return int(match.group(1))
    return None


async def resolve_admin_reply_target(message: Message, db: Database) -> Optional[int]:
    if not message.from_user or not message.reply_to_message:
        return None
    admin_id = message.from_user.id
    reply_id = message.reply_to_message.message_id
    target_user_id = ADMIN_REPLY_TARGETS.get((admin_id, reply_id))
    if not target_user_id:
        target_user_id = await db.get_admin_reply_target(admin_id, reply_id)
    if not target_user_id:
        target_user_id = await db.get_near_admin_reply_target(admin_id, reply_id)
    if not target_user_id:
        target_user_id = extract_user_id_from_admin_message(message.reply_to_message)
    return target_user_id


@router.message(Command("start"))
async def cmd_start(message: Message, db: Database):
    await ensure_player(message, db)
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        await message.reply(
            "🎰 Бот запущен в группе. Используйте /bonus, /freespin, /balance, /casino 10, /coin орел 10, /crash 10 2.0, /top.\n"
            "Подробности: /help"
        )
    else:
        await message.answer(help_text(is_private=True))


@router.message(Command("help"))
async def cmd_help(message: Message, db: Database):
    await ensure_player(message, db)
    await message.reply(help_text(is_private=message.chat.type == ChatType.PRIVATE))


@router.message(Command("balance"))
async def cmd_balance(message: Message, db: Database):
    await ensure_player(message, db)
    player = await db.get_player(message.from_user.id)
    await message.reply(f"💰 Ваш баланс: <b>{int(player['balance'])}</b> 🪙")


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)

    args = (command.args or "").split()

    # Основной формат: /give @username 10 или /give 123456789 10
    # Дополнительно: можно ответить на сообщение игрока и написать /give 10
    target_query = None
    amount = None

    if len(args) >= 2:
        target_query = args[0]
        amount = parse_int(args[1])
    elif len(args) == 1 and message.reply_to_message and message.reply_to_message.from_user:
        target_query = str(message.reply_to_message.from_user.id)
        amount = parse_int(args[0])
    else:
        await message.reply(
            "Пример: <code>/give @username 10</code>\n"
            "Также можно ответить на сообщение игрока: <code>/give 10</code>"
        )
        return

    if amount is None or amount <= 0:
        await message.reply("Сумма перевода должна быть целым числом больше 0.")
        return

    receiver = await db.find_player(target_query)
    if not receiver:
        await message.reply(
            "Игрок не найден. Он должен хотя бы раз написать команду боту.\n"
            "Пример: <code>/give @username 10</code>"
        )
        return

    result = await db.transfer_balance(
        from_user_id=message.from_user.id,
        to_user_id=int(receiver["user_id"]),
        amount=amount,
    )

    if result.get("ok"):
        await message.reply(
            f"✅ Вы передали {player_link(receiver)} <b>{amount}</b> 🪙\n"
            f"Ваш баланс: <b>{int(result['sender_balance'])}</b> 🪙",
            disable_web_page_preview=True,
        )
        return

    error = result.get("error")
    if error == "self_transfer":
        await message.reply("Нельзя переводить монеты самому себе.")
    elif error == "not_enough":
        await message.reply(f"Недостаточно монет. Ваш баланс: <b>{int(result.get('balance', 0))}</b> 🪙")
    elif error == "sender_banned":
        await message.reply("🔒 Вы заблокированы в боте.")
    elif error == "receiver_banned":
        await message.reply("🔒 Получатель заблокирован в боте.")
    else:
        await message.reply("⚠️ Не удалось выполнить перевод.")


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, db: Database):
    await ensure_player(message, db)
    result = await db.claim_bonus(message.from_user.id, BONUS_AMOUNT, BONUS_COOLDOWN_SECONDS)
    if result.get("ok"):
        await message.reply(f"✅ Получено <b>+{BONUS_AMOUNT}</b> 🪙\nБаланс: <b>{result['balance']}</b> 🪙")
        return
    if result.get("error") == "cooldown":
        await message.reply(f"⏳ Бонус уже брали. Следующий через <b>{fmt_seconds(result['remaining'])}</b>.")
    elif result.get("error") == "banned":
        await message.reply("🔒 Вы заблокированы в боте.")
    else:
        await message.reply("⚠️ Не удалось выдать бонус. Попробуйте позже.")


FREE_SPIN_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎", "7️⃣"]
FREE_SPIN_REWARDS = [
    {"amount": 5, "label": "малый бонус", "weight": 34, "line": ["🍒", "🍋", "🍇"]},
    {"amount": 10, "label": "обычный бонус", "weight": 28, "line": ["🍒", "🍒", "🍋"]},
    {"amount": 25, "label": "хороший бонус", "weight": 20, "line": ["🔔", "🔔", "⭐"]},
    {"amount": 50, "label": "крупный бонус", "weight": 12, "line": ["⭐", "⭐", "⭐"]},
    {"amount": 100, "label": "супер бонус", "weight": 5, "line": ["💎", "💎", "💎"]},
    {"amount": 250, "label": "джекпот", "weight": 1, "line": ["7️⃣", "7️⃣", "7️⃣"]},
]


def pick_free_spin_reward() -> dict:
    return rng.choices(FREE_SPIN_REWARDS, weights=[item["weight"] for item in FREE_SPIN_REWARDS], k=1)[0]


def free_spin_line_text(line: list[str]) -> str:
    return " | ".join(line)


async def animate_free_spin(message: Message, final_line: list[str]):
    spin_message = await message.reply("🎰 <b>Free Spin</b>\n[ ❔ | ❔ | ❔ ]\nКрутим...")
    frames = 8
    for i in range(frames):
        if i == frames - 1:
            line = final_line
        else:
            line = [rng.choice(FREE_SPIN_SYMBOLS) for _ in range(3)]
        text = f"🎰 <b>Free Spin</b>\n[ {free_spin_line_text(line)} ]\nКрутим{'.' * ((i % 3) + 1)}"
        try:
            await spin_message.edit_text(text)
        except Exception:
            pass
        await asyncio.sleep(0.35)
    return spin_message


@router.message(Command("freespin", "spin"))
async def cmd_free_spin(message: Message, db: Database):
    await ensure_player(message, db)
    reward = pick_free_spin_reward()
    line = list(reward["line"])

    result = await db.claim_daily_reward(
        user_id=message.from_user.id,
        reward_key="free_spin",
        amount=int(reward["amount"]),
        cooldown_seconds=FREE_SPIN_COOLDOWN_SECONDS,
        meta=f"label={reward['label']};line={''.join(line)}",
    )
    if not result.get("ok"):
        if result.get("error") == "cooldown":
            await message.reply(
                f"⏳ Free Spin уже использован сегодня.\n"
                f"Следующий прокрут через <b>{fmt_seconds(result['remaining'])}</b>.\n"
                f"Баланс: <b>{int(result.get('balance', 0))}</b> 🪙"
            )
        elif result.get("error") == "banned":
            await message.reply("🔒 Вы заблокированы в боте.")
        else:
            await message.reply("⚠️ Не удалось запустить Free Spin. Попробуйте позже.")
        return

    spin_message = await animate_free_spin(message, line)
    jackpot_line = "\n🔥 <b>ДЖЕКПОТ!</b>" if int(reward["amount"]) >= 250 else ""
    await spin_message.edit_text(
        f"🎰 <b>Free Spin</b>\n"
        f"[ {free_spin_line_text(line)} ]{jackpot_line}\n"
        f"✅ Выпал {reward['label']}: <b>+{int(reward['amount'])}</b> 🪙\n"
        f"Баланс: <b>{result['balance']}</b> 🪙\n"
        f"Следующий бесплатный прокрут — через <b>{fmt_seconds(FREE_SPIN_COOLDOWN_SECONDS)}</b>."
    )


async def get_bet_from_command(message: Message, command: CommandObject) -> Optional[int]:
    if not command.args:
        await message.reply("Укажите ставку. Пример: <code>/casino 10</code>")
        return None
    bet = parse_int(command.args.split()[0])
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0.")
        return None
    return bet


async def send_bet_error(message: Message, result: dict):
    error = result.get("error")
    if error == "not_enough":
        await message.reply(f"Недостаточно монет. Баланс: <b>{int(result.get('balance', 0))}</b> 🪙")
    elif error == "banned":
        await message.reply("🔒 Вы заблокированы в боте.")
    else:
        await message.reply("⚠️ Ставка не принята. Проверьте баланс и попробуйте снова.")


@router.message(Command("casino"))
async def cmd_casino(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    bet = await get_bet_from_command(message, command)
    if bet is None:
        return

    chance = await db.get_effective_chance(message.from_user.id)
    win = rng.uniform(0, 100) < chance
    payout = bet * 2 if win else 0
    result = await db.apply_bet_result(
        user_id=message.from_user.id,
        bet=bet,
        payout=payout,
        game="casino",
        meta=f"chance={chance};win={win}",
    )
    if not result.get("ok"):
        await send_bet_error(message, result)
        return

    if win:
        await message.reply(
            f"🎉 Победа! Ставка <b>{bet}</b>, выигрыш <b>{payout}</b> 🪙\n"
            f"Профит: <b>+{result['profit']}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )
    else:
        await message.reply(f"😔 Проигрыш. Потеряно <b>{bet}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙")


@router.message(Command("dice", "keno"))
async def disabled_removed_games(message: Message):
    return


COIN_ALIASES = {
    "heads": "heads",
    "head": "heads",
    "h": "heads",
    "орел": "heads",
    "орёл": "heads",
    "orel": "heads",
    "о": "heads",
    "tails": "tails",
    "tail": "tails",
    "t": "tails",
    "решка": "tails",
    "reshka": "tails",
    "р": "tails",
}


@router.message(Command("coin", "flip"))
async def cmd_coin(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.reply("🪙 Укажите сторону и ставку. Пример: <code>/coin орел 10</code> или <code>/coin tails 10</code>")
        return

    side = None
    bet = None
    for part in parts[:2]:
        normalized = part.lower()
        if normalized in COIN_ALIASES:
            side = COIN_ALIASES[normalized]
        else:
            maybe_bet = parse_int(part)
            if maybe_bet is not None:
                bet = maybe_bet

    if side is None:
        await message.reply("Сторона должна быть <b>орел</b>/<b>решка</b> или <b>heads</b>/<b>tails</b>.")
        return
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0. Пример: <code>/coin орел 10</code>")
        return

    chance = await db.get_effective_chance(message.from_user.id)
    win = rng.uniform(0, 100) < chance
    landed = side if win else ("tails" if side == "heads" else "heads")
    payout = bet * 2 if win else 0

    result = await db.apply_bet_result(
        user_id=message.from_user.id,
        bet=bet,
        payout=payout,
        game="coin",
        meta=f"chance={chance};side={side};landed={landed};win={win}",
    )
    if not result.get("ok"):
        await send_bet_error(message, result)
        return

    side_ru = {"heads": "орел", "tails": "решка"}
    if win:
        await message.reply(
            f"🪙 Выпало: <b>{side_ru[landed]}</b>\n"
            f"✅ Победа! Множитель <b>x2</b>\n"
            f"Профит: <b>+{result['profit']}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )
    else:
        await message.reply(
            f"🪙 Выпало: <b>{side_ru[landed]}</b>\n"
            f"❌ Проигрыш: <b>-{bet}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )


@router.message(Command("crash"))
async def cmd_crash(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.reply(
            "🚀 Укажите ставку и цель от 1.2 до 5.0.\n"
            "Пример: <code>/crash 10 2.0</code>\n"
            "Если ракета долетит до цели — выплата по выбранному множителю."
        )
        return

    bet = parse_int(parts[0])
    try:
        target = float(parts[1].replace(",", "."))
    except ValueError:
        target = 0

    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0.")
        return
    if target < 1.2 or target > 5:
        await message.reply("Цель должна быть числом от <b>1.2</b> до <b>5.0</b>. Пример: <code>/crash 10 2.0</code>")
        return

    target = round(target, 2)
    chance = await db.get_effective_chance(message.from_user.id)
    win = rng.uniform(0, 100) < chance
    if win:
        crash_at = round(rng.uniform(target, max(target + 0.05, 5.25)), 2)
    else:
        crash_at = round(rng.uniform(1.0, max(1.01, target - 0.01)), 2)

    payout = int(bet * target) if win else 0
    result = await db.apply_bet_result(
        user_id=message.from_user.id,
        bet=bet,
        payout=payout,
        game="crash",
        meta=f"chance={chance};target={target};crash_at={crash_at};win={win}",
    )
    if not result.get("ok"):
        await send_bet_error(message, result)
        return

    if win:
        await message.reply(
            f"🚀 Crash остановился на <b>x{crash_at:g}</b>\n"
            f"✅ Вы успели забрать на <b>x{target:g}</b>\n"
            f"Выигрыш: <b>{payout}</b> 🪙\n"
            f"Профит: <b>+{result['profit']}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )
    else:
        await message.reply(
            f"💥 Crash остановился на <b>x{crash_at:g}</b>, цель была <b>x{target:g}</b>\n"
            f"❌ Проигрыш: <b>-{bet}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )


BJ_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BJ_SUITS = ["♠️", "♥️", "♦️", "♣️"]


def bj_new_deck() -> list[tuple[str, str]]:
    deck = [(rank, suit) for suit in BJ_SUITS for rank in BJ_RANKS]
    rng.shuffle(deck)
    return deck


def bj_card_label(card: tuple[str, str]) -> str:
    rank, suit = card
    return f"{rank}{suit}"


def bj_draw(game: dict) -> tuple[str, str]:
    if not game.get("deck"):
        game["deck"] = bj_new_deck()
    return game["deck"].pop()


def bj_take_deck_card(game: dict, index: int) -> tuple[str, str]:
    if not game.get("deck"):
        game["deck"] = bj_new_deck()
    return game["deck"].pop(index)


def bj_draw_for_chance(game: dict, cards: list[tuple[str, str]], chance: float) -> tuple[str, str]:
    """
    Подкрученная доборная карта для Блек Джека.
    При удачном ролле выбирает карту, которая не делает перебор и приближает к 21.
    При неудачном ролле, если возможно, выбирает карту, которая даёт перебор.
    """
    if not game.get("deck"):
        game["deck"] = bj_new_deck()

    lucky = rng.uniform(0, 100) < float(chance)
    deck = game["deck"]

    if lucky:
        safe: list[tuple[int, int]] = []
        for idx, card in enumerate(deck):
            score = bj_hand_score(cards + [card])
            if score <= 21:
                safe.append((idx, score))
        if safe:
            best_score = max(score for _idx, score in safe)
            best_indexes = [idx for idx, score in safe if score == best_score]
            return bj_take_deck_card(game, rng.choice(best_indexes))
    else:
        bust_indexes = []
        for idx, card in enumerate(deck):
            if bj_hand_score(cards + [card]) > 21:
                bust_indexes.append(idx)
        if bust_indexes:
            return bj_take_deck_card(game, rng.choice(bust_indexes))

    return bj_draw(game)


def bj_synthetic_card(rank: str, n: int = 0) -> tuple[str, str]:
    return (rank, BJ_SUITS[n % len(BJ_SUITS)])


def bj_find_hand_with_score(first_card: tuple[str, str], target_score: int) -> Optional[list[tuple[str, str]]]:
    """Подбирает карты дилеру с сохранением первой открытой карты."""
    for extra_count in range(1, 5):
        for ranks in product(BJ_RANKS, repeat=extra_count):
            cards = [first_card] + [bj_synthetic_card(rank, i) for i, rank in enumerate(ranks)]
            if bj_hand_score(cards) == target_score:
                return cards
    return None


def bj_force_dealer_result(game: dict, want_player_win: bool):
    """
    Подкрутка финала обычного Блек Джека через скрытые карты дилера.
    Первая открытая карта дилера сохраняется, чтобы сообщение выглядело логично.
    """
    player_score = bj_hand_score(game["player"])
    if player_score > 21 or not game.get("dealer"):
        return

    first = game["dealer"][0]

    if want_player_win:
        # Делаем дилеру перебор. Первая карта остаётся той же, которую игрок видел.
        cards = [first, bj_synthetic_card("K", 0), bj_synthetic_card("Q", 1), bj_synthetic_card("J", 2)]
        if bj_hand_score(cards) <= 21:
            cards.append(bj_synthetic_card("10", 3))
        game["dealer"] = cards
        return

    # Делаем дилеру сумму больше игрока. Если у игрока 21 — максимум ничья 21.
    min_target = 21 if player_score >= 21 else max(17, player_score + 1)
    for target in range(21, min_target - 1, -1):
        hand = bj_find_hand_with_score(first, target)
        if hand:
            game["dealer"] = hand
            return


def bj_hand_score(cards: list[tuple[str, str]]) -> int:
    total = 0
    aces = 0
    for rank, _suit in cards:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            total += 10
        else:
            total += int(rank)

    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def bj_is_blackjack(cards: list[tuple[str, str]]) -> bool:
    return len(cards) == 2 and bj_hand_score(cards) == 21


def bj_cards_text(cards: list[tuple[str, str]]) -> str:
    return " ".join(bj_card_label(card) for card in cards)


def bj_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🃏 Ещё", callback_data="bj:hit")
    kb.button(text="🛑 Стоп", callback_data="bj:stand")
    kb.adjust(2)
    return kb.as_markup()


def bj_game_text(game: dict, hide_dealer: bool = True) -> str:
    player_cards = game["player"]
    dealer_cards = game["dealer"]
    player_score = bj_hand_score(player_cards)

    if hide_dealer:
        dealer_text = f"{bj_card_label(dealer_cards[0])} 🂠"
        dealer_score = bj_hand_score([dealer_cards[0]])
        dealer_line = f"Карты дилера: <b>{dealer_text}</b> / видно: <b>{dealer_score}</b>"
    else:
        dealer_score = bj_hand_score(dealer_cards)
        dealer_line = f"Карты дилера: <b>{bj_cards_text(dealer_cards)}</b> / сумма: <b>{dealer_score}</b>"

    return (
        "🃏 <b>Блек Джек</b>\n"
        f"Ставка: <b>{int(game['bet'])}</b> 🪙\n\n"
        f"Ваши карты: <b>{bj_cards_text(player_cards)}</b> / сумма: <b>{player_score}</b>\n"
        f"{dealer_line}"
    )


async def bj_finish_game(db: Database, game: dict, result_text: str, payout: int) -> tuple[str, int]:
    """Начисляет выплату и возвращает финальный текст + новый баланс."""
    if payout > 0:
        await db.add_balance(game["user_id"], payout, reason="game:blackjack_payout")

    player = await db.get_player(game["user_id"])
    new_balance = int(player["balance"]) if player else 0
    profit = int(payout) - int(game["bet"])
    sign = "+" if profit > 0 else ""

    text = (
        bj_game_text(game, hide_dealer=False)
        + "\n\n"
        + result_text
        + f"\nВыплата: <b>{int(payout)}</b> 🪙"
        + f"\nИтог: <b>{sign}{profit}</b> 🪙"
        + f"\nБаланс: <b>{new_balance}</b> 🪙"
    )
    return text, new_balance


async def bj_resolve_dealer(db: Database, game: dict) -> tuple[str, int]:
    player_score = bj_hand_score(game["player"])
    bet = int(game["bet"])

    if player_score > 21:
        return await bj_finish_game(db, game, "💥 Перебор! Вы проиграли.", 0)

    # Админская подкрутка шансов работает на Блек Джек:
    # личный шанс игрока перекрывает общий шанс.
    chance = float(game.get("chance") if game.get("chance") is not None else await db.get_effective_chance(game["user_id"]))
    game["chance"] = chance
    want_player_win = rng.uniform(0, 100) < chance
    bj_force_dealer_result(game, want_player_win=want_player_win)

    while bj_hand_score(game["dealer"]) < 17:
        # Если шанс игрока высокий, дилер чаще добирает плохую карту;
        # если шанс низкий — дилер добирает безопаснее.
        dealer_chance = max(0.0, min(100.0, 100.0 - chance))
        game["dealer"].append(bj_draw_for_chance(game, game["dealer"], dealer_chance))

    dealer_score = bj_hand_score(game["dealer"])

    if dealer_score > 21:
        return await bj_finish_game(db, game, "✅ Дилер перебрал. Вы победили!", bet * 2)
    if player_score > dealer_score:
        return await bj_finish_game(db, game, "✅ Победа! Ваши карты сильнее.", bet * 2)
    if player_score == dealer_score:
        return await bj_finish_game(db, game, "🤝 Ничья. Ставка возвращена.", bet)
    return await bj_finish_game(db, game, "❌ Проигрыш. Дилер сильнее.", 0)


def next_blackjack_duel_id() -> int:
    global BLACKJACK_DUEL_NEXT_ID
    duel_id = BLACKJACK_DUEL_NEXT_ID
    BLACKJACK_DUEL_NEXT_ID += 1
    return duel_id


def mention_user(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{escape(name or "Игрок")}</a>'


def next_tictactoe_duel_id() -> int:
    global TICTACTOE_NEXT_ID
    duel_id = TICTACTOE_NEXT_ID
    TICTACTOE_NEXT_ID += 1
    return duel_id


def tictactoe_invite_kb(duel_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"ttt:accept:{duel_id}")
    kb.button(text="❌ Отклонить", callback_data=f"ttt:decline:{duel_id}")
    kb.button(text="🚫 Отменить вызов", callback_data=f"ttt:cancel:{duel_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def tictactoe_game_kb(game: dict) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    board = game["board"]
    for idx, value in enumerate(board):
        kb.button(text=value or "·", callback_data=f"ttt:move:{game['id']}:{idx}")
    kb.adjust(3, 3, 3)
    return kb.as_markup()


def tictactoe_winner_symbol(board: list[str]) -> Optional[str]:
    lines = [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def tictactoe_board_text(board: list[str]) -> str:
    cells = [value or "·" for value in board]
    rows = [" ".join(cells[i:i + 3]) for i in range(0, 9, 3)]
    return "\n".join(rows)


def tictactoe_text(game: dict) -> str:
    bet = int(game["bet"])
    bank = bet * 2
    uid1, uid2 = game["order"]
    p1 = game["players"][uid1]
    p2 = game["players"][uid2]
    current = game.get("current")
    current_line = f"\nСейчас ходит: {game['players'][current]['name']} ({game['players'][current]['symbol']})" if current else ""
    return (
        "❌⭕ <b>Дуэль в крестики-нолики</b>\n"
        f"Ставка каждого: <b>{bet}</b> 🪙\n"
        f"Банк: <b>{bank}</b> 🪙\n\n"
        f"{p1['symbol']} — {p1['name']}\n"
        f"{p2['symbol']} — {p2['name']}\n\n"
        f"<code>{tictactoe_board_text(game['board'])}</code>"
        f"{current_line}"
    )


def is_user_in_tictactoe_duel(chat_id: int, user_id: int) -> bool:
    for invite in TICTACTOE_INVITES.values():
        if int(invite["chat_id"]) == int(chat_id) and int(user_id) in {int(invite["from_user_id"]), int(invite["to_user_id"])}:
            return True

    for game in TICTACTOE_DUELS.values():
        if int(game["chat_id"]) == int(chat_id) and int(user_id) in set(game["order"]):
            return True

    return False


async def finish_tictactoe_duel(db: Database, game: dict, winner_id: Optional[int]) -> str:
    uid1, uid2 = game["order"]
    p1 = game["players"][uid1]
    p2 = game["players"][uid2]
    bet = int(game["bet"])
    bank = bet * 2

    if winner_id is None:
        await db.add_balance(uid1, bet, reason="game:tictactoe_duel_refund", meta=f"duel_id={game['id']}")
        await db.add_balance(uid2, bet, reason="game:tictactoe_duel_refund", meta=f"duel_id={game['id']}")
        result_line = "🤝 Ничья. Ставки возвращены."
        payout_line = f"Возврат: каждому по <b>{bet}</b> 🪙"
    else:
        winner = game["players"][winner_id]
        await db.add_balance(winner_id, bank, reason="game:tictactoe_duel_win", meta=f"duel_id={game['id']}")
        result_line = f"🏆 Победил {winner['name']} ({winner['symbol']})."
        payout_line = f"Выплата победителю: <b>{bank}</b> 🪙"

    b1 = await db.get_player(uid1)
    b2 = await db.get_player(uid2)
    balance_line = (
        f"Баланс {p1['name']}: <b>{int(b1['balance']) if b1 else 0}</b> 🪙\n"
        f"Баланс {p2['name']}: <b>{int(b2['balance']) if b2 else 0}</b> 🪙"
    )
    game["current"] = None
    return tictactoe_text(game) + "\n\n" + result_line + "\n" + payout_line + "\n" + balance_line


async def start_tictactoe_duel(message: Message, command: CommandObject, db: Database):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply(
            "Чтобы вызвать игрока на дуэль в крестики-нолики, ответьте на его сообщение командой:\n"
            "<code>/ttt 100</code>"
        )
        return

    if not command.args:
        await message.reply("Укажите ставку. Пример: <code>/ttt 100</code>")
        return
    bet = parse_int(command.args.split()[0])
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0. Пример: <code>/ttt 100</code>")
        return

    challenger = message.from_user
    opponent = message.reply_to_message.from_user
    if not challenger or not opponent:
        await message.reply("Не удалось определить игроков.")
        return
    if opponent.is_bot:
        await message.reply("Нельзя вызвать бота на дуэль.")
        return
    if challenger.id == opponent.id:
        await message.reply("Нельзя вызвать на дуэль самого себя.")
        return

    await db.register_or_update_player(opponent.id, opponent.username, opponent.first_name)
    await db.touch_chat(message.chat.id, opponent.id)

    if is_user_in_tictactoe_duel(message.chat.id, challenger.id):
        await message.reply("У вас уже есть активная игра/приглашение в крестики-нолики в этом чате.")
        return
    if is_user_in_tictactoe_duel(message.chat.id, opponent.id):
        await message.reply("У второго игрока уже есть активная игра/приглашение в крестики-нолики в этом чате.")
        return

    challenger_player = await db.get_player(challenger.id)
    opponent_player = await db.get_player(opponent.id)
    challenger_balance = int(challenger_player["balance"]) if challenger_player else 0
    opponent_balance = int(opponent_player["balance"]) if opponent_player else 0

    if challenger_balance < bet:
        await message.reply(f"У вас недостаточно монет. Нужно <b>{bet}</b> 🪙, баланс: <b>{challenger_balance}</b> 🪙")
        return
    if opponent_balance < bet:
        await message.reply(
            f"У игрока {mention_user(opponent.id, opponent.full_name)} недостаточно монет. "
            f"Нужно <b>{bet}</b> 🪙, баланс: <b>{opponent_balance}</b> 🪙",
            disable_web_page_preview=True,
        )
        return

    duel_id = next_tictactoe_duel_id()
    TICTACTOE_INVITES[duel_id] = {
        "id": duel_id,
        "chat_id": message.chat.id,
        "from_user_id": challenger.id,
        "to_user_id": opponent.id,
        "bet": bet,
        "from_name": mention_user(challenger.id, challenger.full_name),
        "to_name": mention_user(opponent.id, opponent.full_name),
    }

    await message.reply(
        "❌⭕ <b>Вызов на дуэль в крестики-нолики</b>\n"
        f"Игрок {mention_user(challenger.id, challenger.full_name)} вызывает {mention_user(opponent.id, opponent.full_name)}.\n"
        f"Ставка каждого: <b>{bet}</b> 🪙\n"
        f"Банк: <b>{bet * 2}</b> 🪙\n\n"
        f"{mention_user(opponent.id, opponent.full_name)}, примите или отклоните вызов.",
        reply_markup=tictactoe_invite_kb(duel_id),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("ttt:"))
async def tictactoe_callbacks(callback: CallbackQuery, db: Database):
    if not callback.data or not callback.message:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    action = parts[1]
    try:
        duel_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректная дуэль", show_alert=True)
        return

    if action in {"accept", "decline", "cancel"}:
        invite = TICTACTOE_INVITES.get(duel_id)
        if not invite:
            await callback.answer("Приглашение уже не активно", show_alert=True)
            return

        if action == "cancel":
            if callback.from_user.id != int(invite["from_user_id"]):
                await callback.answer("Отменить вызов может только тот, кто его отправил", show_alert=True)
                return

            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(
                callback.message,
                "🚫 Вызов на дуэль отменён.\n"
                f"{invite['from_name']} vs {invite['to_name']}\n"
                f"Ставка: <b>{int(invite['bet'])}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer("Вызов отменён")
            return

        if callback.from_user.id != int(invite["to_user_id"]):
            await callback.answer("Принять или отклонить может только вызванный игрок", show_alert=True)
            return

        if action == "decline":
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(
                callback.message,
                "❌ Дуэль отклонена.\n"
                f"{invite['from_name']} vs {invite['to_name']}\n"
                f"Ставка: <b>{int(invite['bet'])}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer("Отклонено")
            return

        chat_id = int(invite["chat_id"])
        uid1 = int(invite["from_user_id"])
        uid2 = int(invite["to_user_id"])
        bet = int(invite["bet"])

        busy_reason = None
        for active_game in TICTACTOE_DUELS.values():
            if int(active_game["chat_id"]) == chat_id and (uid1 in active_game["order"] or uid2 in active_game["order"]):
                busy_reason = "один из игроков уже участвует в другой дуэли"
                break
        for other_id, other_invite in TICTACTOE_INVITES.items():
            if int(other_id) == duel_id:
                continue
            if int(other_invite["chat_id"]) == chat_id and (uid1 in {int(other_invite["from_user_id"]), int(other_invite["to_user_id"])} or uid2 in {int(other_invite["from_user_id"]), int(other_invite["to_user_id"])}):
                busy_reason = "у одного из игроков есть другое активное приглашение"
                break
        if busy_reason:
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(callback.message, f"⚠️ Дуэль отменена: {busy_reason}.", disable_web_page_preview=True)
            await callback.answer()
            return

        p1 = await db.get_player(uid1)
        p2 = await db.get_player(uid2)
        b1 = int(p1["balance"]) if p1 else 0
        b2 = int(p2["balance"]) if p2 else 0
        if b1 < bet:
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(callback.message, f"⚠️ Дуэль отменена: у {invite['from_name']} недостаточно монет. Баланс: <b>{b1}</b> 🪙", disable_web_page_preview=True)
            await callback.answer()
            return
        if b2 < bet:
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(callback.message, f"⚠️ Дуэль отменена: у {invite['to_name']} недостаточно монет. Баланс: <b>{b2}</b> 🪙", disable_web_page_preview=True)
            await callback.answer()
            return

        w1 = await db.withdraw_balance(uid1, bet, reason="game:tictactoe_duel_bet", meta=f"duel_id={duel_id}")
        if not w1.get("ok"):
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(callback.message, f"⚠️ Дуэль отменена: у {invite['from_name']} не удалось списать ставку.", disable_web_page_preview=True)
            await callback.answer()
            return
        w2 = await db.withdraw_balance(uid2, bet, reason="game:tictactoe_duel_bet", meta=f"duel_id={duel_id}")
        if not w2.get("ok"):
            await db.add_balance(uid1, bet, reason="game:tictactoe_duel_refund", meta=f"duel_id={duel_id}")
            TICTACTOE_INVITES.pop(duel_id, None)
            await safe_edit_text(callback.message, f"⚠️ Дуэль отменена: у {invite['to_name']} не удалось списать ставку.", disable_web_page_preview=True)
            await callback.answer()
            return

        TICTACTOE_INVITES.pop(duel_id, None)
        game = {
            "id": duel_id,
            "chat_id": chat_id,
            "bet": bet,
            "board": [""] * 9,
            "order": [uid1, uid2],
            "players": {
                uid1: {"name": invite["from_name"], "symbol": "❌"},
                uid2: {"name": invite["to_name"], "symbol": "⭕"},
            },
            "current": uid1,
        }
        TICTACTOE_DUELS[duel_id] = game
        await safe_edit_text(
            callback.message,
            tictactoe_text(game) + "\n\nНажимать клетку может только игрок, чей сейчас ход.",
            reply_markup=tictactoe_game_kb(game),
            disable_web_page_preview=True,
        )
        await callback.answer("Дуэль началась")
        return

    if action == "move":
        if len(parts) != 4:
            await callback.answer("Некорректный ход", show_alert=True)
            return
        try:
            cell = int(parts[3])
        except ValueError:
            await callback.answer("Некорректная клетка", show_alert=True)
            return

        game = TICTACTOE_DUELS.get(duel_id)
        if not game:
            await callback.answer("Игра уже завершена", show_alert=True)
            return
        if callback.from_user.id != int(game.get("current")):
            await callback.answer("Сейчас ход другого игрока", show_alert=True)
            return
        if cell < 0 or cell > 8 or game["board"][cell]:
            await callback.answer("Эта клетка уже занята", show_alert=True)
            return

        uid = int(callback.from_user.id)
        game["board"][cell] = game["players"][uid]["symbol"]
        winner_symbol = tictactoe_winner_symbol(game["board"])

        if winner_symbol:
            winner_id = next(player_id for player_id in game["order"] if game["players"][player_id]["symbol"] == winner_symbol)
            TICTACTOE_DUELS.pop(duel_id, None)
            text = await finish_tictactoe_duel(db, game, winner_id)
            await safe_edit_text(callback.message, text, disable_web_page_preview=True)
            await callback.answer("Победа")
            return

        if all(game["board"]):
            TICTACTOE_DUELS.pop(duel_id, None)
            text = await finish_tictactoe_duel(db, game, None)
            await safe_edit_text(callback.message, text, disable_web_page_preview=True)
            await callback.answer("Ничья")
            return

        uid1, uid2 = game["order"]
        game["current"] = uid2 if uid == uid1 else uid1
        await safe_edit_text(
            callback.message,
            tictactoe_text(game) + "\n\nНажимать клетку может только игрок, чей сейчас ход.",
            reply_markup=tictactoe_game_kb(game),
            disable_web_page_preview=True,
        )
        await callback.answer("Ход принят")
        return

    await callback.answer("Неизвестное действие", show_alert=True)


def blackjack_duel_invite_kb(duel_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"bjd:accept:{duel_id}")
    kb.button(text="❌ Отклонить", callback_data=f"bjd:decline:{duel_id}")
    kb.button(text="🚫 Отменить вызов", callback_data=f"bjd:cancel:{duel_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def blackjack_duel_game_kb(duel_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🃏 Ещё", callback_data=f"bjd:hit:{duel_id}")
    kb.button(text="🛑 Стоп", callback_data=f"bjd:stand:{duel_id}")
    kb.adjust(2)
    return kb.as_markup()


def is_user_in_blackjack_duel(chat_id: int, user_id: int) -> bool:
    if (chat_id, user_id) in BLACKJACK_GAMES:
        return True

    for invite in BLACKJACK_DUEL_INVITES.values():
        if int(invite["chat_id"]) == int(chat_id) and int(user_id) in {int(invite["from_user_id"]), int(invite["to_user_id"])}:
            return True

    for game in BLACKJACK_DUELS.values():
        if int(game["chat_id"]) == int(chat_id) and int(user_id) in set(game["order"]):
            return True

    return False


def blackjack_duel_next_turn(game: dict) -> Optional[int]:
    for uid in game["order"]:
        if game["players"][uid]["status"] == "playing":
            return uid
    return None


def blackjack_duel_text(game: dict) -> str:
    bet = int(game["bet"])
    bank = bet * 2
    lines = [
        "🃏 <b>Дуэль по Блек Джеку</b>",
        f"Ставка каждого: <b>{bet}</b> 🪙",
        f"Банк: <b>{bank}</b> 🪙",
        "",
    ]

    for uid in game["order"]:
        p = game["players"][uid]
        score = bj_hand_score(p["cards"])
        status = p["status"]
        if status == "playing" and uid == game.get("current"):
            status_text = "🎮 ходит"
        elif status == "playing":
            status_text = "⏳ ждёт"
        elif status == "stand":
            status_text = "🛑 стоп"
        elif status == "bust":
            status_text = "💥 перебор"
        else:
            status_text = status
        lines.append(f"{p['name']}: <b>{bj_cards_text(p['cards'])}</b> / сумма: <b>{score}</b> — {status_text}")

    current = game.get("current")
    if current:
        lines.append("")
        lines.append(f"Сейчас ходит: {game['players'][current]['name']}")

    return "\n".join(lines)


async def finish_blackjack_duel(db: Database, game: dict) -> str:
    uid1, uid2 = game["order"]
    p1 = game["players"][uid1]
    p2 = game["players"][uid2]
    bet = int(game["bet"])
    bank = bet * 2

    s1 = bj_hand_score(p1["cards"])
    s2 = bj_hand_score(p2["cards"])
    bust1 = s1 > 21 or p1["status"] == "bust"
    bust2 = s2 > 21 or p2["status"] == "bust"

    winner_id = None
    result_line = ""

    if bust1 and bust2:
        result_line = "🤝 Оба перебрали. Ставки возвращены."
    elif bust1:
        winner_id = uid2
        result_line = f"🏆 Победил {p2['name']} — соперник перебрал."
    elif bust2:
        winner_id = uid1
        result_line = f"🏆 Победил {p1['name']} — соперник перебрал."
    elif s1 > s2:
        winner_id = uid1
        result_line = f"🏆 Победил {p1['name']} со счётом <b>{s1}</b> против <b>{s2}</b>."
    elif s2 > s1:
        winner_id = uid2
        result_line = f"🏆 Победил {p2['name']} со счётом <b>{s2}</b> против <b>{s1}</b>."
    else:
        result_line = f"🤝 Ничья: у обоих <b>{s1}</b>. Ставки возвращены."

    if winner_id is None:
        await db.add_balance(uid1, bet, reason="game:blackjack_duel_refund",)
        await db.add_balance(uid2, bet, reason="game:blackjack_duel_refund",)
        payout_line = f"Возврат: каждому по <b>{bet}</b> 🪙"
    else:
        await db.add_balance(winner_id, bank, reason="game:blackjack_duel_win", meta=f"duel_id={game['id']}")
        payout_line = f"Выплата победителю: <b>{bank}</b> 🪙"

    b1 = await db.get_player(uid1)
    b2 = await db.get_player(uid2)
    balance_line = (
        f"Баланс {p1['name']}: <b>{int(b1['balance']) if b1 else 0}</b> 🪙\n"
        f"Баланс {p2['name']}: <b>{int(b2['balance']) if b2 else 0}</b> 🪙"
    )

    return blackjack_duel_text(game) + "\n\n" + result_line + "\n" + payout_line + "\n" + balance_line


async def start_blackjack_duel(message: Message, args: list[str], db: Database):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply(
            "Чтобы вызвать игрока на дуэль, ответьте на его сообщение командой:\n"
            "<code>/bj duel 100</code>"
        )
        return

    if not args:
        await message.reply("Укажите ставку. Пример: <code>/bj duel 100</code>")
        return

    bet = parse_int(args[0])
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0. Пример: <code>/bj duel 100</code>")
        return

    challenger = message.from_user
    opponent = message.reply_to_message.from_user
    if not challenger or not opponent:
        await message.reply("Не удалось определить игроков.")
        return
    if opponent.is_bot:
        await message.reply("Нельзя вызвать бота на дуэль.")
        return
    if challenger.id == opponent.id:
        await message.reply("Нельзя вызвать на дуэль самого себя.")
        return

    # Регистрируем второго игрока, даже если он раньше не писал команды.
    await db.register_or_update_player(opponent.id, opponent.username, opponent.first_name)
    await db.touch_chat(message.chat.id, opponent.id)

    if is_user_in_blackjack_duel(message.chat.id, challenger.id):
        await message.reply("У вас уже есть активная игра/приглашение в Блек Джек в этом чате.")
        return
    if is_user_in_blackjack_duel(message.chat.id, opponent.id):
        await message.reply("У второго игрока уже есть активная игра/приглашение в Блек Джек в этом чате.")
        return

    challenger_player = await db.get_player(challenger.id)
    opponent_player = await db.get_player(opponent.id)
    challenger_balance = int(challenger_player["balance"]) if challenger_player else 0
    opponent_balance = int(opponent_player["balance"]) if opponent_player else 0

    if challenger_balance < bet:
        await message.reply(f"У вас недостаточно монет. Нужно <b>{bet}</b> 🪙, баланс: <b>{challenger_balance}</b> 🪙")
        return
    if opponent_balance < bet:
        await message.reply(
            f"У игрока {mention_user(opponent.id, opponent.full_name)} недостаточно монет. "
            f"Нужно <b>{bet}</b> 🪙, баланс: <b>{opponent_balance}</b> 🪙",
            disable_web_page_preview=True,
        )
        return

    duel_id = next_blackjack_duel_id()
    BLACKJACK_DUEL_INVITES[duel_id] = {
        "id": duel_id,
        "chat_id": message.chat.id,
        "from_user_id": challenger.id,
        "to_user_id": opponent.id,
        "bet": bet,
        "from_name": mention_user(challenger.id, challenger.full_name),
        "to_name": mention_user(opponent.id, opponent.full_name),
    }

    await message.reply(
        "⚔️ <b>Вызов на дуэль по Блек Джеку</b>\n"
        f"Игрок {mention_user(challenger.id, challenger.full_name)} вызывает {mention_user(opponent.id, opponent.full_name)}.\n"
        f"Ставка каждого: <b>{bet}</b> 🪙\n"
        f"Банк: <b>{bet * 2}</b> 🪙\n\n"
        f"{mention_user(opponent.id, opponent.full_name)}, примите или отклоните вызов.",
        reply_markup=blackjack_duel_invite_kb(duel_id),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("bjd:"))
async def blackjack_duel_callbacks(callback: CallbackQuery, db: Database):
    if not callback.data or not callback.message:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Некорректная кнопка", show_alert=True)
        return

    action = parts[1]
    try:
        duel_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректная дуэль", show_alert=True)
        return

    if action in {"accept", "decline", "cancel"}:
        invite = BLACKJACK_DUEL_INVITES.get(duel_id)
        if not invite:
            await callback.answer("Приглашение уже не активно", show_alert=True)
            return

        if action == "cancel":
            if callback.from_user.id != int(invite["from_user_id"]):
                await callback.answer("Отменить вызов может только тот, кто его отправил", show_alert=True)
                return

            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(
                "🚫 Вызов на дуэль отменён.\n"
                f"{invite['from_name']} vs {invite['to_name']}\n"
                f"Ставка: <b>{int(invite['bet'])}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer("Вызов отменён")
            return

        if callback.from_user.id != int(invite["to_user_id"]):
            await callback.answer("Принять или отклонить может только вызванный игрок", show_alert=True)
            return

        if action == "decline":
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(
                "❌ Дуэль отклонена.\n"
                f"{invite['from_name']} vs {invite['to_name']}\n"
                f"Ставка: <b>{int(invite['bet'])}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer("Отклонено")
            return

        chat_id = int(invite["chat_id"])
        uid1 = int(invite["from_user_id"])
        uid2 = int(invite["to_user_id"])
        bet = int(invite["bet"])

        # Проверяем, что за время ожидания никто не начал другую игру.
        busy_reason = None
        if (chat_id, uid1) in BLACKJACK_GAMES or (chat_id, uid2) in BLACKJACK_GAMES:
            busy_reason = "один из игроков уже играет обычный Блек Джек"
        for active_game in BLACKJACK_DUELS.values():
            if int(active_game["chat_id"]) == chat_id and (uid1 in active_game["order"] or uid2 in active_game["order"]):
                busy_reason = "один из игроков уже участвует в другой дуэли"
                break
        for other_id, other_invite in BLACKJACK_DUEL_INVITES.items():
            if int(other_id) == duel_id:
                continue
            if int(other_invite["chat_id"]) == chat_id and (uid1 in {int(other_invite["from_user_id"]), int(other_invite["to_user_id"])} or uid2 in {int(other_invite["from_user_id"]), int(other_invite["to_user_id"])}):
                busy_reason = "у одного из игроков есть другое активное приглашение"
                break
        if busy_reason:
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(f"⚠️ Дуэль отменена: {busy_reason}.", disable_web_page_preview=True)
            await callback.answer()
            return

        p1 = await db.get_player(uid1)
        p2 = await db.get_player(uid2)
        b1 = int(p1["balance"]) if p1 else 0
        b2 = int(p2["balance"]) if p2 else 0
        if b1 < bet:
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(
                f"⚠️ Дуэль отменена: у {invite['from_name']} недостаточно монет. Баланс: <b>{b1}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer()
            return
        if b2 < bet:
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(
                f"⚠️ Дуэль отменена: у {invite['to_name']} недостаточно монет. Баланс: <b>{b2}</b> 🪙",
                disable_web_page_preview=True,
            )
            await callback.answer()
            return

        # Списываем ставки. Если второе списание не прошло, возвращаем первое.
        w1 = await db.withdraw_balance(uid1, bet, reason="game:blackjack_duel_bet", meta=f"duel_id={duel_id}")
        if not w1.get("ok"):
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(f"⚠️ Дуэль отменена: у {invite['from_name']} не удалось списать ставку.", disable_web_page_preview=True)
            await callback.answer()
            return
        w2 = await db.withdraw_balance(uid2, bet, reason="game:blackjack_duel_bet", meta=f"duel_id={duel_id}")
        if not w2.get("ok"):
            await db.add_balance(uid1, bet, reason="game:blackjack_duel_refund",)
            BLACKJACK_DUEL_INVITES.pop(duel_id, None)
            await callback.message.edit_text(f"⚠️ Дуэль отменена: у {invite['to_name']} не удалось списать ставку.", disable_web_page_preview=True)
            await callback.answer()
            return

        BLACKJACK_DUEL_INVITES.pop(duel_id, None)
        chance1 = await db.get_effective_chance(uid1)
        chance2 = await db.get_effective_chance(uid2)
        game = {
            "id": duel_id,
            "chat_id": chat_id,
            "bet": bet,
            "deck": bj_new_deck(),
            "order": [uid1, uid2],
            "players": {
                uid1: {"name": invite["from_name"], "cards": [], "status": "playing", "chance": chance1},
                uid2: {"name": invite["to_name"], "cards": [], "status": "playing", "chance": chance2},
            },
            "current": uid1,
        }
        for uid in game["order"]:
            chance = float(game["players"][uid].get("chance", 45))
            game["players"][uid]["cards"].append(bj_draw_for_chance(game, game["players"][uid]["cards"], chance))
            game["players"][uid]["cards"].append(bj_draw_for_chance(game, game["players"][uid]["cards"], chance))

        for uid in game["order"]:
            if bj_hand_score(game["players"][uid]["cards"]) == 21:
                game["players"][uid]["status"] = "stand"

        game["current"] = blackjack_duel_next_turn(game)
        if game["current"] is None:
            text = await finish_blackjack_duel(db, game)
            await callback.message.edit_text(text, disable_web_page_preview=True)
        else:
            BLACKJACK_DUELS[duel_id] = game
            await callback.message.edit_text(
                blackjack_duel_text(game) + "\n\nИгроки ходят по очереди. Нажимать кнопки может только тот, чей сейчас ход.",
                reply_markup=blackjack_duel_game_kb(duel_id),
                disable_web_page_preview=True,
            )
        await callback.answer("Дуэль началась")
        return

    if action in {"hit", "stand"}:
        game = BLACKJACK_DUELS.get(duel_id)
        if not game:
            await callback.answer("Дуэль уже завершена или не найдена", show_alert=True)
            return

        current = game.get("current")
        if callback.from_user.id != int(current or 0):
            current_name = game["players"][current]["name"] if current else "другой игрок"
            await callback.answer(f"Сейчас ходит {current_name}", show_alert=True)
            return

        p = game["players"][current]
        if action == "hit":
            # В дуэли личный/общий шанс влияет на карты каждого игрока отдельно.
            chance = await db.get_effective_chance(current)
            p["chance"] = chance
            p["cards"].append(bj_draw_for_chance(game, p["cards"], chance))
            score = bj_hand_score(p["cards"])
            if score > 21:
                p["status"] = "bust"
                game["current"] = None
                BLACKJACK_DUELS.pop(duel_id, None)
                text = await finish_blackjack_duel(db, game)
                await callback.message.edit_text(text, disable_web_page_preview=True)
                await callback.answer("Перебор")
                return
            if score == 21:
                p["status"] = "stand"
        else:
            p["status"] = "stand"

        game["current"] = blackjack_duel_next_turn(game)
        if game["current"] is None:
            BLACKJACK_DUELS.pop(duel_id, None)
            text = await finish_blackjack_duel(db, game)
            await callback.message.edit_text(text, disable_web_page_preview=True)
            await callback.answer()
            return

        await callback.message.edit_text(
            blackjack_duel_text(game) + "\n\nИгроки ходят по очереди. Нажимать кнопки может только тот, чей сейчас ход.",
            reply_markup=blackjack_duel_game_kb(duel_id),
            disable_web_page_preview=True,
        )
        await callback.answer()
        return

    await callback.answer("Неизвестное действие", show_alert=True)


@router.message(Command("blackjack", "bj"))
async def cmd_blackjack(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)

    parts = (command.args or "").split()
    if parts and parts[0].lower() in {"duel", "дуэль"}:
        await start_blackjack_duel(message, parts[1:], db)
        return

    if not parts:
        await message.reply(
            "Укажите ставку. Пример: <code>/blackjack 100</code> или <code>/bj 100</code>\n"
            "Дуэль ответом на сообщение: <code>/bj duel 100</code>"
        )
        return
    bet = parse_int(parts[0])
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0. Пример: <code>/blackjack 100</code>")
        return

    key = (message.chat.id, message.from_user.id)
    if key in BLACKJACK_GAMES:
        await message.reply("У вас уже есть активная партия Блек Джека. Завершите её кнопками под сообщением.")
        return
    if is_user_in_blackjack_duel(message.chat.id, message.from_user.id):
        await message.reply("У вас уже есть активная дуэль или приглашение в Блек Джек в этом чате.")
        return

    withdraw = await db.withdraw_balance(
        user_id=message.from_user.id,
        amount=bet,
        reason="game:blackjack_bet",
        meta="start",
    )
    if not withdraw.get("ok"):
        await send_bet_error(message, withdraw)
        return

    chance = await db.get_effective_chance(message.from_user.id)
    game = {
        "chat_id": message.chat.id,
        "user_id": message.from_user.id,
        "bet": bet,
        "deck": bj_new_deck(),
        "player": [],
        "dealer": [],
        "chance": chance,
    }

    # Первые карты тоже зависят от подкрутки: игроку с высоким шансом чаще идут безопасные/сильные карты,
    # дилеру наоборот.
    game["player"].append(bj_draw_for_chance(game, game["player"], chance))
    game["player"].append(bj_draw_for_chance(game, game["player"], chance))
    dealer_chance = max(0.0, min(100.0, 100.0 - chance))
    game["dealer"].append(bj_draw_for_chance(game, game["dealer"], dealer_chance))
    game["dealer"].append(bj_draw_for_chance(game, game["dealer"], dealer_chance))

    # Натуральный blackjack сразу завершается, но тоже учитывает подкрутку.
    if bj_is_blackjack(game["player"]):
        lucky = rng.uniform(0, 100) < chance
        dealer_blackjack = bj_is_blackjack(game["dealer"])
        if lucky and not dealer_blackjack:
            # Стандартная выплата за натуральный blackjack — 3:2, то есть всего x2.5.
            payout = (bet * 5) // 2
            text, _ = await bj_finish_game(db, game, "🤑 Blackjack! Победа с выплатой <b>x2.5</b>.", payout)
        else:
            hand = bj_find_hand_with_score(game["dealer"][0], 21)
            if hand:
                game["dealer"] = hand
            text, _ = await bj_finish_game(db, game, "🤝 Дилер тоже собрал Blackjack. Ставка возвращена.", bet)
        await message.reply(text)
        return

    BLACKJACK_GAMES[key] = game
    await message.reply(
        bj_game_text(game, hide_dealer=True)
        + f"\n\nСтавка списана. Баланс после ставки: <b>{int(withdraw['new_balance'])}</b> 🪙\n"
        + "Выберите действие:",
        reply_markup=bj_keyboard(),
    )


@router.message(Command("ttt", "xo", "xox"))
async def cmd_tictactoe(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    await start_tictactoe_duel(message, command, db)




class SimpleCommandArgs:
    def __init__(self, args: Optional[str]):
        self.args = args


def is_blackjack_text_command(message: Message) -> bool:
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return False
    first = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    return first in {"/bj", "/blackjack"}


@router.message(is_blackjack_text_command)
async def cmd_blackjack_fallback(message: Message, db: Database):
    """Запасной обработчик, если Command-фильтр не поймал /bj или /blackjack."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else None
    await cmd_blackjack(message, SimpleCommandArgs(args), db)

@router.callback_query(F.data.in_({"bj:hit", "bj:stand"}))
async def blackjack_callbacks(callback: CallbackQuery, db: Database):
    if not callback.message:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return

    key = (callback.message.chat.id, callback.from_user.id)
    game = BLACKJACK_GAMES.get(key)
    if not game:
        await callback.answer("У вас нет активной партии. Запустите /blackjack 100", show_alert=True)
        return

    action = callback.data
    if action == "bj:hit":
        chance = float(game.get("chance", await db.get_effective_chance(callback.from_user.id)))
        game["chance"] = chance
        game["player"].append(bj_draw_for_chance(game, game["player"], chance))
        player_score = bj_hand_score(game["player"])

        if player_score > 21:
            BLACKJACK_GAMES.pop(key, None)
            text, _ = await bj_finish_game(db, game, "💥 Перебор! Вы проиграли.", 0)
            await callback.message.edit_text(text)
            await callback.answer()
            return

        if player_score == 21:
            BLACKJACK_GAMES.pop(key, None)
            text, _ = await bj_resolve_dealer(db, game)
            await callback.message.edit_text(text)
            await callback.answer("У вас 21, дилер завершает игру")
            return

        await callback.message.edit_text(
            bj_game_text(game, hide_dealer=True) + "\n\nВыберите действие:",
            reply_markup=bj_keyboard(),
        )
        await callback.answer()
        return

    if action == "bj:stand":
        BLACKJACK_GAMES.pop(key, None)
        text, _ = await bj_resolve_dealer(db, game)
        await callback.message.edit_text(text)
        await callback.answer()
        return


@router.message(Command("slots"))
async def cmd_slots(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    bet = await get_bet_from_command(message, command)
    if bet is None:
        return

    chance = await db.get_effective_chance(message.from_user.id)
    win = rng.uniform(0, 100) < chance
    symbols = ["🍒", "🍋", "🍇", "🔔", "⭐", "💎"]

    multiplier = 0
    if win:
        roll = rng.uniform(0, 100)
        if roll < 70:
            multiplier = 2
            same = rng.choice(symbols)
            other = rng.choice([s for s in symbols if s != same])
            line = [same, same, other]
            rng.shuffle(line)
        elif roll < 95:
            multiplier = 3
            same = rng.choice(symbols[:-1])
            line = [same, same, same]
        else:
            multiplier = 5
            line = ["💎", "💎", "💎"]
    else:
        line = rng.sample(symbols, 3)  # все разные, чтобы визуально не выглядело как выигрыш

    payout = bet * multiplier
    result = await db.apply_bet_result(
        user_id=message.from_user.id,
        bet=bet,
        payout=payout,
        game="slots",
        meta=f"chance={chance};win={win};multiplier={multiplier};line={''.join(line)}",
    )
    if not result.get("ok"):
        await send_bet_error(message, result)
        return

    line_text = " | ".join(line)
    if win:
        await message.reply(
            f"🎰 <b>{line_text}</b>\n"
            f"✅ Выигрыш! Множитель: <b>x{multiplier}</b>\n"
            f"Профит: <b>+{result['profit']}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )
    else:
        await message.reply(f"🎰 <b>{line_text}</b>\n❌ Проигрыш: <b>-{bet}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙")


ROULETTE_ALIASES = {
    "red": "red",
    "r": "red",
    "красное": "red",
    "красный": "red",
    "к": "red",
    "black": "black",
    "b": "black",
    "черное": "black",
    "чёрное": "black",
    "черный": "black",
    "чёрный": "black",
    "ч": "black",
}


@router.message(Command("roulette"))
async def cmd_roulette(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not command.args:
        await message.reply("Пример: <code>/roulette red 10</code> или <code>/roulette черное 10</code>")
        return
    parts = command.args.split()
    if len(parts) < 2:
        await message.reply("Укажите цвет и ставку. Пример: <code>/roulette red 10</code>")
        return
    color = ROULETTE_ALIASES.get(parts[0].lower())
    bet = parse_int(parts[1])
    if color is None:
        await message.reply("Цвет должен быть red/black или красное/черное.")
        return
    if bet is None or bet <= 0:
        await message.reply("Ставка должна быть целым числом больше 0.")
        return

    chance = await db.get_effective_chance(message.from_user.id)
    win = rng.uniform(0, 100) < chance
    if win:
        landed = color
    else:
        # Иногда показываем зеро как проигрыш, чтобы рулетка выглядела живее.
        landed = "zero" if rng.uniform(0, 100) < 8 else ("black" if color == "red" else "red")

    payout = bet * 2 if win else 0
    result = await db.apply_bet_result(
        user_id=message.from_user.id,
        bet=bet,
        payout=payout,
        game="roulette",
        meta=f"chance={chance};bet_color={color};landed={landed};win={win}",
    )
    if not result.get("ok"):
        await send_bet_error(message, result)
        return

    icon = {"red": "🔴", "black": "⚫", "zero": "🟢"}[landed]
    landed_ru = {"red": "красное", "black": "черное", "zero": "zero"}[landed]
    if win:
        await message.reply(
            f"{icon} Выпало <b>{landed_ru}</b>. Победа!\n"
            f"Профит: <b>+{result['profit']}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙"
        )
    else:
        await message.reply(f"{icon} Выпало <b>{landed_ru}</b>. Проигрыш: <b>-{bet}</b> 🪙\nБаланс: <b>{result['new_balance']}</b> 🪙")


@router.message(Command("top"))
async def cmd_top(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    args = (command.args or "").split()
    global_top = bool(args and args[0].lower() in {"global", "all", "общий"})
    limit = 10
    for part in args:
        maybe = parse_int(part)
        if maybe:
            limit = max(1, min(maybe, 20))
            break

    chat_id = None
    title = "🏆 <b>Общий топ игроков</b>"
    if not global_top and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat_id = message.chat.id
        title = "🏆 <b>Топ этой группы</b>"

    top = await db.get_top(limit=limit, chat_id=chat_id)
    if not top:
        await message.reply("Пока нет игроков в топе.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [title]
    for idx, p in enumerate(top, start=1):
        medal = medals[idx - 1] if idx <= 3 else f"{idx}."
        lines.append(f"{medal} {player_link(p)} — <b>{int(p['balance'])}</b> 🪙")
    if chat_id is not None:
        lines.append("\nДля общего топа: <code>/top global</code>")
    await message.reply("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database):
    await ensure_player(message, db)
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Нет доступа.")
        return
    text = (
        "🛠 <b>Админ-панель</b>\n"
        "Здесь можно управлять игроками, балансами и шансами выпадения.\n\n"
        "Быстрые команды:\n"
        "<code>/player ID</code> — карточка игрока\n"
        "<code>/setbalance ID 100</code> — установить баланс\n"
        "<code>/addbalance ID 10</code> — добавить/списать монеты\n"
        "<code>/setchance ID 60</code> — личный шанс игрока\n"
        "<code>/setchance ID off</code> — убрать личный шанс\n"
        "<code>/ban ID</code>, <code>/unban ID</code>\n"
        "<code>/mute ID</code>, <code>/unmute ID</code> — запретить/разрешить обычные ЛС боту\n\n"
        "📣 Бродкаст отправляет сообщение во все чаты, где бот уже видел команды."
    )
    await message.reply(text, reply_markup=admin_main_kb())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext, db: Database):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    data = callback.data or ""

    if data == "admin:menu":
        await state.clear()
        await safe_edit_text(callback.message, "🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
        await callback.answer()
        return

    if data == "admin:stats":
        stats = await db.get_stats()
        text = (
            "📊 <b>Статистика</b>\n"
            f"Игроков: <b>{int(stats['players_count'])}</b>\n"
            f"Забанено: <b>{int(stats['banned_count'] or 0)}</b>\n"
            f"В муте ЛС: <b>{int(stats['muted_count'] or 0)}</b>\n"
            f"Суммарный баланс: <b>{int(stats['total_balance'])}</b> 🪙\n"
            f"Макс. баланс: <b>{int(stats['max_balance'])}</b> 🪙\n"
        f"Игроков с личным шансом: <b>{int(stats['personal_chance_count'] or 0)}</b>\n"
        f"Общий шанс: <b>{float(stats['global_win_chance']):g}%</b>"
    )
        await safe_edit_text(callback.message, text, reply_markup=admin_back_kb())
        await callback.answer()
        return

    if data == "admin:broadcast":
        chat_ids = await db.get_known_chat_ids()
        await state.set_state(AdminStates.waiting_broadcast)
        await safe_edit_text(
            callback.message,
            "📣 <b>Бродкаст</b>\n"
            f"Бот знает чатов: <b>{len(chat_ids)}</b>.\n\n"
            "Отправьте следующим сообщением текст, фото, видео или любой пост, который нужно разослать.\n"
            "Для отмены отправьте <code>/cancel</code>.",
            reply_markup=admin_back_kb(),
        )
        await callback.answer()
        return

    if data == "admin:global_chance":
        await state.set_state(AdminStates.waiting_global_chance)
        await safe_edit_text(
            callback.message,
            "🎯 Отправьте новый общий шанс выигрыша от <b>0</b> до <b>100</b>.\n"
            "Например: <code>45</code> или <code>62.5</code>",
            reply_markup=admin_back_kb(),
        )
        await callback.answer()
        return

    if data == "admin:find_player":
        await state.set_state(AdminStates.waiting_player_lookup)
        await safe_edit_text(
            callback.message,
            "👤 Отправьте Telegram ID игрока или @username.\n"
            "Важно: игрок должен хотя бы раз написать команду боту.",
            reply_markup=admin_back_kb(),
        )
        await callback.answer()
        return

    if data == "admin:top":
        top = await db.get_top(limit=10, chat_id=None)
        lines = ["🏆 <b>Общий топ игроков</b>"]
        if not top:
            lines.append("Пока пусто.")
        for idx, p in enumerate(top, start=1):
            lines.append(f"{idx}. {player_link(p)} — <b>{int(p['balance'])}</b> 🪙")
        await safe_edit_text(callback.message, "\n".join(lines), reply_markup=admin_back_kb(), disable_web_page_preview=True)
        await callback.answer()
        return

    if data.startswith("admin:p:"):
        parts = data.split(":")
        # admin:p:action:user_id[:value]
        if len(parts) < 4:
            await callback.answer("Некорректная кнопка", show_alert=True)
            return
        action = parts[2]
        uid = int(parts[3])
        player = await db.get_player(uid)
        if not player:
            await callback.answer("Игрок не найден", show_alert=True)
            return

        if action == "setbal":
            await state.update_data(target_user_id=uid)
            await state.set_state(AdminStates.waiting_set_balance)
            await safe_edit_text(
                callback.message,
                f"💰 Отправьте новый баланс для <code>{uid}</code>.",
                reply_markup=admin_back_kb(),
            )
            await callback.answer()
            return

        if action == "addbal":
            await state.update_data(target_user_id=uid)
            await state.set_state(AdminStates.waiting_add_balance)
            await safe_edit_text(
                callback.message,
                f"➕ Отправьте изменение баланса для <code>{uid}</code>.\n"
                "Например: <code>10</code> или <code>-50</code>",
                reply_markup=admin_back_kb(),
            )
            await callback.answer()
            return

        if action == "chance":
            await state.update_data(target_user_id=uid)
            await state.set_state(AdminStates.waiting_player_chance)
            await safe_edit_text(
                callback.message,
                f"🎯 Отправьте личный шанс для <code>{uid}</code> от 0 до 100.\n"
                "Или отправьте <code>off</code>, чтобы вернуть общий шанс.",
                reply_markup=admin_back_kb(),
            )
            await callback.answer()
            return

        if action == "chanceoff":
            await db.set_personal_chance(uid, None)
            player = await db.get_player(uid)
            await safe_edit_text(callback.message, await player_card_text(db, player), reply_markup=player_admin_kb(player))
            await callback.answer("Личный шанс отключен")
            return

        if action == "ban":
            value = bool(int(parts[4])) if len(parts) >= 5 else True
            await db.set_ban(uid, value)
            player = await db.get_player(uid)
            await safe_edit_text(callback.message, await player_card_text(db, player), reply_markup=player_admin_kb(player))
            await callback.answer("Готово")
            return

        if action == "mute":
            value = bool(int(parts[4])) if len(parts) >= 5 else True
            await db.set_mute(uid, value)
            player = await db.get_player(uid)
            await safe_edit_text(callback.message, await player_card_text(db, player), reply_markup=player_admin_kb(player))
            await callback.answer("ЛС замучены" if value else "ЛС размучены")
            return

    await callback.answer("Неизвестное действие", show_alert=True)


@router.message(AdminStates.waiting_global_chance)
async def state_global_chance(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    value = parse_percent(message.text or "")
    if value is None:
        await message.reply("Введите число от 0 до 100. Например: <code>45</code>")
        return
    await db.set_global_chance(value)
    await state.clear()
    await message.reply(f"✅ Общий шанс установлен: <b>{value:g}%</b>", reply_markup=admin_main_kb())


@router.message(AdminStates.waiting_player_lookup)
async def state_player_lookup(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    player = await db.find_player(message.text or "")
    if not player:
        await message.reply("Игрок не найден. Укажите ID или @username игрока, который уже пользовался ботом.", reply_markup=admin_back_kb())
        return
    await state.clear()
    await message.reply(await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(AdminStates.waiting_set_balance)
async def state_set_balance(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    amount = parse_int(message.text or "")
    if amount is None or amount < 0:
        await message.reply("Введите целый баланс от 0 и выше.")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    await db.set_balance(uid, amount)
    player = await db.get_player(uid)
    await state.clear()
    await message.reply("✅ Баланс обновлен.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(AdminStates.waiting_add_balance)
async def state_add_balance(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    delta = parse_int(message.text or "")
    if delta is None:
        await message.reply("Введите целое число. Например: <code>10</code> или <code>-50</code>")
        return
    data = await state.get_data()
    uid = int(data["target_user_id"])
    await db.add_balance(uid, delta)
    player = await db.get_player(uid)
    await state.clear()
    await message.reply("✅ Баланс изменен.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(AdminStates.waiting_player_chance)
async def state_player_chance(message: Message, state: FSMContext, db: Database):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().lower()
    data = await state.get_data()
    uid = int(data["target_user_id"])
    if raw in {"off", "none", "нет", "выкл", "общий"}:
        await db.set_personal_chance(uid, None)
    else:
        value = parse_percent(raw)
        if value is None:
            await message.reply("Введите шанс от 0 до 100 или <code>off</code>.")
            return
        await db.set_personal_chance(uid, value)
    player = await db.get_player(uid)
    await state.clear()
    await message.reply("✅ Шанс обновлен.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(AdminStates.waiting_broadcast)
async def state_broadcast(message: Message, state: FSMContext, bot: Bot, db: Database):
    if not is_admin(message.from_user.id):
        return

    if (message.text or "").strip().lower() in {"/cancel", "cancel", "отмена"}:
        await state.clear()
        await message.reply("🚫 Бродкаст отменён.", reply_markup=admin_main_kb())
        return

    chat_ids = await db.get_known_chat_ids()
    if not chat_ids:
        await state.clear()
        await message.reply("Пока нет известных чатов для рассылки.", reply_markup=admin_main_kb())
        return

    await state.clear()
    status_message = await message.reply(f"📣 Начинаю рассылку по <b>{len(chat_ids)}</b> чатам...")
    sent = 0
    failed = 0
    failed_ids: list[int] = []
    signature = admin_signature(message.from_user)

    for chat_id in chat_ids:
        try:
            await message.copy_to(chat_id=chat_id)
        except Exception:
            failed += 1
            if len(failed_ids) < 10:
                failed_ids.append(chat_id)
        else:
            sent += 1
            try:
                await bot.send_message(chat_id, signature, disable_web_page_preview=True)
            except Exception:
                pass
        await asyncio.sleep(0.07)

    failed_text = ""
    if failed_ids:
        failed_text = "\nНе удалось отправить в: " + ", ".join(f"<code>{chat_id}</code>" for chat_id in failed_ids)
        if failed > len(failed_ids):
            failed_text += f" и ещё {failed - len(failed_ids)}"

    await status_message.edit_text(
        "📣 <b>Бродкаст завершён</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Ошибок: <b>{failed}</b>"
        f"{failed_text}"
    )
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


async def require_admin_message(message: Message) -> bool:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.reply("⛔ Нет доступа.")
        return False
    return True


@router.message(Command("player"))
async def cmd_player(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if not command.args:
        await message.reply("Пример: <code>/player 123456789</code> или <code>/player @username</code>")
        return
    player = await db.find_player(command.args.strip())
    if not player:
        await message.reply("Игрок не найден.")
        return
    await message.reply(await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("setbalance"))
async def cmd_setbalance(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.reply("Пример: <code>/setbalance 123456789 100</code>")
        return
    player = await db.find_player(parts[0])
    amount = parse_int(parts[1])
    if not player or amount is None or amount < 0:
        await message.reply("Проверьте ID игрока и сумму.")
        return
    await db.set_balance(int(player["user_id"]), amount)
    player = await db.get_player(int(player["user_id"]))
    await message.reply("✅ Готово.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("addbalance"))
async def cmd_addbalance(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.reply("Пример: <code>/addbalance 123456789 10</code> или <code>/addbalance 123456789 -50</code>")
        return
    player = await db.find_player(parts[0])
    delta = parse_int(parts[1])
    if not player or delta is None:
        await message.reply("Проверьте ID игрока и число.")
        return
    await db.add_balance(int(player["user_id"]), delta)
    player = await db.get_player(int(player["user_id"]))
    await message.reply("✅ Готово.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("setchance"))
async def cmd_setchance(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    parts = (command.args or "").split()
    if len(parts) < 2:
        await message.reply("Пример: <code>/setchance 123456789 60</code> или <code>/setchance 123456789 off</code>")
        return
    player = await db.find_player(parts[0])
    if not player:
        await message.reply("Игрок не найден.")
        return
    raw = parts[1].lower()
    if raw in {"off", "none", "нет", "выкл", "общий"}:
        await db.set_personal_chance(int(player["user_id"]), None)
    else:
        value = parse_percent(raw)
        if value is None:
            await message.reply("Шанс должен быть от 0 до 100 или off.")
            return
        await db.set_personal_chance(int(player["user_id"]), value)
    player = await db.get_player(int(player["user_id"]))
    await message.reply("✅ Готово.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if not command.args:
        await message.reply("Пример: <code>/ban 123456789</code>")
        return
    player = await db.find_player(command.args.strip())
    if not player:
        await message.reply("Игрок не найден.")
        return
    await db.set_ban(int(player["user_id"]), True)
    await message.reply("🔒 Игрок забанен.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if not command.args:
        await message.reply("Пример: <code>/unban 123456789</code>")
        return
    player = await db.find_player(command.args.strip())
    if not player:
        await message.reply("Игрок не найден.")
        return
    await db.set_ban(int(player["user_id"]), False)
    await message.reply("🔓 Игрок разбанен.")


@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if not command.args:
        await message.reply("Пример: <code>/mute 123456789</code>")
        return
    player = await db.find_player(command.args.strip())
    if not player:
        await message.reply("Игрок не найден.")
        return
    await db.set_mute(int(player["user_id"]), True)
    player = await db.get_player(int(player["user_id"]))
    await message.reply("🔇 Игрок замучен в ЛС бота.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if not command.args:
        await message.reply("Пример: <code>/unmute 123456789</code>")
        return
    player = await db.find_player(command.args.strip())
    if not player:
        await message.reply("Игрок не найден.")
        return
    await db.set_mute(int(player["user_id"]), False)
    player = await db.get_player(int(player["user_id"]))
    await message.reply("🔈 Игрок размучен в ЛС бота.\n\n" + await player_card_text(db, player), reply_markup=player_admin_kb(player))


@router.message(Command("reply"))
async def cmd_reply_to_user(message: Message, command: CommandObject, bot: Bot, db: Database):
    await ensure_player(message, db)
    if not await require_admin_message(message):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Команда /reply работает в личке с ботом.")
        return

    args = (command.args or "").strip()
    target_user_id = None
    reply_text = args

    if args:
        parts = args.split(maxsplit=1)
        maybe_id = parse_int(parts[0])
        if maybe_id:
            target_user_id = maybe_id
            reply_text = parts[1] if len(parts) > 1 else ""

    if not target_user_id and message.reply_to_message:
        target_user_id = await resolve_admin_reply_target(message, db)

    if not target_user_id:
        await message.reply(
            "Не нашёл получателя. Используйте <code>/reply ID текст</code> "
            "или ответьте командой <code>/reply текст</code> на сообщение обращения."
        )
        return
    if not reply_text:
        await message.reply("Укажите текст ответа. Пример: <code>/reply 123456789 Привет</code>")
        return

    try:
        await bot.send_message(
            target_user_id,
            "📨 <b>Ответ администратора:</b>\n"
            + escape(reply_text)
            + "\n\n"
            + admin_signature(message.from_user),
            disable_web_page_preview=True,
        )
        await message.reply("✅ Ответ отправлен пользователю.")
    except Exception:
        await message.reply("⚠️ Не удалось отправить ответ. Возможно, пользователь заблокировал бота.")


@router.message()
async def forward_non_command_messages_to_admins(message: Message, bot: Bot, db: Database):
    """
    1) Сообщения пользователей из ЛИЧКИ отправляет админам.
    2) Сообщения из групп НЕ отправляет админам.
    3) Если админ отвечает реплаем на полученное сообщение — ответ уходит пользователю.
    """
    if is_command_like_message(message):
        return

    await ensure_player(message, db)

    # Ответ админа пользователю.
    if message.from_user and is_admin(message.from_user.id):
        if message.chat.type == ChatType.PRIVATE and message.reply_to_message:
            target_user_id = await resolve_admin_reply_target(message, db)
            if not target_user_id:
                await message.answer(
                    "⚠️ Не нашёл получателя. Ответьте реплаем именно на сообщение пользователя, "
                    "которое бот прислал вам после обращения. Или используйте <code>/reply ID текст</code>."
                )
                return
            try:
                await bot.send_message(target_user_id, "📨 <b>Ответ администратора:</b>")
                await bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                await bot.send_message(target_user_id, admin_signature(message.from_user), disable_web_page_preview=True)
                await message.answer("✅ Ответ отправлен пользователю.")
            except Exception:
                await message.answer("⚠️ Не удалось отправить ответ. Возможно, пользователь заблокировал бота.")
        return

    # Не отправляем админам обычные сообщения из групп/супергрупп.
    if message.chat.type != ChatType.PRIVATE:
        return

    if not message.from_user:
        return

    player = await db.get_player(message.from_user.id)
    if player and int(player.get("is_muted") or 0):
        await message.answer("🔇 Вы в муте. В личке бота доступны только команды.")
        return

    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            info_msg = await bot.send_message(
                admin_id,
                user_info_text(message)
                + "\n\n↩️ <b>Чтобы ответить пользователю</b>, ответьте реплаем на это сообщение или на сообщение ниже.\n"
                + f"Запасной вариант: <code>/reply {message.from_user.id} текст ответа</code>",
                disable_web_page_preview=True,
            )
            ADMIN_REPLY_TARGETS[(admin_id, info_msg.message_id)] = message.from_user.id
            await db.save_admin_reply_target(admin_id, info_msg.message_id, message.from_user.id)

            try:
                copied_msg = await bot.copy_message(
                    chat_id=admin_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            except Exception:
                copied_msg = None

            # Админ может ответить реплаем либо на инфо-сообщение, либо на скопированное сообщение пользователя.
            if copied_msg:
                ADMIN_REPLY_TARGETS[(admin_id, copied_msg.message_id)] = message.from_user.id
                await db.save_admin_reply_target(admin_id, copied_msg.message_id, message.from_user.id)
            delivered += 1
        except Exception:
            # Например, админ еще не нажал /start у бота или заблокировал бота.
            pass

    if delivered:
        await message.answer("✅ Сообщение отправлено администраторам.")
    else:
        await message.answer("⚠️ Не удалось отправить сообщение администраторам. Админ должен сначала открыть бота и нажать /start.")



@router.error()
async def global_error_handler(event: ErrorEvent, bot: Bot):
    """Показывает ошибки в консоли и отправляет их админам, чтобы легче чинить Pydroid/Telegram ошибки."""
    if isinstance(event.exception, TelegramRetryAfter):
        print(f"Telegram flood control: retry after {event.exception.retry_after} seconds")
        return True

    error_text = "".join(
        traceback.format_exception(
            type(event.exception),
            event.exception,
            event.exception.__traceback__,
        )
    )
    print(error_text)

    short_text = error_text[-3500:]
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "⚠️ <b>Ошибка в боте</b>\n<pre>" + escape(short_text) + "</pre>")
        except Exception:
            pass

    return True



async def set_commands(bot: Bot):
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустить бота"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="balance", description="Баланс"),
            BotCommand(command="bonus", description="Получить +10 монет раз в 5 минут"),
            BotCommand(command="freespin", description="Бесплатный прокрут раз в день"),
            BotCommand(command="give", description="Передать монеты: /give @username 10"),
            BotCommand(command="casino", description="Ставка: /casino 10"),
            BotCommand(command="slots", description="Слоты: /slots 10"),
            BotCommand(command="coin", description="Монетка x2: /coin орел 10"),
            BotCommand(command="crash", description="Crash: /crash 10 2.0"),
            BotCommand(command="blackjack", description="Блек Джек: /blackjack 100"),
            BotCommand(command="bj", description="Блек Джек коротко: /bj 100"),
            BotCommand(command="ttt", description="Крестики-нолики дуэль: /ttt 100"),
            BotCommand(command="roulette", description="Рулетка: /roulette red 10"),
            BotCommand(command="top", description="Топ игроков"),
        ]
    )


async def main():
    if not BOT_TOKEN or BOT_TOKEN in {"ВАШ_ТОКЕН_ОТ_BOTFATHER", "твой_токен", "123456:ABCDEF"}:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Варианты исправления:\n"
            "1) Создайте файл .env рядом с bot.py и напишите: BOT_TOKEN=ваш_токен\n"
            "2) Если запускаете в Pydroid 3 и .env не читается, откройте bot.py и вставьте токен в MANUAL_BOT_TOKEN.\n"
            f"Текущая папка запуска: {Path.cwd()}"
        )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(flood_retry_request_middleware)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    db = Database(DB_PATH)
    await db.init(default_global_chance=DEFAULT_GLOBAL_WIN_CHANCE)

    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Casino bot started")
    await dp.start_polling(bot, db=db)


if __name__ == "__main__":
    asyncio.run(main())
