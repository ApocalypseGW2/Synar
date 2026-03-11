import re
from datetime import datetime, timezone
from storage.db import get_connection
import discord


DISCORD_TIMESTAMP_RE = re.compile(r"<t:(\d+)(?::[a-zA-Z])?>")

def parse_unix_timestamp(value: str) -> int | None:
    """
    Parse and validate a Unix timestamp (seconds).

    Returns the timestamp as int if valid, otherwise None.
    """
    
    raw = value.strip()

    match = DISCORD_TIMESTAMP_RE.fullmatch(raw)
    if match:
        raw = match.group(1)

    try:
        ts = int(raw)
    except ValueError:
        return None

    if ts < 946684800:
        return None

    max_future = int(datetime.now(tz=timezone.utc).timestamp()) + 10 * 365 * 24 * 3600
    if ts > max_future:
        return None

    return ts


def default_max_slots(category: str) -> int:
    if category == "Raids":
        return 10
    if category in ("Dungeons", "Fractals"):
        return 5
    return 50


def user_has_allowed_role(member: discord.Member | None, allowed_role_ids: list[int]) -> bool:
    if not member:
        return False
    member_role_ids = {r.id for r in member.roles}
    return any(rid in member_role_ids for rid in allowed_role_ids)


def get_allowed_role_ids(conn, event_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT role_id FROM event_allowed_roles WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    return [r[0] for r in rows]


def count_signups(conn, event_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM event_signups WHERE event_id = ? AND status = 'available'",
        (event_id,),
    ).fetchone()
    return row[0] if row else 0


async def send_invalid_timestamp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Invalid timestamp",
        description=(
            "The timestamp you entered is not a valid Unix timestamp.\n\n"
            "Generate one here:\n"
            "https://hammertime.cyou"
        ),
        color=discord.Color.red(),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


def normalize_announcement_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = message.strip()
    return normalized or None


def build_event_announcement_content(
    *,
    ping_roles: bool,
    allowed_role_ids: list[int] | None,
    message: str | None,
) -> str | None:
    parts: list[str] = []

    if ping_roles and allowed_role_ids:
        parts.append(" ".join(f"<@&{role_id}>" for role_id in allowed_role_ids))

    normalized_message = normalize_announcement_message(message)
    if normalized_message:
        parts.append(normalized_message)

    if not parts:
        return None

    return "\n".join(parts)


async def insert_schedule(*, interaction, title, category, frequency, interval_value,
                          day_of_week, time_ts, start_ts, end_ts, next_run_at,
                          duration,
                          signup_mode, allowed_role_ids, ping_roles, announcement_message):
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO schedules (
                guild_id, channel_id, creator_id,
                title, category,
                duration,
                frequency, interval, day_of_week,
                time_of_day, start_date, end_date,
                signup_mode, ping_roles, announcement_message,
                created_at, next_run_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                title,
                category,
                duration,
                frequency,
                interval_value,
                day_of_week,
                time_ts,
                start_ts,
                end_ts,
                signup_mode.lower(),
                int(ping_roles),
                announcement_message,
                now_ts,
                next_run_at,
            ),
        )
        schedule_id = cursor.lastrowid

        if allowed_role_ids:
            for role_id in allowed_role_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO schedule_allowed_roles (schedule_id, role_id) VALUES (?, ?)",
                    (schedule_id, role_id),
                )

        conn.commit()
    finally:
        conn.close()
