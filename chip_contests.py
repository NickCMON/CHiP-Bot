from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class ContestError(RuntimeError):
    """Base error for contest state and validation problems."""


class ContestNotFound(ContestError):
    """Raised when the guild has no contest in the requested state."""


class ContestStateError(ContestError):
    """Raised when an operation is invalid for the contest's current state."""


@dataclass(frozen=True)
class ContestRecord:
    contest_id: int
    guild_id: int
    title: str
    status: str
    anonymous: bool
    duration_hours: int
    created_by: int
    channel_id: Optional[int]
    intro_message_id: Optional[int]
    result_message_id: Optional[int]
    closes_at: Optional[str]
    created_at: str
    closed_at: Optional[str]


@dataclass(frozen=True)
class ContestEntryRecord:
    entry_id: int
    contest_id: int
    entry_number: int
    contestant_name: str
    filename: str
    content_type: str
    image_data: bytes
    created_by: int
    message_id: Optional[int]


@dataclass(frozen=True)
class VoteOutcome:
    contest_id: int
    contest_title: str
    entry_id: int
    entry_number: int
    previous_entry_number: Optional[int]
    already_selected: bool


@dataclass(frozen=True)
class ContestTally:
    entry: ContestEntryRecord
    votes: int


class ContestStore:
    """SQLite-backed contest storage serialized for safe async bot access."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _contest_from_row(row: sqlite3.Row) -> ContestRecord:
        return ContestRecord(
            contest_id=int(row["contest_id"]),
            guild_id=int(row["guild_id"]),
            title=str(row["title"]),
            status=str(row["status"]),
            anonymous=bool(row["anonymous"]),
            duration_hours=int(row["duration_hours"]),
            created_by=int(row["created_by"]),
            channel_id=(int(row["channel_id"]) if row["channel_id"] else None),
            intro_message_id=(
                int(row["intro_message_id"]) if row["intro_message_id"] else None
            ),
            result_message_id=(
                int(row["result_message_id"]) if row["result_message_id"] else None
            ),
            closes_at=(str(row["closes_at"]) if row["closes_at"] else None),
            created_at=str(row["created_at"]),
            closed_at=(str(row["closed_at"]) if row["closed_at"] else None),
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> ContestEntryRecord:
        keys = set(row.keys())
        image_data = row["image_data"] if "image_data" in keys else b""
        return ContestEntryRecord(
            entry_id=int(row["entry_id"]),
            contest_id=int(row["contest_id"]),
            entry_number=int(row["entry_number"]),
            contestant_name=str(row["contestant_name"]),
            filename=str(row["filename"]),
            content_type=str(row["content_type"] or "application/octet-stream"),
            image_data=bytes(image_data or b""),
            created_by=int(row["created_by"]),
            message_id=(int(row["message_id"]) if row["message_id"] else None),
        )

    def _initialize_sync(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contest_managers (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    added_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contests (
                    contest_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'open', 'closed', 'cancelled')),
                    anonymous INTEGER NOT NULL DEFAULT 1,
                    duration_hours INTEGER NOT NULL DEFAULT 72,
                    created_by INTEGER NOT NULL,
                    channel_id INTEGER,
                    intro_message_id INTEGER,
                    result_message_id INTEGER,
                    closes_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contests_one_active_per_guild
                ON contests(guild_id)
                WHERE status IN ('draft', 'open')
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contest_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contest_id INTEGER NOT NULL,
                    entry_number INTEGER NOT NULL,
                    contestant_name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    image_data BLOB NOT NULL,
                    created_by INTEGER NOT NULL,
                    message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (contest_id, entry_number),
                    FOREIGN KEY (contest_id) REFERENCES contests(contest_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS contest_votes (
                    contest_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    entry_id INTEGER NOT NULL,
                    voted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (contest_id, user_id),
                    FOREIGN KEY (contest_id) REFERENCES contests(contest_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (entry_id) REFERENCES contest_entries(entry_id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_contest_entries_contest
                ON contest_entries(contest_id, entry_number)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_contest_votes_entry
                ON contest_votes(entry_id)
                """
            )
            connection.commit()

    async def initialize(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _is_manager_sync(self, guild_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM contest_managers
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        return row is not None

    async def is_manager(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._is_manager_sync, guild_id, user_id)

    def _add_manager_sync(self, guild_id: int, user_id: int, added_by: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO contest_managers (guild_id, user_id, added_by)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET added_by = excluded.added_by
                """,
                (guild_id, user_id, added_by),
            )
            connection.commit()

    async def add_manager(self, guild_id: int, user_id: int, added_by: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._add_manager_sync, guild_id, user_id, added_by
            )

    def _remove_manager_sync(self, guild_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM contest_managers
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    async def remove_manager(self, guild_id: int, user_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._remove_manager_sync, guild_id, user_id
            )

    def _list_managers_sync(self, guild_id: int) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM contest_managers
                WHERE guild_id = ? ORDER BY created_at, user_id
                """,
                (guild_id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    async def list_managers(self, guild_id: int) -> List[int]:
        async with self._lock:
            return await asyncio.to_thread(self._list_managers_sync, guild_id)

    def _get_active_contest_sync(self, guild_id: int) -> Optional[ContestRecord]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM contests
                WHERE guild_id = ? AND status IN ('draft', 'open')
                ORDER BY contest_id DESC LIMIT 1
                """,
                (guild_id,),
            ).fetchone()
        return self._contest_from_row(row) if row else None

    async def get_active_contest(self, guild_id: int) -> Optional[ContestRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_active_contest_sync, guild_id)

    def _get_contest_sync(self, contest_id: int) -> Optional[ContestRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
        return self._contest_from_row(row) if row else None

    async def get_contest(self, contest_id: int) -> Optional[ContestRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._get_contest_sync, contest_id)

    def _create_contest_sync(
        self,
        guild_id: int,
        title: str,
        anonymous: bool,
        duration_hours: int,
        created_by: int,
    ) -> ContestRecord:
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT contest_id FROM contests
                WHERE guild_id = ? AND status IN ('draft', 'open')
                """,
                (guild_id,),
            ).fetchone()
            if active:
                raise ContestStateError(
                    "Finish or cancel the current contest before creating another."
                )
            cursor = connection.execute(
                """
                INSERT INTO contests (
                    guild_id, title, anonymous, duration_hours, created_by
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, title, int(anonymous), duration_hours, created_by),
            )
            contest_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            connection.commit()
        return self._contest_from_row(row)

    async def create_contest(
        self,
        guild_id: int,
        title: str,
        anonymous: bool,
        duration_hours: int,
        created_by: int,
    ) -> ContestRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_contest_sync,
                guild_id,
                title,
                anonymous,
                duration_hours,
                created_by,
            )

    def _add_entry_sync(
        self,
        guild_id: int,
        contestant_name: str,
        filename: str,
        content_type: str,
        image_data: bytes,
        created_by: int,
    ) -> ContestEntryRecord:
        with self._connect() as connection:
            contest_row = connection.execute(
                """
                SELECT * FROM contests
                WHERE guild_id = ? AND status = 'draft'
                ORDER BY contest_id DESC LIMIT 1
                """,
                (guild_id,),
            ).fetchone()
            if not contest_row:
                raise ContestNotFound("Create a draft contest before adding entries.")
            contest = self._contest_from_row(contest_row)
            number_row = connection.execute(
                """
                SELECT COALESCE(MAX(entry_number), 0) + 1 AS next_number
                FROM contest_entries WHERE contest_id = ?
                """,
                (contest.contest_id,),
            ).fetchone()
            entry_number = int(number_row["next_number"])
            cursor = connection.execute(
                """
                INSERT INTO contest_entries (
                    contest_id, entry_number, contestant_name, filename,
                    content_type, image_data, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contest.contest_id,
                    entry_number,
                    contestant_name,
                    filename,
                    content_type,
                    image_data,
                    created_by,
                ),
            )
            entry_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM contest_entries WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()
            connection.commit()
        return self._entry_from_row(row)

    async def add_entry(
        self,
        guild_id: int,
        contestant_name: str,
        filename: str,
        content_type: str,
        image_data: bytes,
        created_by: int,
    ) -> ContestEntryRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._add_entry_sync,
                guild_id,
                contestant_name,
                filename,
                content_type,
                image_data,
                created_by,
            )

    def _list_entries_sync(
        self, contest_id: int, include_image_data: bool
    ) -> List[ContestEntryRecord]:
        image_column = "image_data" if include_image_data else "NULL AS image_data"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT entry_id, contest_id, entry_number, contestant_name,
                       filename, content_type, {image_column}, created_by, message_id
                FROM contest_entries
                WHERE contest_id = ?
                ORDER BY entry_number
                """,
                (contest_id,),
            ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    async def list_entries(
        self, contest_id: int, include_image_data: bool = False
    ) -> List[ContestEntryRecord]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_entries_sync, contest_id, include_image_data
            )

    def _get_entry_sync(
        self, entry_id: int, include_image_data: bool
    ) -> Optional[ContestEntryRecord]:
        image_column = "image_data" if include_image_data else "NULL AS image_data"
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT entry_id, contest_id, entry_number, contestant_name,
                       filename, content_type, {image_column}, created_by, message_id
                FROM contest_entries WHERE entry_id = ?
                """,
                (entry_id,),
            ).fetchone()
        return self._entry_from_row(row) if row else None

    async def get_entry(
        self, entry_id: int, include_image_data: bool = False
    ) -> Optional[ContestEntryRecord]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_entry_sync, entry_id, include_image_data
            )

    def _remove_entry_sync(
        self, guild_id: int, entry_number: int
    ) -> ContestEntryRecord:
        with self._connect() as connection:
            contest_row = connection.execute(
                """
                SELECT * FROM contests
                WHERE guild_id = ? AND status = 'draft'
                ORDER BY contest_id DESC LIMIT 1
                """,
                (guild_id,),
            ).fetchone()
            if not contest_row:
                raise ContestNotFound("There is no draft contest to edit.")
            contest = self._contest_from_row(contest_row)
            entry_row = connection.execute(
                """
                SELECT * FROM contest_entries
                WHERE contest_id = ? AND entry_number = ?
                """,
                (contest.contest_id, entry_number),
            ).fetchone()
            if not entry_row:
                raise ContestNotFound(f"Entry #{entry_number} does not exist.")
            removed = self._entry_from_row(entry_row)
            connection.execute(
                "DELETE FROM contest_entries WHERE entry_id = ?",
                (removed.entry_id,),
            )

            remaining = connection.execute(
                """
                SELECT entry_id FROM contest_entries
                WHERE contest_id = ? ORDER BY entry_number
                """,
                (contest.contest_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE contest_entries SET entry_number = -entry_id
                WHERE contest_id = ?
                """,
                (contest.contest_id,),
            )
            connection.executemany(
                "UPDATE contest_entries SET entry_number = ? WHERE entry_id = ?",
                [
                    (new_number, int(row["entry_id"]))
                    for new_number, row in enumerate(remaining, start=1)
                ],
            )
            connection.commit()
        return removed

    async def remove_entry(
        self, guild_id: int, entry_number: int
    ) -> ContestEntryRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._remove_entry_sync, guild_id, entry_number
            )

    def _cancel_draft_sync(self, guild_id: int) -> ContestRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM contests
                WHERE guild_id = ? AND status = 'draft'
                ORDER BY contest_id DESC LIMIT 1
                """,
                (guild_id,),
            ).fetchone()
            if not row:
                raise ContestNotFound("There is no draft contest to cancel.")
            contest = self._contest_from_row(row)
            connection.execute(
                """
                UPDATE contests SET status = 'cancelled', closed_at = CURRENT_TIMESTAMP
                WHERE contest_id = ?
                """,
                (contest.contest_id,),
            )
            connection.commit()
        return contest

    async def cancel_draft(self, guild_id: int) -> ContestRecord:
        async with self._lock:
            return await asyncio.to_thread(self._cancel_draft_sync, guild_id)

    def _publish_contest_sync(
        self,
        contest_id: int,
        channel_id: int,
        intro_message_id: int,
        closes_at: str,
        entry_message_ids: Dict[int, int],
    ) -> ContestRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            if not row:
                raise ContestNotFound("The contest no longer exists.")
            contest = self._contest_from_row(row)
            if contest.status != "draft":
                raise ContestStateError("Only a draft contest can be published.")
            entry_rows = connection.execute(
                "SELECT entry_id FROM contest_entries WHERE contest_id = ?",
                (contest_id,),
            ).fetchall()
            entry_ids = {int(entry_row["entry_id"]) for entry_row in entry_rows}
            if len(entry_ids) < 2:
                raise ContestStateError("Add at least two entries before publishing.")
            if entry_ids != set(entry_message_ids):
                raise ContestStateError("Every contest entry needs a published image card.")

            connection.execute(
                """
                UPDATE contests
                SET status = 'open', channel_id = ?, intro_message_id = ?,
                    closes_at = ?
                WHERE contest_id = ?
                """,
                (channel_id, intro_message_id, closes_at, contest_id),
            )
            connection.executemany(
                "UPDATE contest_entries SET message_id = ? WHERE entry_id = ?",
                [
                    (message_id, entry_id)
                    for entry_id, message_id in entry_message_ids.items()
                ],
            )
            updated_row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            connection.commit()
        return self._contest_from_row(updated_row)

    async def publish_contest(
        self,
        contest_id: int,
        channel_id: int,
        intro_message_id: int,
        closes_at: str,
        entry_message_ids: Dict[int, int],
    ) -> ContestRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._publish_contest_sync,
                contest_id,
                channel_id,
                intro_message_id,
                closes_at,
                entry_message_ids,
            )

    def _record_vote_sync(self, entry_id: int, user_id: int) -> VoteOutcome:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT e.entry_id, e.entry_number, e.contest_id,
                       c.title AS contest_title, c.status
                FROM contest_entries e
                JOIN contests c ON c.contest_id = e.contest_id
                WHERE e.entry_id = ?
                """,
                (entry_id,),
            ).fetchone()
            if not row:
                raise ContestNotFound("That contest entry no longer exists.")
            if row["status"] != "open":
                raise ContestStateError("Voting for this contest is closed.")

            previous = connection.execute(
                """
                SELECT e.entry_id, e.entry_number
                FROM contest_votes v
                JOIN contest_entries e ON e.entry_id = v.entry_id
                WHERE v.contest_id = ? AND v.user_id = ?
                """,
                (int(row["contest_id"]), user_id),
            ).fetchone()
            already_selected = bool(
                previous and int(previous["entry_id"]) == int(row["entry_id"])
            )
            if not already_selected:
                connection.execute(
                    """
                    INSERT INTO contest_votes (contest_id, user_id, entry_id, voted_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(contest_id, user_id) DO UPDATE SET
                        entry_id = excluded.entry_id,
                        voted_at = CURRENT_TIMESTAMP
                    """,
                    (int(row["contest_id"]), user_id, int(row["entry_id"])),
                )
                connection.commit()

        return VoteOutcome(
            contest_id=int(row["contest_id"]),
            contest_title=str(row["contest_title"]),
            entry_id=int(row["entry_id"]),
            entry_number=int(row["entry_number"]),
            previous_entry_number=(
                int(previous["entry_number"]) if previous else None
            ),
            already_selected=already_selected,
        )

    async def record_vote(self, entry_id: int, user_id: int) -> VoteOutcome:
        async with self._lock:
            return await asyncio.to_thread(self._record_vote_sync, entry_id, user_id)

    def _vote_count_sync(self, contest_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM contest_votes WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
        return int(row["count"])

    async def vote_count(self, contest_id: int) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._vote_count_sync, contest_id)

    def _close_contest_sync(
        self, contest_id: int
    ) -> Tuple[ContestRecord, List[ContestTally]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            if not row:
                raise ContestNotFound("The contest no longer exists.")
            contest = self._contest_from_row(row)
            if contest.status != "open":
                raise ContestStateError("That contest is not open for voting.")

            connection.execute(
                """
                UPDATE contests
                SET status = 'closed', closed_at = CURRENT_TIMESTAMP
                WHERE contest_id = ? AND status = 'open'
                """,
                (contest_id,),
            )
            tally_rows = connection.execute(
                """
                SELECT e.*, COUNT(v.user_id) AS vote_count
                FROM contest_entries e
                LEFT JOIN contest_votes v ON v.entry_id = e.entry_id
                WHERE e.contest_id = ?
                GROUP BY e.entry_id
                ORDER BY vote_count DESC, e.entry_number ASC
                """,
                (contest_id,),
            ).fetchall()
            updated_row = connection.execute(
                "SELECT * FROM contests WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            connection.commit()
        tallies = [
            ContestTally(
                entry=self._entry_from_row(tally_row),
                votes=int(tally_row["vote_count"]),
            )
            for tally_row in tally_rows
        ]
        return self._contest_from_row(updated_row), tallies

    async def close_contest(
        self, contest_id: int
    ) -> Tuple[ContestRecord, List[ContestTally]]:
        async with self._lock:
            return await asyncio.to_thread(self._close_contest_sync, contest_id)

    def _expired_open_contests_sync(self, now_iso: str) -> List[ContestRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM contests
                WHERE status = 'open' AND closes_at IS NOT NULL AND closes_at <= ?
                ORDER BY closes_at
                """,
                (now_iso,),
            ).fetchall()
        return [self._contest_from_row(row) for row in rows]

    async def expired_open_contests(self, now_iso: str) -> List[ContestRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._expired_open_contests_sync, now_iso)

    def _open_entry_message_ids_sync(self) -> List[Tuple[int, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.entry_id, e.message_id
                FROM contest_entries e
                JOIN contests c ON c.contest_id = e.contest_id
                WHERE c.status = 'open' AND e.message_id IS NOT NULL
                """
            ).fetchall()
        return [
            (int(row["entry_id"]), int(row["message_id"])) for row in rows
        ]

    async def open_entry_message_ids(self) -> List[Tuple[int, int]]:
        async with self._lock:
            return await asyncio.to_thread(self._open_entry_message_ids_sync)

    def _mark_result_message_sync(self, contest_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE contests SET result_message_id = ? WHERE contest_id = ?
                """,
                (message_id, contest_id),
            )
            connection.commit()

    async def mark_result_message(self, contest_id: int, message_id: int) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_result_message_sync, contest_id, message_id
            )
