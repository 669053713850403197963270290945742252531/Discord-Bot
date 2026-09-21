"""Database administration commands for the Supabase license database."""

import csv
import io
import json
from typing import List
from pathlib import PurePosixPath

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, send_success, default_ui_error, safe_defer, safe_send_modal
from api.alerts import send_alert, alert_embed, ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT
from api.supabase_db import fetch_users, fetch_api_text_and_sha, commit_content, serialize_users_json, list_games, get_game, create_game, delete_game, update_game
from api.supabase_storage import fetch_game_script_bytes, upload_game_script, delete_game_script, get_game_script_filename, SupabaseStorageError
from api.users import revoke_buyer_role, find_removed_discord_ids

GUILD = discord.Object(id=config.GUILD_ID)


class GameEditModal(discord.ui.Modal, title="Edit Game"):
        game_id = discord.ui.Label(text="Game ID", component=discord.ui.TextInput(max_length=100))
        name = discord.ui.Label(text="Name", component=discord.ui.TextInput(max_length=100))
        file_name = discord.ui.Label(text="File Name", component=discord.ui.TextInput(max_length=255))
        enabled = discord.ui.Label(
            text="Enabled",
            description="Whether this game is available for license checks.",
            component=discord.ui.Checkbox(default=True),
        )

        def __init__(self, game: dict, file_name: str):
            super().__init__(title=f"Edit {game.get('name', 'Game')}"[:45])
            self.original_game_id = str(game.get("id") or "")
            self.game_id.component.default = self.original_game_id
            self.name.component.default = str(game.get("name") or "")
            self.file_name.component.default = file_name
            self.enabled.component.default = bool(game.get("enabled", True))

        async def on_error(self, interaction: discord.Interaction, error: Exception):
            await default_ui_error(interaction, error, label="GameEditModal")

        async def on_submit(self, interaction: discord.Interaction):
            await safe_defer(interaction, ephemeral=True)

            new_game_id = self.game_id.component.value.strip()
            new_name = self.name.component.value.strip()
            new_file_name = self.file_name.component.value.strip().lstrip("/")
            # The Script Path is intentionally derived from File Name on submission.
            # This prevents the two fields from becoming mismatched.
            new_script_path = new_file_name
            new_enabled = bool(self.enabled.component.value)

            if not new_game_id:
                return await send_error(interaction, "Game ID cannot be empty.")
            if not new_name:
                return await send_error(interaction, "Name cannot be empty.")
            if not new_file_name:
                return await send_error(interaction, "File name cannot be empty.")
            if ".." in new_file_name.split("/"):
                return await send_error(interaction, "File name cannot contain `..` path segments.")

            try:
                current_game = await get_game(self.original_game_id)
            except Exception as exc:
                return await send_error(interaction, f"Failed to verify the current game record: {exc}")
            if not current_game:
                return await send_error(interaction, "This game no longer exists in the database.")

            try:
                duplicate = await get_game(new_game_id)
            except Exception as exc:
                return await send_error(interaction, f"Failed to validate the new Game ID: {exc}")
            if duplicate and str(duplicate.get("id")) != self.original_game_id:
                return await send_error(interaction, f"A game with ID `{new_game_id}` already exists.")

            old_script_path = str(current_game.get("script_path") or "").strip().lstrip("/")
            if not old_script_path:
                return await send_error(interaction, "The existing game has no valid script path.")

            storage_changed = new_script_path != old_script_path
            new_uploaded = False
            if storage_changed:
                try:
                    script_bytes = await fetch_game_script_bytes(old_script_path)
                    await upload_game_script(new_script_path, script_bytes)
                    new_uploaded = True
                except SupabaseStorageError as exc:
                    return await send_error(interaction, f"Failed to move the game script in Storage: {exc}")

            try:
                updated = await update_game(
                    self.original_game_id,
                    new_game_id,
                    new_name,
                    new_script_path,
                    new_enabled,
                )
            except Exception as exc:
                if new_uploaded:
                    try:
                        await delete_game_script(new_script_path)
                    except Exception:
                        pass
                return await send_error(interaction, f"Failed to update the Games database: {exc}")

            if storage_changed:
                try:
                    await delete_game_script(old_script_path)
                except SupabaseStorageError as exc:
                    await send_success(
                        interaction,
                        f"Updated **{new_name}**, but the old Storage object `{old_script_path}` could not be removed.\n\nError: {exc}",
                    )
                    return

            await send_success(interaction, f"Updated **{new_name}** (`{new_game_id}`).")

def _parse_bool_text(value, default=True):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError("enabled must be True or False")


async def _upload_impl(interaction, file: discord.Attachment):
    await safe_defer(interaction, ephemeral=True)
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
        stripped = text.lstrip()
        if stripped.startswith("["):
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError("JSON root must be an array")
        else:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if not rows:
                raise ValueError("CSV contains no license records")
            data = []
            for row in rows:
                normalized = {str(k).strip().lower(): v for k, v in row.items()}
                data.append({
                    "Identifier": normalized.get("identifier", "").strip(),
                    "DiscordId": normalized.get("discord_id", "").strip() or None,
                    "Key": normalized.get("license_key", normalized.get("key", "")).strip() or None,
                    "Activated": normalized.get("activated", "").strip() or None,
                    "Executions": int(normalized.get("executions") or 0),
                    "Rank": normalized.get("rank", "User").strip() or "User",
                    "Notes": normalized.get("notes", "").strip() or None,
                    "Enabled": _parse_bool_text(normalized.get("enabled"), True),
                    "ExpiresAt": normalized.get("expires_at", "").strip() or None,
                    "Games": [x.strip() for x in normalized.get("games", "*").split(",") if x.strip()] or ["*"],
                    "HWID": normalized.get("hwid", "").strip() or None,
                    "LastHwidReset": normalized.get("last_hwid_reset", "").strip() or None,
                    "totalHwidResets": int(normalized.get("hwid_resets") or 0),
                    "CreatedAt": normalized.get("created_at", "").strip() or None,
                    "UpdatedAt": normalized.get("updated_at", "").strip() or None,
                    "ActivationCountry": normalized.get("activation_country_code", "").strip().upper() or None,
                    "ActivationRobloxUserId": (int(normalized.get("activation_roblox_user_id")) if normalized.get("activation_roblox_user_id", "").strip() else None),
                })
        old = await fetch_users()
        await commit_content(json.dumps(data, ensure_ascii=False), None, f"Import license database by {interaction.user}")
    except Exception as e:
        return await send_error(interaction, f"Failed to import license database: {e}")
    def _identifier(record):
        return str(record.get("Identifier") or "").strip()

    def _normalize_games(value):
        if isinstance(value, (list, tuple, set)):
            return tuple(str(game).strip() for game in value)
        if value in (None, ""):
            return tuple()
        return tuple(part.strip() for part in str(value).split(",") if part.strip())

    def _signature(record):
        return (
            _identifier(record),
            str(record.get("DiscordId") or "").strip(),
            str(record.get("Key") or "").strip(),
            str(record.get("Activated") or "").strip(),
            int(record.get("Executions") or 0),
            str(record.get("Rank") or "User").strip(),
            str(record.get("Notes") or "").strip(),
            bool(record.get("Enabled", True)),
            str(record.get("ExpiresAt") or "").strip(),
            _normalize_games(record.get("Games") or []),
            str(record.get("HWID") or "").strip(),
            str(record.get("LastHwidReset") or record.get("LastHWIDReset") or "").strip(),
            int(record.get("totalHwidResets", record.get("HwidResets", record.get("HWIDResets", 0))) or 0),
            str(record.get("CreatedAt") or "").strip(),
            str(record.get("UpdatedAt") or "").strip(),
            str(record.get("ActivationCountry") or "").strip().upper(),
            int(record.get("ActivationRobloxUserId") or 0),
        )

    old_by_identifier = {_identifier(record): record for record in old if _identifier(record)}
    new_by_identifier = {_identifier(record): record for record in data if _identifier(record)}

    added_ids = [identifier for identifier in new_by_identifier if identifier not in old_by_identifier]
    removed_ids = [identifier for identifier in old_by_identifier if identifier not in new_by_identifier]
    changed_ids = [
        identifier
        for identifier in new_by_identifier
        if identifier in old_by_identifier
        and _signature(old_by_identifier[identifier]) != _signature(new_by_identifier[identifier])
    ]

    # Preserve the existing role cleanup for licenses that disappeared.
    for identifier in removed_ids:
        discord_id = old_by_identifier[identifier].get("DiscordId")
        if discord_id:
            await revoke_buyer_role(interaction.guild, discord_id)

    def _format_names(identifiers, source):
        values = []
        for identifier in identifiers[:15]:
            record = source.get(identifier, {})
            discord_id = str(record.get("DiscordId") or "").strip()
            mention = f"<@{discord_id}>" if discord_id else "no Discord ID"
            values.append(f"`{identifier}` ({mention})")
        suffix = "" if len(identifiers) <= 15 else f" + {len(identifiers) - 15} more"
        return ", ".join(values) + suffix

    details = [
        f"**Total:** {len(data)}",
        f"**Added:** {len(added_ids)}" + (f" — {_format_names(added_ids, new_by_identifier)}" if added_ids else ""),
        f"**Removed:** {len(removed_ids)}" + (f" — {_format_names(removed_ids, old_by_identifier)}" if removed_ids else ""),
        f"**Changed:** {len(changed_ids)}" + (f" — {_format_names(changed_ids, new_by_identifier)}" if changed_ids else ""),
    ]
    await send_success(interaction, "Imported license database into Supabase.\n\n" + "\n".join(details))
    await send_alert(
        interaction.client,
        alert_embed(
            "📥 License Database Uploaded",
            f"{interaction.user.mention} replaced the license database from `{file.filename}`.",
            color=ALERT_COLOR_EDIT,
            fields=[
                ("Total", str(len(data)), True),
                ("Added", str(len(added_ids)), True),
                ("Removed", str(len(removed_ids)), True),
                ("Changed", str(len(changed_ids)), True),
            ],
        ),
    )


class Database(commands.Cog):
    def __init__(self, bot): self.bot = bot




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

    @games_group.command(name="edit", description="Edits a supported game's information.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    @app_commands.describe(game_id="The unique game ID to edit.")
    async def games_edit(self, interaction, game_id: str):
        game_id = game_id.strip()
        if not game_id:
            return await send_error(interaction, "Game ID cannot be empty.")

        try:
            game = await get_game(game_id)
        except Exception as exc:
            return await send_error(interaction, f"Failed to look up game `{game_id}`: {exc}")

        if not game:
            return await send_error(interaction, f"No supported game with ID `{game_id}` was found.")

        script_path = str(game.get("script_path") or "").strip().lstrip("/")
        if not script_path:
            return await send_error(interaction, f"Game `{game_id}` does not have a valid script path configured.")

        try:
            file_name = await get_game_script_filename(script_path)
        except SupabaseStorageError as exc:
            return await send_error(
                interaction,
                f"The game exists in the database, but its script could not be found in the private `game-scripts` bucket: {exc}",
            )

        await safe_send_modal(interaction, GameEditModal(game, file_name))

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
        await send_alert(
            interaction.client,
            alert_embed(
                "🎮 Game Added",
                f"{interaction.user.mention} added **{name}** (`{game_id}`) to the Games database and uploaded `{script_path}`.",
                color=ALERT_COLOR_ADD,
            ),
        )


    @games_group.command(name="remove", description="Removes a supported game and its script from the private storage bucket.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    @app_commands.describe(game_id="The game ID of the supported game to remove.")
    async def games_remove(self, interaction, game_id: str):
        await safe_defer(interaction, ephemeral=True)

        game_id = game_id.strip()
        if not game_id:
            return await send_error(interaction, "Game ID cannot be empty.")

        try:
            game = await get_game(game_id)
        except Exception as exc:
            return await send_error(interaction, f"Failed to look up game `{game_id}`: {exc}")

        if not game:
            return await send_error(interaction, f"No game with ID `{game_id}` was found in the Games database.")

        script_path = str(game.get("script_path") or "").strip().lstrip("/")
        if not script_path:
            return await send_error(interaction, f"Game `{game_id}` does not have a configured script path; refusing to remove it.")

        # Keep the database and private Storage cleanup tied to the same game
        # record. The script_path comes exclusively from the database lookup.
        try:
            deleted_game = await delete_game(game_id)
        except Exception as exc:
            return await send_error(interaction, f"Failed to delete game `{game_id}` from the Games database: {exc}")

        if not deleted_game:
            return await send_error(interaction, f"Game `{game_id}` could not be deleted because it no longer exists.")

        try:
            await delete_game_script(script_path)
        except SupabaseStorageError as exc:
            # Best-effort rollback so a Storage failure does not leave the
            # database without its supported-game record.
            try:
                await create_game(
                    str(deleted_game.get("id", game_id)),
                    str(deleted_game.get("name", "")),
                    script_path,
                )
            except Exception as rollback_exc:
                return await send_error(
                    interaction,
                    "The game was removed from the Games database, but its Storage script could not be deleted, "
                    f"and the database rollback also failed: {exc} | Rollback: {rollback_exc}",
                )
            return await send_error(
                interaction,
                f"Failed to delete the game script `{script_path}` from Storage. The Games database entry was restored: {exc}",
            )

        embed = discord.Embed(
            title="✅ Game Removed",
            description=f"**{game.get('name', game_id)}** was removed successfully.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Game ID", value=f"`{game_id}`", inline=True)
        embed.add_field(name="Script Path", value=f"`{script_path}`", inline=False)
        embed.set_footer(text="Database entry and private Storage script deleted.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await send_alert(
            interaction.client,
            alert_embed(
                "🎮 Game Removed",
                f"{interaction.user.mention} removed **{game.get('name', game_id)}** (`{game_id}`) and deleted `{script_path}` from Storage.",
                color=ALERT_COLOR_REMOVE,
            ),
        )

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
        await send_alert(
            interaction.client,
            alert_embed(
                "🎮 Game Script Updated",
                f"{interaction.user.mention} replaced the script for **{game.get('name', game_id)}** (`{game_id}`) at `{script_path}`.",
                color=ALERT_COLOR_EDIT,
            ),
        )


async def setup(bot):
    await bot.add_cog(Database(bot))
