from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_error, build_embed
from api.ciphers import CIPHER_ALGORITHMS, CIPHER_CHOICES, cipher_text, decipher_text

GUILD = discord.Object(id=config.GUILD_ID)


def _safe_codeblock(value: str, limit: int = 1000) -> str:
    # Same truncation/escaping approach as utility.py's /encode & /decode --
    # stay under Discord's 1024-char embed field limit, and break up any
    # literal ``` in the input so it can't prematurely close the code block.
    value = value.replace("```", "``\u200b`")
    if len(value) > limit:
        value = value[:limit] + "… (truncated)"
    return value


def _key_field(algorithm_key: str, used_key: Optional[str], *, generated: bool) -> Optional[tuple]:
    """Builds a ("Key Used", value, False) embed field, or None for
    key-less ciphers. `generated` flags a freshly auto-generated Simple
    Substitution key so the field can nudge the user to save it."""
    entry = CIPHER_ALGORITHMS[algorithm_key]
    if entry["key_mode"] == "none":
        return None
    if not used_key:
        return ("Key Used", "Standard grid (no keyword)", False)
    suffix = " -- save this, you'll need it to decipher!" if generated else ""
    return ("Key Used", f"`{used_key}`{suffix}", False)


class Ciphers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="cipher", description="Encrypts text with a classic cipher (Caesar, Vigenère, Playfair, and more).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        text="The text to cipher",
        algorithm="Which cipher to use",
        key="Key/keyword for the cipher (optional for some -- blank uses the default or generates one)",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in CIPHER_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def cipher_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: Optional[str] = None,
    ):
        entry = CIPHER_ALGORITHMS[algorithm.value]

        try:
            result, used_key = cipher_text(algorithm.value, text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        fields = [
            ("Cipher", f"`{algorithm.name}`", False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(result)}```", False),
        ]
        generated_key = entry["key_mode"] == "required_or_generate" and not (key and key.strip())
        key_field = _key_field(algorithm.value, used_key, generated=generated_key)
        if key_field:
            fields.append(key_field)
        if entry.get("note"):
            fields.append(("Note", entry["note"], False))

        embed = build_embed(title="🔒 Cipher Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="decipher", description="Decrypts text with a classic cipher (must match the cipher & key used to encrypt).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        text="The ciphertext to decipher",
        algorithm="Which cipher to decrypt with",
        key="Key/keyword the text was ciphered with (required unless that cipher has no key)",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in CIPHER_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def decipher_cmd(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: Optional[str] = None,
    ):
        entry = CIPHER_ALGORITHMS[algorithm.value]

        try:
            result, used_key = decipher_text(algorithm.value, text, key)
        except ValueError as e:
            return await send_error(interaction, str(e))

        fields = [
            ("Cipher", f"`{algorithm.name}`", False),
            ("Before", f"```{_safe_codeblock(text)}```", False),
            ("After", f"```{_safe_codeblock(result)}```", False),
        ]
        key_field = _key_field(algorithm.value, used_key, generated=False)
        if key_field:
            fields.append(key_field)
        if entry.get("note"):
            fields.append(("Note", entry["note"], False))

        embed = build_embed(title="🔓 Decipher Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ciphers(bot))
