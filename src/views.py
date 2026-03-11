from datetime import datetime, timezone
import discord

from storage.db import get_connection
from helpers import (
    parse_unix_timestamp,
    default_max_slots,
    user_has_allowed_role,
    get_allowed_role_ids,
    insert_schedule,
    build_event_announcement_content,
)

from embeds import build_signup_embed



DAILY_INTERVALS = {
    "daily": 1,
    "every 2 days": 2,
    "every 3 days": 3,
    "every 4 days": 4,
    "every 5 days": 5,
    "every 6 days": 6,
}
WEEKLY_INTERVALS = {
    "weekly": 1,
    "every 2 weeks": 2,
    "every 3 weeks": 3,
    "every 4 weeks": 4,
}
WEEKDAY_OPTIONS = [
    discord.SelectOption(label="Monday", value="0"),
    discord.SelectOption(label="Tuesday", value="1"),
    discord.SelectOption(label="Wednesday", value="2"),
    discord.SelectOption(label="Thursday", value="3"),
    discord.SelectOption(label="Friday", value="4"),
    discord.SelectOption(label="Saturday", value="5"),
    discord.SelectOption(label="Sunday", value="6"),
]

REMIND_OPTIONS = [
    discord.SelectOption(label="10 minutes before", value="600"),
    discord.SelectOption(label="30 minutes before", value="1800"),
    discord.SelectOption(label="1 hour before", value="3600"),
    discord.SelectOption(label="6 hours before", value="21600"),
]


class SignupView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.label == "Sign Up":
                    child.custom_id = f"signup:avail:{event_id}"
                if child.label == "Decline":
                    child.custom_id = f"signup:decline:{event_id}"
                if child.label == "Maybe":
                    child.custom_id = f"signup:maybe:{event_id}"
                if child.label == "Remind Me":
                    child.custom_id = f"signup:remind:{event_id}"
                    

    async def _set_status(self, interaction: discord.Interaction, status: str):
        conn = get_connection()
        try:
            event = conn.execute(
                "SELECT * FROM events WHERE id = ?",
                (self.event_id,),
            ).fetchone()
            if not event:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return

            allowed_roles = get_allowed_role_ids(conn, self.event_id)
            signup_mode = (event["signup_mode"] or "open").lower()

            if signup_mode == "invite":
                if interaction.user.id != event["creator_id"]:
                    await interaction.response.send_message("Invite-only. Ask the host.", ephemeral=True)
                    return

            if signup_mode == "role":
                member = interaction.user if isinstance(interaction.user, discord.Member) else None
                if member is None and interaction.guild:
                    member = await interaction.guild.fetch_member(interaction.user.id)
                if not user_has_allowed_role(member, allowed_roles):
                    await interaction.response.send_message("You don't have the required role(s).", ephemeral=True)
                    return

            if status == "available":
                current = conn.execute(
                    "SELECT COUNT(*) FROM event_signups WHERE event_id = ? AND status = 'available'",
                    (self.event_id,),
                ).fetchone()[0]
                max_slots = event["max_slots"]
                if current >= max_slots:
                    await interaction.response.send_message("Event is full.", ephemeral=True)
                    return

            conn.execute(
                """
                INSERT OR REPLACE INTO event_signups (event_id, user_id, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (self.event_id, interaction.user.id, status, int(datetime.now(tz=timezone.utc).timestamp())),
            )
            conn.commit()
        finally:
            conn.close()

        embed = await build_signup_embed(
            guild=interaction.guild,
            title=event["title"],
            category=event["category"],
            timestamp=event["timestamp"],
            duration=event["duration"],
            signup_mode=event["signup_mode"],
            max_slots=event["max_slots"],
            creator_id=event["creator_id"],
            event_id=self.event_id,
            allowed_role_ids=allowed_roles,
            schedule_id=event["schedule_id"],
        )

        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.green, row=0)
    async def signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "available")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, row=0)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "unavailable")

    @discord.ui.button(label="Maybe", style=discord.ButtonStyle.gray, row=0)
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_status(interaction, "maybe")

    @discord.ui.button(label="Remind Me", style=discord.ButtonStyle.secondary, emoji="🔔", row=1)
    async def remind_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "When should I remind you?",
            view=ReminderSelectView(self.event_id),
            ephemeral=True
        )


class ReminderSelectView(discord.ui.View):
    def __init__(self, event_id: int):
        super().__init__(timeout=120)
        self.event_id = event_id

    @discord.ui.select(
        placeholder="Choose when to be reminded",
        options=REMIND_OPTIONS
    )
    async def select_reminder(self, interaction: discord.Interaction, select: discord.ui.Select):
        seconds_before = int(select.values[0])

        conn = get_connection()
        try:
            event = conn.execute(
                "SELECT id, title, timestamp FROM events WHERE id = ?",
                (self.event_id,),
            ).fetchone()

            if not event:
                await interaction.response.send_message("Event not found.", ephemeral=True)
                return

            remind_at = int(event["timestamp"]) - seconds_before
            now_ts = int(datetime.now(tz=timezone.utc).timestamp())
            if remind_at <= now_ts:
                await interaction.response.send_message("That event is too soon for that reminder.", ephemeral=True)
                return

            conn.execute(
                """
                INSERT OR IGNORE INTO event_reminders (event_id, user_id, remind_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event["id"],
                    interaction.user.id,
                    remind_at,
                    now_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        await interaction.response.edit_message(
            content=f"✅ I will send you a message {seconds_before // 60} minutes before.",
            view=None
        )


class EventRolePickerView(discord.ui.View):
    def __init__(
        self,
        *,
        title: str,
        category: str,
        timestamp: int,
        duration: int,
        signup_mode: str,
        creator_id: int,
        guild_id: int,
        channel_id: int,
        ping_roles: bool,
        announcement_message: str | None,
    ):
        super().__init__(timeout=300)
        self.title = title
        self.category = category
        self.timestamp = timestamp
        self.duration = duration
        self.signup_mode = signup_mode
        self.creator_id = creator_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ping_roles = ping_roles
        self.announcement_message = announcement_message
        self.selected_role_ids: list[int] = []

    @discord.ui.select(
        placeholder="Select allowed roles (max 5)",
        min_values=1,
        max_values=5,
        cls=discord.ui.RoleSelect,
    )
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.selected_role_ids = [r.id for r in select.values]
        await interaction.response.defer()

    @discord.ui.button(label="Create Event", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_role_ids:
            await interaction.response.send_message("Please select at least one role.", ephemeral=True)
            return

        max_slots = default_max_slots(self.category)
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
                    self.guild_id,
                    self.channel_id,
                    self.creator_id,
                    self.title,
                    self.category,
                    self.duration,
                    self.signup_mode.lower(),
                    max_slots,
                    self.timestamp,
                    int(self.ping_roles),
                    self.announcement_message,
                    now_ts,
                ),
            )
            event_id = cursor.lastrowid

            for role_id in self.selected_role_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO event_allowed_roles (event_id, role_id) VALUES (?, ?)",
                    (event_id, role_id),
                )

            conn.commit()
        finally:
            conn.close()

        embed = await build_signup_embed(
            guild=interaction.guild,
            title=self.title,
            category=self.category,
            timestamp=self.timestamp,
            duration=self.duration,
            signup_mode=self.signup_mode,
            max_slots=default_max_slots(self.category),
            creator_id=self.creator_id,
            event_id=event_id,
            allowed_role_ids=self.selected_role_ids,
            schedule_id=None,
        )

        channel = interaction.client.get_channel(self.channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(self.channel_id)

        view = SignupView(event_id)
        content = build_event_announcement_content(
            ping_roles=self.ping_roles,
            allowed_role_ids=self.selected_role_ids,
            message=self.announcement_message,
        )
        message = await channel.send(
            content=content,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(
                roles=self.ping_roles,
                users=False,
                everyone=False,
            ),
        )
        await message.create_thread(name=f"{self.title} Discussion")
        await interaction.response.edit_message(content="Event created.", view=None)


class ScheduleIntervalView(discord.ui.View):
    def __init__(
        self,
        *,
        title: str,
        category: str,
        frequency: str,
        time: str,
        duration: int,
        signup_mode: str,
        ping_roles: bool,
        announcement_message: str | None,
        start_date: str | None,
        end_date: str | None
    ):
        super().__init__(timeout=300)
        self.title = title
        self.category = category
        self.frequency = frequency
        self.time = time
        self.duration = duration
        self.signup_mode = signup_mode
        self.ping_roles = ping_roles
        self.announcement_message = announcement_message
        self.start_date = start_date
        self.end_date = end_date
        self.interval_value: int | None = None
        self.day_of_week: int | None = None

        options = []
        if frequency == "daily":
            for label in DAILY_INTERVALS.keys():
                options.append(discord.SelectOption(label=label))
        else:
            for label in WEEKLY_INTERVALS.keys():
                options.append(discord.SelectOption(label=label))

        self.interval_select.options = options

        self.weekday_select.options = WEEKDAY_OPTIONS
        if self.frequency != "weekly":
            self.remove_item(self.weekday_select)

    @discord.ui.select(placeholder="Select interval")
    async def interval_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        label = select.values[0]
        if self.frequency == "daily":
            self.interval_value = DAILY_INTERVALS[label]
        else:
            self.interval_value = WEEKLY_INTERVALS[label]
        await interaction.response.defer()

    @discord.ui.select(placeholder="Select weekday")
    async def weekday_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.day_of_week = int(select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.interval_value is None:
            await interaction.response.send_message("Please select an interval first.", ephemeral=True)
            return

        if self.frequency == "weekly" and self.day_of_week is None:
            await interaction.response.send_message("Please select a weekday.", ephemeral=True)
            return

        time_ts = parse_unix_timestamp(self.time)
        if time_ts is None:
            await interaction.response.send_message("Time must be a valid Unix timestamp.", ephemeral=True)
            return
        
        time_ts -= time_ts % 60

        raw_start = (self.start_date or "").strip()
        if raw_start:
            start_ts = parse_unix_timestamp(raw_start)
            if start_ts is None:
                await interaction.response.send_message(
                    "start_date must be a valid Unix timestamp.", ephemeral=True
                )
                return
        else:
            start_ts = int(datetime.now(tz=timezone.utc).timestamp())
        start_ts -= start_ts % 60

        raw_end = (self.end_date or "").strip()
        if raw_end:
            end_ts = parse_unix_timestamp(raw_end)
            if end_ts is None:
                await interaction.response.send_message(
                    "end_date must be a valid Unix timestamp.", ephemeral=True
                )
                return
        else:
            end_ts = None
        if end_ts is not None:
            end_ts -= end_ts % 60

        if end_ts is not None and end_ts <= start_ts:
            await interaction.response.send_message(
                "end_date must be after start_date.", ephemeral=True
            )
            return

        step_seconds = 86400 if self.frequency == "daily" else 7 * 86400
        step_seconds *= self.interval_value

        first_run_at = time_ts
        while first_run_at < start_ts:
            first_run_at += step_seconds

        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        while first_run_at < now_ts:
            first_run_at += step_seconds

        if self.signup_mode == "Role":
            view = ScheduleRolePickerView(
                title=self.title,
                category=self.category,
                frequency=self.frequency,
                interval_value=self.interval_value,
                day_of_week=self.day_of_week,
                time_ts=time_ts,
                duration=self.duration,
                start_ts=start_ts,
                end_ts=end_ts,
                first_run_at=first_run_at,
                creator_id=interaction.user.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                ping_roles=self.ping_roles,
                announcement_message=self.announcement_message,
            )

            await interaction.response.edit_message(
                content="Select allowed roles (max 5):",
                view=view
            )
            return

        await insert_schedule(
            interaction=interaction,
            title=self.title,
            category=self.category,
            frequency=self.frequency,
            interval_value=self.interval_value,
            day_of_week=self.day_of_week,
            time_ts=time_ts,
            start_ts=start_ts,
            end_ts=end_ts,
            next_run_at=first_run_at,
            duration=self.duration,
            signup_mode=self.signup_mode,
            allowed_role_ids=None,
            ping_roles=self.ping_roles,
            announcement_message=self.announcement_message,
        )
        await interaction.response.edit_message(content="Schedule created.", view=None)


class ScheduleRolePickerView(discord.ui.View):
    def __init__(
        self,
        *,
        title: str,
        category: str,
        frequency: str,
        interval_value: int,
        day_of_week: int | None,
        time_ts: int,
        duration: int,
        start_ts: int,
        end_ts: int | None,
        first_run_at: int,
        creator_id: int,
        guild_id: int,
        channel_id: int,
        ping_roles: bool,
        announcement_message: str | None,
    ):
        super().__init__(timeout=300)
        self.title = title
        self.category = category
        self.frequency = frequency
        self.interval_value = interval_value
        self.day_of_week = day_of_week
        self.time_ts = time_ts
        self.duration = duration
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.first_run_at = first_run_at
        self.creator_id = creator_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.ping_roles = ping_roles
        self.announcement_message = announcement_message
        self.selected_role_ids: list[int] = []

    @discord.ui.select(
        placeholder="Select allowed roles (max 5)",
        min_values=1,
        max_values=5,
        cls=discord.ui.RoleSelect,
    )
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.selected_role_ids = [r.id for r in select.values]
        await interaction.response.defer()

    @discord.ui.button(label="Create Schedule", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_role_ids:
            await interaction.response.send_message("Select at least one role.", ephemeral=True)
            return

        await insert_schedule(
            interaction=interaction,
            title=self.title,
            category=self.category,
            frequency=self.frequency,
            interval_value=self.interval_value,
            day_of_week=self.day_of_week,
            time_ts=self.time_ts,
            start_ts=self.start_ts,
            end_ts=self.end_ts,
            next_run_at=self.first_run_at,
            duration=self.duration,
            signup_mode="Role",
            allowed_role_ids=self.selected_role_ids,
            ping_roles=self.ping_roles,
            announcement_message=self.announcement_message,
        )

        await interaction.response.edit_message(content="Schedule created.", view=None)


class ScheduleEditRolePickerView(discord.ui.View):
    def __init__(
        self,
        *,
        schedule_id: int,
        title: str,
        category: str,
        frequency: str,
        interval_value: int,
        day_of_week: int | None,
        time_ts: int,
        duration: int | None,
        start_ts: int | None,
        end_ts: int | None,
        next_run_at: int,
        signup_mode: str,
        ping_roles: bool,
        announcement_message: str | None,
    ):
        super().__init__(timeout=300)
        self.schedule_id = schedule_id
        self.title = title
        self.category = category
        self.frequency = frequency
        self.interval_value = interval_value
        self.day_of_week = day_of_week
        self.time_ts = time_ts
        self.duration = duration
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.next_run_at = next_run_at
        self.signup_mode = signup_mode
        self.ping_roles = ping_roles
        self.announcement_message = announcement_message
        self.selected_role_ids: list[int] = []

    @discord.ui.select(
        placeholder="Select allowed roles (max 5)",
        min_values=1,
        max_values=5,
        cls=discord.ui.RoleSelect,
    )
    async def select_roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.selected_role_ids = [r.id for r in select.values]
        await interaction.response.defer()

    @discord.ui.button(label="Save Changes", style=discord.ButtonStyle.green)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_role_ids:
            await interaction.response.send_message("Select at least one role.", ephemeral=True)
            return

        conn = get_connection()
        try:
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
                    self.title,
                    self.category,
                    self.frequency,
                    self.interval_value,
                    self.day_of_week,
                    self.time_ts,
                    self.duration,
                    self.start_ts,
                    self.end_ts,
                    self.signup_mode,
                    int(self.ping_roles),
                    self.announcement_message,
                    self.next_run_at,
                    self.schedule_id,
                ),
            )

            conn.execute(
                "DELETE FROM schedule_allowed_roles WHERE schedule_id = ?",
                (self.schedule_id,),
            )
            for role_id in self.selected_role_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO schedule_allowed_roles (schedule_id, role_id) VALUES (?, ?)",
                    (self.schedule_id, role_id),
                )

            conn.commit()
        finally:
            conn.close()

        await interaction.response.edit_message(content="Schedule updated.", view=None)
