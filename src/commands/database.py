"""Database administration commands for the Supabase license database."""

import csv
import io
import json
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, send_success
from api.supabase_db import fetch_users, fetch_api_text_and_sha, commit_content, serialize_users_json, list_games
from api.users import revoke_buyer_role, find_removed_discord_ids

GUILD = discord.Object(id=config.GUILD_ID)


class Database(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @app_commands.command(name="export", description="Exports the current Supabase license database.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(format="Export format")
    @app_commands.choices(format=[app_commands.Choice(name="JSON", value="json"), app_commands.Choice(name="CSV", value="csv")])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def export(self, interaction, format: app_commands.Choice[str] = None):
        users = await fetch_users()
        selected = format.value if format else "json"
        if selected == "csv":
            buf = io.StringIO(); writer = csv.writer(buf)
            writer.writerow(["identifier","discord_id","license_key","activated","executions","rank","notes","enabled","expires_at","games"])
            for u in users:
                writer.writerow([u.get("Identifier"),u.get("DiscordId"),u.get("Key"),u.get("Activated"),u.get("Executions",0),u.get("Rank"),u.get("Notes"),u.get("Enabled",True),u.get("ExpiresAt"),",".join(u.get("Games") or [])])
            data = buf.getvalue().encode()
            await interaction.response.send_message(file=discord.File(io.BytesIO(data), filename="licenses.csv"), ephemeral=True)
            return
        data = serialize_users_json(users).encode()
        await interaction.response.send_message(file=discord.File(io.BytesIO(data), filename="licenses.json"), ephemeral=True)

    @app_commands.command(name="upload", description="Replaces the Supabase license database using an exported JSON file.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(file="JSON export containing an array of license records")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def upload(self, interaction, file: discord.Attachment):
        raw = await file.read()
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, list): raise ValueError("root must be an array")
            old = await fetch_users()
            await commit_content(json.dumps(data, ensure_ascii=False), None, f"Import license database by {interaction.user}")
        except Exception as e:
            return await send_error(interaction, f"Failed to import license database: {e}")
        removed = find_removed_discord_ids(old, data)
        for discord_id in removed:
            await revoke_buyer_role(interaction.guild, discord_id)
        await send_success(interaction, f"Imported {len(data)} license record(s) into Supabase.")

    @app_commands.command(name="dbsearch", description="Searches the license database for a value.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(query="Text to search for")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def dbsearch(self, interaction, query: str):
        q = query.lower().strip(); users = await fetch_users(); matches=[]
        for u in users:
            hay = " ".join(str(u.get(k,"")) for k in ("Identifier","DiscordId","Key","Rank","Notes","Games")).lower()
            if q in hay: matches.append(u)
        if not matches: return await send_error(interaction, "No matching license records found.")
        lines=[]
        for u in matches[:25]:
            lines.append(f"**{u.get('Identifier','Unknown')}** — <@{u.get('DiscordId')}> — `{u.get('Key')}` — Games: {', '.join(u.get('Games') or [])}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="games", description="Lists supported games in the Supabase games database.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def games(self, interaction):
        games = await list_games()
        lines=[f"`{g.get('id')}` — {g.get('name')} — `{g.get('script_path')}` — {'enabled' if g.get('enabled',True) else 'disabled'}" for g in games if g.get('id') != '*']
        await interaction.response.send_message("\n".join(lines) if lines else "No games configured.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Database(bot))
