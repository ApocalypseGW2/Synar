import discord
from storage.db import get_connection


async def build_signup_embed(
    *,
    guild: discord.Guild | None,
    title: str,
    category: str,
    timestamp: int,
    duration: int | None,
    signup_mode: str,
    max_slots: int,
    creator_id: int,
    event_id: int,
    allowed_role_ids: list[int] | None = None,
    schedule_id: int | None = None,
) -> discord.Embed:
    def display_name(user_id: int) -> str:
        if guild:
            m = guild.get_member(user_id)
            if m:
                return m.display_name
        return f"<@{user_id}>"

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id, status FROM event_signups WHERE event_id = ?",
            (event_id,),
        ).fetchall()
    finally:
        conn.close()

    available = [display_name(r["user_id"]) for r in rows if r["status"] == "available"]
    unavailable = [display_name(r["user_id"]) for r in rows if r["status"] == "unavailable"]
    maybe = [display_name(r["user_id"]) for r in rows if r["status"] == "maybe"]

    embed = discord.Embed(
        title=title or "Event",
        description=(
            f"**Category:** {category}\n"
            f"**Signup-Mode:** {(signup_mode or 'open').capitalize()}\n"
            f"**Date:** <t:{timestamp}:F>\n"
            f"**Duration:** {duration if duration is not None else '-'} hours\n"
            f"**Signups:** {len(available)}/{max_slots}"
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(name="Available", value="\n".join(available) or "-", inline=True)
    embed.add_field(name="Unavailable", value="\n".join(unavailable) or "-", inline=True)
    embed.add_field(name="Maybe", value="\n".join(maybe) or "-", inline=True)

    embed.add_field(
        name="Allowed Roles",
        value=role_names_text(guild, allowed_role_ids, signup_mode),
        inline=False,
    )

    creator_name = None
    if guild:
        m = guild.get_member(creator_id)
        if not m:
            try:
                m = await guild.fetch_member(creator_id)
            except discord.NotFound:
                m = None
        if m:
            creator_name = m.display_name

    footer = f"Event ID: {event_id}"
    if schedule_id is not None:
        footer += f" | Schedule ID: {schedule_id}"
    if creator_name:
        footer += f" | Host: {creator_name}"
    embed.set_footer(text=footer)

    return embed


def role_names_text(guild: discord.Guild | None, role_ids: list[int] | None, signup_mode: str) -> str:
    if signup_mode.lower() == "invite":
        return "Invite Only"
    if not role_ids:
        return "Everyone"
    if not guild:
        return "Roles set"
    names = []
    for rid in role_ids:
        role = guild.get_role(rid)
        if role:
            names.append(role.name)
    return ", ".join(names) if names else "Roles set"
