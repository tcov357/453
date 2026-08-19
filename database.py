import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import aiosqlite


class Database:
    def __init__(self, path: str):
        self.path = path
        self._schema_ready = False
        self._default_global_chance = 45.0

    @asynccontextmanager
    async def _connect(self):
        db_path = Path(self.path)
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        if not self._schema_ready:
            await self._init_schema(conn, self._default_global_chance)
            self._schema_ready = True
        try:
            yield conn
        finally:
            await conn.close()

    async def init(self, default_global_chance: float = 45.0):
        self._default_global_chance = float(default_global_chance)
        async with self._connect() as db:
            await self._verify_schema(db)

    async def _init_schema(self, db: aiosqlite.Connection, default_global_chance: float):
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                last_bonus_at INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                is_muted INTEGER NOT NULL DEFAULT 0,
                personal_chance REAL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                meta TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES players(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS player_chats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id),
                FOREIGN KEY(user_id) REFERENCES players(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS admin_reply_targets (
                admin_id INTEGER NOT NULL,
                admin_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(admin_id, admin_message_id)
            );

            CREATE TABLE IF NOT EXISTS daily_rewards (
                user_id INTEGER NOT NULL,
                reward_key TEXT NOT NULL,
                last_claim_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, reward_key),
                FOREIGN KEY(user_id) REFERENCES players(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_players_username ON players(username);
            CREATE INDEX IF NOT EXISTS idx_players_balance ON players(balance DESC);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_player_chats_chat_id ON player_chats(chat_id);
            """
        )
        cur = await db.execute("PRAGMA table_info(players)")
        columns = {str(row["name"]) for row in await cur.fetchall()}
        if "is_muted" not in columns:
            await db.execute("ALTER TABLE players ADD COLUMN is_muted INTEGER NOT NULL DEFAULT 0")
        await db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('global_win_chance', ?)",
            (str(float(default_global_chance)),),
        )
        await db.commit()
        await self._verify_schema(db)

    async def _verify_schema(self, db: aiosqlite.Connection):
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'players'")
        if not await cur.fetchone():
            raise RuntimeError(f"Database schema was not initialized for {self.path}")

    async def register_or_update_player(self, user_id: int, username: Optional[str], first_name: Optional[str]):
        now = int(time.time())
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO players(user_id, username, first_name, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (user_id, username or "", first_name or "", now, now),
            )
            await db.commit()

    async def touch_chat(self, chat_id: int, user_id: int):
        now = int(time.time())
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO player_chats(chat_id, user_id, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                (chat_id, user_id, now, now),
            )
            await db.commit()

    async def get_known_chat_ids(self) -> list[int]:
        async with self._connect() as db:
            cur = await db.execute(
                """
                SELECT chat_id, MAX(last_seen_at) AS last_seen
                FROM player_chats
                GROUP BY chat_id
                ORDER BY last_seen DESC
                """
            )
            rows = await cur.fetchall()
            return [int(row["chat_id"]) for row in rows]

    async def save_admin_reply_target(self, admin_id: int, admin_message_id: int, user_id: int):
        now = int(time.time())
        async with self._connect() as db:
            await db.execute(
                """
                INSERT INTO admin_reply_targets(admin_id, admin_message_id, user_id, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(admin_id, admin_message_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    created_at = excluded.created_at
                """,
                (admin_id, admin_message_id, user_id, now),
            )
            await db.commit()

    async def get_admin_reply_target(self, admin_id: int, admin_message_id: int) -> Optional[int]:
        async with self._connect() as db:
            cur = await db.execute(
                "SELECT user_id FROM admin_reply_targets WHERE admin_id = ? AND admin_message_id = ?",
                (admin_id, admin_message_id),
            )
            row = await cur.fetchone()
            return int(row["user_id"]) if row else None

    async def get_near_admin_reply_target(self, admin_id: int, admin_message_id: int, max_distance: int = 8) -> Optional[int]:
        async with self._connect() as db:
            cur = await db.execute(
                """
                SELECT user_id, admin_message_id
                FROM admin_reply_targets
                WHERE admin_id = ?
                  AND admin_message_id <= ?
                  AND admin_message_id >= ?
                ORDER BY admin_message_id DESC
                LIMIT 1
                """,
                (admin_id, admin_message_id, admin_message_id - int(max_distance)),
            )
            row = await cur.fetchone()
            return int(row["user_id"]) if row else None

    async def get_player(self, user_id: int) -> Optional[dict[str, Any]]:
        async with self._connect() as db:
            cur = await db.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def find_player(self, query: str) -> Optional[dict[str, Any]]:
        query = query.strip()
        if not query:
            return None
        async with self._connect() as db:
            if query.startswith("@"):
                username = query[1:].lower()
                cur = await db.execute("SELECT * FROM players WHERE lower(username) = ?", (username,))
            else:
                try:
                    user_id = int(query)
                except ValueError:
                    username = query.lower()
                    cur = await db.execute("SELECT * FROM players WHERE lower(username) = ?", (username,))
                else:
                    cur = await db.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self._connect() as db:
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    async def get_global_chance(self) -> float:
        return float(await self.get_setting("global_win_chance", "45"))

    async def set_global_chance(self, chance: float):
        await self.set_setting("global_win_chance", str(float(chance)))

    async def get_effective_chance(self, user_id: int) -> float:
        player = await self.get_player(user_id)
        if player and player.get("personal_chance") is not None:
            return float(player["personal_chance"])
        return await self.get_global_chance()

    async def set_personal_chance(self, user_id: int, chance: Optional[float]):
        async with self._connect() as db:
            await db.execute("UPDATE players SET personal_chance = ?, updated_at = ? WHERE user_id = ?", (chance, int(time.time()), user_id))
            await db.commit()

    async def set_ban(self, user_id: int, is_banned: bool):
        async with self._connect() as db:
            await db.execute("UPDATE players SET is_banned = ?, updated_at = ? WHERE user_id = ?", (1 if is_banned else 0, int(time.time()), user_id))
            await db.commit()

    async def set_mute(self, user_id: int, is_muted: bool):
        async with self._connect() as db:
            await db.execute("UPDATE players SET is_muted = ?, updated_at = ? WHERE user_id = ?", (1 if is_muted else 0, int(time.time()), user_id))
            await db.commit()

    async def set_balance(self, user_id: int, amount: int, reason: str = "admin_set_balance"):
        amount = max(0, int(amount))
        now = int(time.time())
        async with self._connect() as db:
            cur = await db.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                return None
            old_balance = int(row["balance"])
            delta = amount - old_balance
            await db.execute("UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?", (amount, now, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, delta, reason, f"old={old_balance};new={amount}", now),
            )
            await db.commit()
            return amount

    async def add_balance(self, user_id: int, delta: int, reason: str = "admin_add_balance", meta: str = ""):
        now = int(time.time())
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return None
            old_balance = int(row["balance"])
            new_balance = max(0, old_balance + int(delta))
            real_delta = new_balance - old_balance
            await db.execute("UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?", (new_balance, now, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, real_delta, reason, (f"old={old_balance};new={new_balance}" + (f";{meta}" if meta else "")), now),
            )
            await db.commit()
            return new_balance

    async def transfer_balance(self, from_user_id: int, to_user_id: int, amount: int):
        """Перевод игровых монет между игроками."""
        amount = int(amount)
        now = int(time.time())
        if amount <= 0:
            return {"ok": False, "error": "bad_amount"}
        if int(from_user_id) == int(to_user_id):
            return {"ok": False, "error": "self_transfer"}

        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")

            cur = await db.execute(
                "SELECT user_id, balance, is_banned FROM players WHERE user_id IN (?, ?)",
                (from_user_id, to_user_id),
            )
            rows = await cur.fetchall()
            players = {int(r["user_id"]): r for r in rows}

            sender = players.get(int(from_user_id))
            receiver = players.get(int(to_user_id))

            if not sender or not receiver:
                await db.rollback()
                return {"ok": False, "error": "not_found"}
            if int(sender["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "sender_banned"}
            if int(receiver["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "receiver_banned"}

            sender_balance = int(sender["balance"])
            receiver_balance = int(receiver["balance"])
            if sender_balance < amount:
                await db.rollback()
                return {"ok": False, "error": "not_enough", "balance": sender_balance}

            new_sender_balance = sender_balance - amount
            new_receiver_balance = receiver_balance + amount

            await db.execute(
                "UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_sender_balance, now, from_user_id),
            )
            await db.execute(
                "UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_receiver_balance, now, to_user_id),
            )
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (from_user_id, -amount, "transfer_out", f"to={to_user_id}", now),
            )
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (to_user_id, amount, "transfer_in", f"from={from_user_id}", now),
            )
            await db.commit()
            return {
                "ok": True,
                "amount": amount,
                "sender_balance": new_sender_balance,
                "receiver_balance": new_receiver_balance,
            }

    async def withdraw_balance(self, user_id: int, amount: int, reason: str = "withdraw", meta: str = ""):
        """Списывает монеты с баланса. Используется для игр с несколькими ходами."""
        amount = int(amount)
        now = int(time.time())
        if amount <= 0:
            return {"ok": False, "error": "bad_amount"}

        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance, is_banned FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return {"ok": False, "error": "not_found"}
            if int(row["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "banned"}

            balance = int(row["balance"])
            if balance < amount:
                await db.rollback()
                return {"ok": False, "error": "not_enough", "balance": balance}

            new_balance = balance - amount
            await db.execute("UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?", (new_balance, now, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, -amount, reason, meta, now),
            )
            await db.commit()
            return {"ok": True, "new_balance": new_balance}

    async def claim_bonus(self, user_id: int, amount: int, cooldown_seconds: int):
        now = int(time.time())
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance, last_bonus_at, is_banned FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return {"ok": False, "error": "not_found"}
            if int(row["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "banned"}

            last_bonus_at = int(row["last_bonus_at"] or 0)
            remaining = cooldown_seconds - (now - last_bonus_at)
            if remaining > 0:
                await db.rollback()
                return {"ok": False, "error": "cooldown", "remaining": remaining, "balance": int(row["balance"])}

            new_balance = int(row["balance"]) + int(amount)
            await db.execute(
                "UPDATE players SET balance = ?, last_bonus_at = ?, updated_at = ? WHERE user_id = ?",
                (new_balance, now, now, user_id),
            )
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, amount, "bonus", "", now),
            )
            await db.commit()
            return {"ok": True, "balance": new_balance}

    async def claim_daily_reward(self, user_id: int, reward_key: str, amount: int, cooldown_seconds: int, meta: str = ""):
        amount = int(amount)
        now = int(time.time())
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance, is_banned FROM players WHERE user_id = ?", (user_id,))
            player = await cur.fetchone()
            if not player:
                await db.rollback()
                return {"ok": False, "error": "not_found"}
            if int(player["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "banned"}

            cur = await db.execute(
                "SELECT last_claim_at FROM daily_rewards WHERE user_id = ? AND reward_key = ?",
                (user_id, reward_key),
            )
            row = await cur.fetchone()
            last_claim_at = int(row["last_claim_at"] or 0) if row else 0
            remaining = int(cooldown_seconds) - (now - last_claim_at)
            if remaining > 0:
                await db.rollback()
                return {"ok": False, "error": "cooldown", "remaining": remaining, "balance": int(player["balance"])}

            new_balance = int(player["balance"]) + amount
            await db.execute(
                "UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?",
                (new_balance, now, user_id),
            )
            await db.execute(
                """
                INSERT INTO daily_rewards(user_id, reward_key, last_claim_at)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id, reward_key) DO UPDATE SET last_claim_at = excluded.last_claim_at
                """,
                (user_id, reward_key, now),
            )
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, amount, f"daily_reward:{reward_key}", meta, now),
            )
            await db.commit()
            return {"ok": True, "balance": new_balance, "amount": amount}

    async def apply_bet_result(self, user_id: int, bet: int, payout: int, game: str, meta: str):
        """
        bet списывается, payout начисляется. При выигрыше 2x payout означает: ставка вернулась + прибыль.
        Возвращает dict с ok/new_balance/profit или error.
        """
        bet = int(bet)
        payout = int(payout)
        now = int(time.time())
        if bet <= 0:
            return {"ok": False, "error": "bad_bet"}
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance, is_banned FROM players WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return {"ok": False, "error": "not_found"}
            if int(row["is_banned"]):
                await db.rollback()
                return {"ok": False, "error": "banned"}
            balance = int(row["balance"])
            if balance < bet:
                await db.rollback()
                return {"ok": False, "error": "not_enough", "balance": balance}

            delta = -bet + payout
            new_balance = balance + delta
            await db.execute("UPDATE players SET balance = ?, updated_at = ? WHERE user_id = ?", (new_balance, now, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, delta, reason, meta, created_at) VALUES(?, ?, ?, ?, ?)",
                (user_id, delta, f"game:{game}", meta, now),
            )
            await db.commit()
            return {"ok": True, "new_balance": new_balance, "profit": delta}

    async def get_top(self, limit: int = 10, chat_id: Optional[int] = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        async with self._connect() as db:
            if chat_id is None:
                cur = await db.execute(
                    """
                    SELECT user_id, username, first_name, balance, personal_chance
                    FROM players
                    WHERE is_banned = 0
                    ORDER BY balance DESC, updated_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = await db.execute(
                    """
                    SELECT p.user_id, p.username, p.first_name, p.balance, p.personal_chance
                    FROM players p
                    INNER JOIN player_chats pc ON pc.user_id = p.user_id
                    WHERE p.is_banned = 0 AND pc.chat_id = ?
                    ORDER BY p.balance DESC, p.updated_at ASC
                    LIMIT ?
                    """,
                    (chat_id, limit),
                )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_stats(self) -> dict[str, Any]:
        async with self._connect() as db:
            cur = await db.execute(
                """
                SELECT
                    COUNT(*) AS players_count,
                    COALESCE(SUM(balance), 0) AS total_balance,
                    COALESCE(MAX(balance), 0) AS max_balance,
                    SUM(CASE WHEN is_banned = 1 THEN 1 ELSE 0 END) AS banned_count,
                    SUM(CASE WHEN is_muted = 1 THEN 1 ELSE 0 END) AS muted_count,
                    SUM(CASE WHEN personal_chance IS NOT NULL THEN 1 ELSE 0 END) AS personal_chance_count
                FROM players
                """
            )
            row = await cur.fetchone()
            stats = dict(row)
            stats["global_win_chance"] = await self.get_global_chance()
            return stats
