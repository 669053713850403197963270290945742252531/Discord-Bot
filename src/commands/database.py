"""Database administration commands for the Supabase license database."""

import csv
import io
import json
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, send_success, safe_defer
from api.supabase_db import fetch_users, fetch_api_text_and_sha, commit_content, serialize_users_json, list_games, get_game, create_game
from api.supabase_storage import upload_game_script, SupabaseStorageError
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
        await safe_defer(interaction, ephemeral=True)
        users = await fetch_users()
        selected = format.value if format else "json"
        if selected == "csv":
            buf = io.StringIO(); writer = csv.writer(buf)
            writer.writerow(["identifier","discord_id","license_key","activated","executions","rank","notes","enabled","expires_at","games"])
            for u in users:
                writer.writerow([u.get("Identifier"),u.get("DiscordId"),u.get("Key"),u.get("Activated"),u.get("Executions",0),u.get("Rank"),u.get("Notes"),u.get("Enabled",True),u.get("ExpiresAt"),",".join(u.get("Games") or [])])
            data = buf.getvalue().encode()
            await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="licenses.csv"), ephemeral=True)
            return
        data = serialize_users_json(users).encode()
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename="licenses.json"), ephemeral=True)

    @app_commands.command(name="upload", description="Replaces the Supabase license database using an exported JSON file.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(file="JSON export containing an array of license records")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def upload(self, interaction, file: discord.Attachment):
        await safe_defer(interaction, ephemeral=True)
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
        await safe_defer(interaction, ephemeral=True)
        q = query.lower().strip(); users = await fetch_users(); matches=[]
        for u in users:
            hay = " ".join(str(u.get(k,"")) for k in ("Identifier","DiscordId","Key","Rank","Notes","Games")).lower()
            if q in hay: matches.append(u)
        if not matches: return await send_error(interaction, "No matching license records found.")
        lines=[]
        for u in matches[:25]:
            lines.append(f"**{u.get('Identifier','Unknown')}** — <@{u.get('DiscordId')}> — `{u.get('Key')}` — Games: {', '.join(u.get('Games') or [])}")
        embed = discord.Embed(title="🔎 Database Search", description=f"Found {len(matches)} matching license record(s).", color=discord.Color.blurple())
        for index, u in enumerate(matches[:25], start=1):
            embed.add_field(
                name=f"{index}. {u.get('Identifier', 'Unknown')}",
                value=f"Discord: <@{u.get('DiscordId')}>\nKey: `{u.get('Key')}`\nGames: {', '.join(u.get('Games') or []) or 'None'}",
                inline=False,
            )
        if len(matches) > 25:
            embed.set_footer(text=f"Showing 25 of {len(matches)} matches")
        await interaction.followup.send(embed=embed, ephemeral=True)

    games_group = app_commands.guilds(GUILD)(
        app_commands.Group(name="games", description="Game management commands.")
    )

    @games_group.command(name="list", description="Lists all supported games.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def games_list(self, interaction):
        await safe_defer(interaction, ephemeral=True)
        games = await list_games()
        visible = [g for g in games if g.get("id") != "*"]

        embed = discord.Embed(
            title="🎮 Supported Games",
            description="Games currently available through the License Server.",
            color=discord.Color.blurple(),
        )

        if visible:
            chunks = []
            current = []
            current_length = 0
            for game in visible:
                game_id = str(game.get("id", "Unknown"))
                name = str(game.get("name", "Unknown Game"))
                status = "🟢 Enabled" if game.get("enabled", True) else "🔴 Disabled"
                line = f"**{name}** — `{game_id}` • {status}"
                if current and current_length + len(line) + 1 > 1024:
                    chunks.append(current)
                    current = []
                    current_length = 0
                current.append(line)
                current_length += len(line) + 1
            if current:
                chunks.append(current)

            for index, chunk in enumerate(chunks, start=1):
                embed.add_field(
                    name="Supported Games" if index == 1 else "Supported Games (continued)",
                    value="\n".join(chunk),
                    inline=False,
                )
            embed.set_footer(text=f"{len(visible)} supported game(s)")
        else:
            embed.description = "No games are currently configured."

        await interaction.followup.send(embed=embed, ephemeral=True)

    @games_group.command(name="add", description="Adds a game and uploads its script to the private game-scripts bucket.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    @app_commands.describe(
        game_id="The unique game ID to add to the Games database.",
        name="The display name of the game.",
        script_path="The private Storage object path for the game's script; this also determines the uploaded file name/path.",
        file="The game script file to upload; it will be stored using Script Path as its name/path.",
    )
    async def games_add(
        self,
        interaction,
        game_id: str,
        name: str,
        script_path: str,
        file: discord.Attachment,
    ):
        await safe_defer(interaction, ephemeral=True)

        game_id = game_id.strip()
        name = name.strip()
        script_path = script_path.strip().lstrip("/")

        if not game_id:
            return await send_error(interaction, "Game ID cannot be empty.")
        if not name:
            return await send_error(interaction, "Game name cannot be empty.")
        if not script_path:
            return await send_error(interaction, "Script Path cannot be empty.")
        if ".." in script_path.split("/"):
            return await send_error(interaction, "Script Path cannot contain `..` path segments.")

        try:
            existing = await get_game(game_id)
        except Exception as exc:
            return await send_error(interaction, f"Failed to look up game `{game_id}`: {exc}")

        if existing:
            return await send_error(interaction, f"A game with ID `{game_id}` already exists.")

        try:
            data = await file.read()
        except Exception as exc:
            return await send_error(interaction, f"Failed to read the uploaded script file: {exc}")

        if not data:
            return await send_error(interaction, "The uploaded script file is empty.")

        try:
            await upload_game_script(script_path, data)
        except SupabaseStorageError as exc:
            return await send_error(interaction, f"Failed to upload the game script: {exc}")

        try:
            game = await create_game(game_id, name, script_path)
        except Exception as exc:
            return await send_error(
                interaction,
                "The script was uploaded, but the Games database entry could not be created: "
                f"{exc}",
            )

        embed = discord.Embed(
            title="✅ Game Added",
            description=f"**{name}** was added to the Games database and its script was uploaded successfully.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Game ID", value=f"`{game.get('id', game_id)}`", inline=True)
        embed.add_field(name="Name", value=str(game.get("name", name)), inline=True)
        embed.add_field(name="Script Path", value=f"`{script_path}`", inline=False)
        embed.add_field(name="Uploaded File", value=f"`{file.filename}`", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @games_group.command(name="update", description="Replaces a game's script in the private game-scripts bucket.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    @app_commands.describe(
        game_id="The game ID whose configured script should be replaced.",
        file="The new game script file to upload.",
    )
    async def games_update(self, interaction, game_id: str, file: discord.Attachment):
        await safe_defer(interaction, ephemeral=True)

        try:
            game = await get_game(game_id.strip())
        except Exception as exc:
            return await send_error(interaction, f"Failed to look up game `{game_id}`: {exc}")

        if not game:
            return await send_error(interaction, f"No game with ID `{game_id}` was found in the Games database.")

        script_path = str(game.get("script_path") or "").strip()
        if not script_path:
            return await send_error(interaction, f"Game `{game_id}` does not have a configured script path.")

        try:
            data = await file.read()
        except Exception as exc:
            return await send_error(interaction, f"Failed to read the uploaded script file: {exc}")

        if not data:
            return await send_error(interaction, "The uploaded script file is empty.")

        try:
            await upload_game_script(script_path, data)
        except SupabaseStorageError as exc:
            return await send_error(interaction, f"Failed to update the game script: {exc}")

        embed = discord.Embed(
            title="✅ Game Script Updated",
            description=f"The script for **{game.get('name', game_id)}** has been replaced successfully.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Script Path", value=f"`{script_path}`", inline=True)
        embed.add_field(name="Updated File", value=f"`{file.filename}`", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Database(bot))
