import difflib
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_error, build_embed
from api.hashing import get_available_hash_algorithms, hash_text, SHAKE_OUTPUT_BYTES
from api.transforms import TRANSFORM_FORMAT_CHOICES, transform_text

GUILD = discord.Object(id=config.GUILD_ID)


def _safe_codeblock(value: str, limit: int = 1000) -> str:
    # Truncate to stay under Discord's 1024-char embed field limit, and
    # break up any literal ``` in the input so it can't prematurely close
    # the surrounding code block.
    value = value.replace("```", "``\u200b`")
    if len(value) > limit:
        value = value[:limit] + "… (truncated)"
    return value


async def hash_algorithm_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """
    Populates /hash's `algorithm` option as the user types. hashlib can
    easily expose more algorithms than Discord's 25-result autocomplete cap
    (especially once OpenSSL's extras are counted), so this narrows to
    substring matches against whatever's typed so far instead of always
    showing the same first 25 alphabetically.
    """
    algorithms = get_available_hash_algorithms()
    query = current.lower().strip()
    matches = [a for a in algorithms if query in a] if query else algorithms
    return [app_commands.Choice(name=a, value=a) for a in matches[:25]]


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="hash", description="Hashes text using a chosen algorithm (MD5, SHA-2, SHA-3, BLAKE2, SHAKE, etc).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(text="The text to hash", algorithm="Hash algorithm to use -- start typing to search the full list")
    @app_commands.autocomplete(algorithm=hash_algorithm_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def hash_cmd(self, interaction: discord.Interaction, text: str, algorithm: str):
        algo = algorithm.lower().strip()
        available = get_available_hash_algorithms()

        if algo not in available:
            # Autocomplete only *suggests* valid values -- Discord still lets
            # a user submit whatever raw text they typed, so this re-validates
            # rather than trusting the input.
            suggestion = difflib.get_close_matches(algo, available, n=1)
            hint = f" Did you mean `{suggestion[0]}`?" if suggestion else " Start typing to see the list of supported algorithms."
            return await send_error(interaction, f"`{algorithm}` isn't a supported hash algorithm.{hint}")

        try:
            digest = hash_text(algo, text)
        except (TypeError, ValueError) as e:
            return await send_error(interaction, f"Failed to hash text with `{algo}`: {e}")

        algorithm_label = f"`{algo}`"
        if algo.startswith("shake_"):
            algorithm_label += f" (SHAKE / XOF -- shown at {SHAKE_OUTPUT_BYTES * 8}-bit output length)"

        embed = build_embed(
            title="🔐 Hash Result",
            color=discord.Color.blue(),
            fields=[
                ("Algorithm", algorithm_label, False),
                ("Before", f"```{_safe_codeblock(text)}```", False),
                ("After", f"```{_safe_codeblock(digest)}```", False),
            ],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="transform", description="Transforms text into a stylized Unicode format (superscript, cursive, zalgo, and more).")
    @app_commands.guilds(GUILD)
    @app_commands.describe(text="The text to transform", format="Style to transform the text into")
    @app_commands.choices(format=[app_commands.Choice(name=name, value=value) for name, value in TRANSFORM_FORMAT_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def transform_cmd(self, interaction: discord.Interaction, text: str, format: app_commands.Choice[str]):
        try:
            result = transform_text(format.value, text)
        except ValueError as e:
            return await safe_respond(interaction, embed=build_embed(title="Error", description=str(e), color=discord.Color.red()), ephemeral=True)

        embed = build_embed(
            title="🎨 Transform Result",
            color=discord.Color.blue(),
            fields=[
                ("Format", f"`{format.name}`", False),
                ("Before", f"```{_safe_codeblock(text)}```", False),
                ("After", f"```{_safe_codeblock(result)}```", False),
            ],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
