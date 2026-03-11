import logging
import time
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import tasks

from config import ENV, DISCORD_TOKEN, DEV_GUILD_ID, LOG_LEVEL, SYNC_COMMANDS, CLEAR_COMMANDS
from storage.db import init_db, get_connection
from helpers import default_max_slots, build_event_announcement_content
from embeds import build_signup_embed
from views import SignupView
from commands import register_commands



class MyClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        register_commands(self)

        if SYNC_COMMANDS:
            if ENV == "dev":
                guild = discord.Object(id=int(DEV_GUILD_ID))
                if CLEAR_COMMANDS:
                    self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
                log.info("Synced commands globally")


        now_ts = int(time.time())
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id FROM events WHERE timestamp > ?",
                (now_ts,),
            ).fetchall()
        finally:
            conn.close()

        for (event_id,) in rows:
            self.add_view(SignupView(event_id))
        scheduler_loop.start()
        reminder_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)


client = MyClient()


@tasks.loop(minutes=1)
async def scheduler_loop():
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM schedules
            WHERE end_date IS NULL OR end_date > ?
            """,
            (now_ts,),
        ).fetchall()

        for row in rows:
            step = 86400 if row["frequency"] == "daily" else 7 * 86400
            step *= row["interval"]

            next_run = row["next_run_at"]
            while next_run <= now_ts:
                next_run += step

            if row["end_date"] is not None and next_run > row["end_date"]:
                continue

            if next_run != row["next_run_at"]:
                conn.execute(
                    "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                    (next_run, row["id"]),
                )

            exists = conn.execute(
                """
                SELECT 1 FROM events
                WHERE schedule_id = ? AND timestamp = ?
                LIMIT 1
                """,
                (row["id"], next_run)
            ).fetchone()

            if not exists:
                signup_mode = (row["signup_mode"] or "open").lower()
                max_slots = default_max_slots(row["category"])

                allowed_role_ids = []
                if signup_mode == "role":
                    role_rows = conn.execute(
                        "SELECT role_id FROM schedule_allowed_roles WHERE schedule_id = ?",
                        (row["id"],),
                    ).fetchall()
                    allowed_role_ids = [r[0] for r in role_rows]

                conn.execute(
                    """
                    INSERT INTO events (
                        schedule_id,
                        guild_id, channel_id, creator_id,
                        title, category, duration, signup_mode, max_slots,
                        timestamp, ping_roles, announcement_message, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["guild_id"],
                        row["channel_id"],
                        row["creator_id"],
                        row["title"],
                        row["category"],
                        row["duration"],
                        signup_mode,
                        max_slots,
                        next_run,
                        int(bool(row["ping_roles"])),
                        row["announcement_message"],
                        now_ts
                    ),
                )
                event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                if signup_mode == "role":
                    for role_id in allowed_role_ids:
                        conn.execute(
                            "INSERT OR IGNORE INTO event_allowed_roles (event_id, role_id) VALUES (?, ?)",
                            (event_id, role_id),
                        )

                channel = client.get_channel(row["channel_id"])
                if channel is None:
                    channel = await client.fetch_channel(row["channel_id"])

                embed = await build_signup_embed(
                    guild=getattr(channel, "guild", None),
                    title=row["title"],
                    category=row["category"],
                    timestamp=next_run,
                    duration=row["duration"],
                    signup_mode=signup_mode,
                    max_slots=max_slots,
                    creator_id=row["creator_id"],
                    event_id=event_id,
                    allowed_role_ids=allowed_role_ids,
                    schedule_id=row["id"],
                )
                content = build_event_announcement_content(
                    ping_roles=bool(row["ping_roles"]) and signup_mode == "role",
                    allowed_role_ids=allowed_role_ids,
                    message=row["announcement_message"],
                )
                message = await channel.send(
                    content=content,
                    embed=embed,
                    view=SignupView(event_id),
                    allowed_mentions=discord.AllowedMentions(
                        roles=bool(row["ping_roles"]) and signup_mode == "role",
                        users=False,
                        everyone=False,
                    ),
                )
                await message.create_thread(name=f"{row['title']} Discussion")

        conn.commit()
    finally:
        conn.close()


@tasks.loop(minutes=1)
async def reminder_loop():
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.user_id, r.event_id, e.title, e.timestamp
            FROM event_reminders r
            JOIN events e ON e.id = r.event_id
            WHERE r.remind_at <= ?
            """,
            (now_ts,),
        ).fetchall()

        for r in rows:
            try:
                user = client.get_user(r["user_id"]) or await client.fetch_user(r["user_id"])
                await user.send(
                    f"⏰ Reminder: **{r['title']}** starts at <t:{r['timestamp']}:F> (<t:{r['timestamp']}:R>)."
                )
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

            conn.execute("DELETE FROM event_reminders WHERE id = ?", (r["id"],))

        conn.commit()
    finally:
        conn.close()




def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


log = logging.getLogger("synar")


def main() -> None:
    setup_logging()
    init_db()
    log.info("Starting Synar (env=%s)", ENV)
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
