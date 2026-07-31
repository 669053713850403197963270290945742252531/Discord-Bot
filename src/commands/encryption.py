from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_error, build_embed
from api.encryption import ENCRYPTION_ALGORITHMS, ENCRYPTION_CHOICES, encrypt_text, decrypt_text

GUILD = discord.Object(id=config.GUILD_ID)


def _safe_codeblock(value: str, limit: int = 1000) -> str:
    # Same truncation/escaping approach as ciphers.py & utility.py's
    # /encode encode & /encode decode -- stay under Discord's 1024-char
    # embed field limit, and break up any literal ``` in the input so it
    # can't prematurely close the code block.
    value = value.replace("```", "``\u200b`")
    if len(value) > limit:
        value = value[:limit] + "… (truncated)"
    return value


class Encryption(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="encrypt", description="Encrypts text with real encryption -- 19 algorithms, each rated for security.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        text="The text to encrypt",
        algorithm="Which algorithm to use (each option shows a short security rating)",
        key="Passphrase/key (leave blank to have a strong one generated for you -- you must save it to decrypt!)",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in ENCRYPTION_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def encrypt_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: Optional[str] = None,
    ):
        entry = ENCRYPTION_ALGORITHMS[algorithm.value]

        try:
            result, used_key = encrypt_text(algorithm.value, text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        generated_key = not (key and key.strip())
        key_note = " -- save this, you'll need it to decrypt!" if generated_key else ""

        fields = [
            ("Algorithm", f"`{entry['name']}`", False),
            ("Security", entry.get("security", "N/A"), False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(result)}```", False),
            ("Key Used", f"`{used_key}`{key_note}", False),
        ]
        if entry.get("note"):
            fields.append(("Note", entry["note"], False))

        embed = build_embed(title="🔐 Encrypt Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="decrypt", description="Decrypts text that was encrypted with /encrypt (needs the exact key used).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        text="The ciphertext to decrypt",
        algorithm="Which algorithm it was encrypted with (each option shows a short security rating)",
        key="The exact passphrase/key it was encrypted with",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in ENCRYPTION_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def decrypt_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: str,
    ):
        entry = ENCRYPTION_ALGORITHMS[algorithm.value]

        try:
            result = decrypt_text(algorithm.value, text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        fields = [
            ("Algorithm", f"`{entry['name']}`", False),
            ("Security", entry.get("security", "N/A"), False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(result)}```", False),
        ]
        if entry.get("note"):
            fields.append(("Note", entry["note"], False))

        embed = build_embed(title="🔓 Decrypt Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Encryption(bot))
