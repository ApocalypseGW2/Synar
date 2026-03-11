from datetime import datetime, timezone
from typing import Literal
import discord
from discord import app_commands

from storage.db import get_connection
from helpers import (
    parse_unix_timestamp,
    default_max_slots,
    send_invalid_timestamp,
    normalize_announcement_message,
    build_event_announcement_content,
)
from embeds import build_signup_embed
from views import SignupView, EventRolePickerView, ScheduleIntervalView, ScheduleEditRolePickerView



def register_commands(client: discord.Client):
    client.tree.add_command(create)
    client.tree.add_command(delete)
    client.tree.add_command(edit)

create = app_commands.Group(name="create", description="Create events and schedules")

@create.command(name="event", description="Create a one-time event")
@app_commands.describe(
    title="Title of the event",
    category="Type of event",
    timestamp="Date of the event (Unix timestamp)",
    duration="Planned duration (in hours) shown in the signup embed",
    signup_mode="Restrictions for users to sign up",
    ping_roles="Ping the allowed roles in the event post",
    message="Optional text shown above the signup embed",
)
async def create_event(
    interaction: discord.Interaction,
    title: str,
    category: Literal["Raids", "Dungeons", "Fractals", "Other"],
    timestamp: str,
    duration: int,
    signup_mode: Literal["Open", "Role"], #, "Invite"],
    ping_roles: Literal["Yes", "No"] = "No",
    message: str | None = None,
) -> None:
    ts = parse_unix_timestamp(timestamp)
    if ts is None:
        await send_invalid_timestamp(interaction)
        return
    if duration <= 0:
        await interaction.response.send_message("Duration must be greater than 0.", ephemeral=True)
        return

    ping_allowed_roles = ping_roles == "Yes"
    announcement_message = normalize_announcement_message(message)

    if signup_mode != "Role":
        ping_allowed_roles = False

    if signup_mode == "Role":
        view = EventRolePickerView(
            title=title,
            category=category,
            timestamp=ts,
            duration=duration,
            signup_mode=signup_mode,
            creator_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            ping_roles=ping_allowed_roles,
            announcement_message=announcement_message,
        )
        await interaction.response.send_message(
            "Select allowed roles (max 5):",
            view=view,
            ephemeral=True,
        )
        return

    max_slots = default_max_slots(category)
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())

    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO events (
                guild_id,
                channel_id,
                creator_id,
                title,
                category,
                duration,
                signup_mode,
                max_slots,
                timestamp,
                ping_roles,
                announcement_message,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                title,
                category,
                duration,
                signup_mode.lower(),
                max_slots,
                ts,
                int(ping_allowed_roles),
                announcement_message,
                now_ts,
            ),
        )
        event_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    embed = await build_signup_embed(
        guild=interaction.guild,
        title=title,
        category=category,
        timestamp=ts,
        duration=duration,
        signup_mode=signup_mode,
        max_slots=default_max_slots(category),
        creator_id=interaction.user.id,
        event_id=event_id,
        allowed_role_ids=None,
        schedule_id=None,
    )

    view = SignupView(event_id)
    content = build_event_announcement_content(
        ping_roles=ping_allowed_roles,
        allowed_role_ids=None,
        message=announcement_message,
    )
    allowed_mentions = discord.AllowedMentions(
        roles=ping_allowed_roles,
        users=False,
        everyone=False,
    )
    await interaction.response.send_message(
        content=content,
        embed=embed,
        view=view,
        allowed_mentions=allowed_mentions,
    )

    msg = await interaction.original_response()
    await msg.create_thread(name=f"{title} Discussion")


@create.command(name="schedule", description="Create a recurring schedule")
@app_commands.describe(
    title="Title of the event",
    category="Event type",
    frequency="daily or weekly",
    time="Use @time to pick a timestamp (e. g. @time -> Enter -> 22:15 -> Enter)",
    duration="Planned duration (in hours) shown in each signup embed",
    signup_mode="Restrictions for users to sign up",
    ping_roles="Ping the allowed roles in each scheduled event post",
    message="Optional text shown above each scheduled signup embed",
    start_date="Use @time to pick a timestamp for your starting date of your schedule (defaults to instantly)",
    end_date="Use @time to pick a timestamp for your ending date of your schedule.",
)
async def create_schedule(
    interaction: discord.Interaction,
    title: str,
    category: Literal["Raids", "Dungeons", "Fractals", "Other"],
    frequency: Literal["daily", "weekly"],
    time: str,
    duration: int,
    signup_mode: Literal["Open", "Role"], #", Invite"],
    ping_roles: Literal["Yes", "No"] = "No",
    message: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    ping_allowed_roles = ping_roles == "Yes"
    announcement_message = normalize_announcement_message(message)

    if signup_mode != "Role":
        ping_allowed_roles = False

    view = ScheduleIntervalView(
        title=title,
        category=category,
        frequency=frequency,
        time=time,
        duration=duration,
        signup_mode=signup_mode,
        ping_roles=ping_allowed_roles,
        announcement_message=announcement_message,
        start_date=start_date,
        end_date=end_date
    )
    await interaction.response.send_message(
        "Pick an interval:", view=view, ephemeral=True
    )


delete = app_commands.Group(name="delete", description="Delete things")

@delete.command(name="schedule", description="Remove your schedule")
@app_commands.describe(id="ID of the schedule")
async def remove_schedule(interaction: discord.Interaction, id: int) -> None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, creator_id FROM schedules WHERE id = ?",
            (id,),
        ).fetchone()

        if not row:
            await interaction.response.send_message("Schedule not found.", ephemeral=True)
            return

        is_admin = False
        if isinstance(interaction.user, discord.Member):
            is_admin = interaction.user.guild_permissions.administrator

        if row["creator_id"] != interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "Only the creator or a server admin can remove this schedule.",
                ephemeral=True,
            )
            return

        conn.execute("DELETE FROM schedule_allowed_roles WHERE schedule_id = ?", (id,))
        conn.execute("DELETE FROM schedules WHERE id = ?", (id,))

        # Optional: delete future events created by this schedule
        conn.execute(
            "DELETE FROM events WHERE schedule_id = ? AND timestamp > ?",
            (id, int(datetime.now(tz=timezone.utc).timestamp())),
        )

        conn.commit()
    finally:
        conn.close()

    await interaction.response.send_message("Schedule removed.", ephemeral=True)


edit = app_commands.Group(name="edit", description="Edit things")

@edit.command(name="schedule", description="Edit a schedule")
@app_commands.describe(
    id="Schedule ID",
    title="New title",
    category="New category",
    frequency="daily or weekly",
    interval="New interval (1, 2, 3...)",
    day_of_week="0=Mon ... 6=Sun (weekly only)",
    time="Unix timestamp (@time)",
    duration="Planned duration (in hours) shown in each signup embed",
    start_date="Unix timestamp (@time)",
    end_date="Unix timestamp (@time)",
    signup_mode="Open/Role/Invite",
    ping_roles="Ping the allowed roles in each scheduled event post",
    message="Optional text shown above each scheduled signup embed",
)
async def edit_schedule(
    interaction: discord.Interaction,
    id: int,
    title: str | None = None,
    category: Literal["Raids", "Dungeons", "Fractals", "Other"] | None = None,
    frequency: Literal["daily", "weekly"] | None = None,
    interval: int | None = None,
    day_of_week: int | None = None,
    time: str | None = None,
    duration: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    signup_mode: Literal["Open", "Role"] | None = None, #, "Invite"]
    ping_roles: Literal["Yes", "No"] | None = None,
    message: str | None = None,
) -> None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ?",
            (id,),
        ).fetchone()

        if not row:
            await interaction.response.send_message("Schedule not found.", ephemeral=True)
            return

        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if row["creator_id"] != interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "Only the creator or an admin can edit this schedule.",
                ephemeral=True,
            )
            return

        # merge values
        new_title = title if title is not None else row["title"]
        new_category = category if category is not None else row["category"]
        new_frequency = frequency if frequency is not None else row["frequency"]
        new_interval = interval if interval is not None else row["interval"]
        new_day_of_week = day_of_week if day_of_week is not None else row["day_of_week"]
        new_duration = duration if duration is not None else row["duration"]
        new_signup_mode = (signup_mode or row["signup_mode"] or "open").lower()
        new_ping_roles = bool(row["ping_roles"])
        if ping_roles is not None:
            new_ping_roles = ping_roles == "Yes"
        new_announcement_message = (
            normalize_announcement_message(message)
            if message is not None
            else row["announcement_message"]
        )

        if new_duration is not None and new_duration <= 0:
            await interaction.response.send_message("Duration must be greater than 0.", ephemeral=True)
            return

        if new_signup_mode != "role":
            new_ping_roles = False

        # parse timestamps
        new_time_ts = row["time_of_day"]
        if time is not None:
            ts = parse_unix_timestamp(time)
            if ts is None:
                await interaction.response.send_message("Time must be a valid Unix timestamp.", ephemeral=True)
                return
            new_time_ts = ts - (ts % 60)

        new_start_ts = row["start_date"]
        if start_date is not None:
            ts = parse_unix_timestamp(start_date)
            if ts is None:
                await interaction.response.send_message("start_date must be a valid Unix timestamp.", ephemeral=True)
                return
            new_start_ts = ts - (ts % 60)

        new_end_ts = row["end_date"]
        if end_date is not None:
            ts = parse_unix_timestamp(end_date)
            if ts is None:
                await interaction.response.send_message("end_date must be a valid Unix timestamp.", ephemeral=True)
                return
            new_end_ts = ts - (ts % 60)

        if new_end_ts is not None and new_start_ts is not None and new_end_ts <= new_start_ts:
            await interaction.response.send_message("end_date must be after start_date.", ephemeral=True)
            return

        if new_frequency == "weekly" and new_day_of_week is None:
            await interaction.response.send_message("Weekly schedules need a day_of_week.", ephemeral=True)
            return

        # recompute next_run_at if time/frequency/interval/day/start changed
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        step_seconds = 86400 if new_frequency == "daily" else 7 * 86400
        step_seconds *= new_interval

        first_run_at = new_time_ts
        while new_start_ts is not None and first_run_at < new_start_ts:
            first_run_at += step_seconds
        while first_run_at < now_ts:
            first_run_at += step_seconds

        # If Role signup mode -> open picker
        if new_signup_mode == "role":
            view = ScheduleEditRolePickerView(
                schedule_id=id,
                title=new_title,
                category=new_category,
                frequency=new_frequency,
                interval_value=new_interval,
                day_of_week=new_day_of_week,
                time_ts=new_time_ts,
                duration=new_duration,
                start_ts=new_start_ts,
                end_ts=new_end_ts,
                next_run_at=first_run_at,
                signup_mode=new_signup_mode,
                ping_roles=new_ping_roles,
                announcement_message=new_announcement_message,
            )
            await interaction.response.send_message(
                "Select allowed roles (max 5):",
                view=view,
                ephemeral=True
            )
            return

        # Otherwise update directly
        conn.execute(
            """
            UPDATE schedules
            SET title = ?,
                category = ?,
                frequency = ?,
                interval = ?,
                day_of_week = ?,
                time_of_day = ?,
                duration = ?,
                start_date = ?,
                end_date = ?,
                signup_mode = ?,
                ping_roles = ?,
                announcement_message = ?,
                next_run_at = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_category,
                new_frequency,
                new_interval,
                new_day_of_week,
                new_time_ts,
                new_duration,
                new_start_ts,
                new_end_ts,
                new_signup_mode,
                int(new_ping_roles),
                new_announcement_message,
                first_run_at,
                id,
            ),
        )

        # If leaving Role, clear allowed roles
        conn.execute("DELETE FROM schedule_allowed_roles WHERE schedule_id = ?", (id,))

        conn.commit()
    finally:
        conn.close()

    await interaction.response.send_message("Schedule updated.", ephemeral=True)
