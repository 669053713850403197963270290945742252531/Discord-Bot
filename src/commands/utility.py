import difflib
import random
import re
from typing import List

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, safe_respond, send_error, build_embed,
    build_unified_diff, send_diff_result,
)
from api.hashing import get_available_hash_algorithms, hash_text, SHAKE_OUTPUT_BYTES
from api.transforms import TRANSFORM_FORMAT_CHOICES, transform_text
from api.encoding import (
    ENCODING_ALGORITHMS, ENCODING_CHOICES, IDENTIFY_CHOICE_VALUE,
    encode_text, decode_text, identify_encoding,
)
from api.encoding_help import ENCODING_HELP, get_demo_text
from api.entropy import analyze_entropy

GUILD = discord.Object(id=config.GUILD_ID)

# /diff reads both attachments fully into memory to decode + diff them, so
# this caps how large a single file it'll accept -- generous enough for any
# source file a person would realistically want to compare, without letting
# a huge upload stall the bot on decoding/diffing it.
MAX_DIFF_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5 MiB


def _sanitize_filename_part(name: str) -> str:
    # Attachment filenames are user-controlled -- this keeps the generated
    # .diff filename filesystem/Discord-safe (no path separators, spaces,
    # or other punctuation) instead of trusting them verbatim.
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return base[:40] or "file"


ENTROPY_RATING_COLORS = {
    "Very Weak": discord.Color.dark_red(),
    "Weak": discord.Color.red(),
    "Reasonable": discord.Color.orange(),
    "Strong": discord.Color.blue(),
    "Very Strong": discord.Color.teal(),
    "Excellent": discord.Color.green(),
}


def _entropy_bar(bits: float, cap: float = 128.0, width: int = 10) -> str:
    """Renders effective-entropy bits as a filled/empty block bar, capped
    visually at `cap` bits (128 -- roughly "cryptographic strength" --
    fills the bar completely rather than needing an unbounded scale)."""
    filled = max(0, min(width, round((bits / cap) * width)))
    return "▰" * filled + "▱" * (width - filled)


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
            return await send_error(interaction, str(e))

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

    # /encode, /decode, and /encodehelp are now subcommands of a single
    # /encode group: /encode encode, /encode decode, /encode help. The
    # guild restriction moves to the group itself -- per discord.py, a
    # group's subcommands can't carry their own @app_commands.guilds(...)
    # default, so decorating the group here makes every child inherit it.
    encode_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="encode",
            description="Text encoding tools -- encode, decode (or identify), and learn how an algorithm works.",
        )
    )

    @encode_group.command(name="encode", description="Encodes text using a chosen algorithm (Base64, URL Encode, ROT13, and more).")
    @app_commands.describe(text="The text to encode", algorithm="Algorithm to encode with")
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in ENCODING_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def encode_encode(self, interaction: discord.Interaction, text: str, algorithm: app_commands.Choice[str]):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await send_error(
                interaction,
                "`Identify` only applies to `/encode decode` -- it guesses how existing encoded text was "
                "produced. Pick a specific algorithm here to encode into.",
            )

        try:
            result = encode_text(algorithm.value, text)
        except ValueError as e:
            return await send_error(interaction, str(e))

        embed = build_embed(
            title="🔒 Encode Result",
            color=discord.Color.blue(),
            fields=[
                ("Algorithm", f"`{algorithm.name}`", False),
                ("Before", f"```{_safe_codeblock(text)}```", False),
                ("After", f"```{_safe_codeblock(result)}```", False),
            ],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @encode_group.command(name="decode", description="Decodes text using a chosen algorithm, or estimates which one was used.")
    @app_commands.describe(text="The text to decode", algorithm="Algorithm to decode with, or Identify to estimate which one was used")
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in ENCODING_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def encode_decode(self, interaction: discord.Interaction, text: str, algorithm: app_commands.Choice[str]):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await self._identify_and_respond(interaction, text)

        try:
            result = decode_text(algorithm.value, text)
        except ValueError as e:
            return await send_error(interaction, str(e))

        embed = build_embed(
            title="🔓 Decode Result",
            color=discord.Color.blue(),
            fields=[
                ("Algorithm", f"`{algorithm.name}`", False),
                ("Before", f"```{_safe_codeblock(text)}```", False),
                ("After", f"```{_safe_codeblock(result)}```", False),
            ],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @encode_group.command(
        name="help",
        description="Explains how an encoding works, with an example and whether it's actually secure.",
    )
    @app_commands.describe(algorithm="Which encode/decode algorithm to learn about")
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in ENCODING_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def encode_help(self, interaction: discord.Interaction, algorithm: app_commands.Choice[str]):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await send_error(
                interaction,
                "`Identify` isn't an algorithm itself -- it's an `/encode decode` mode that guesses how "
                "existing text was encoded by looking at its shape. Pick a specific algorithm from the list "
                "to learn about that one.",
            )

        help_entry = ENCODING_HELP[algorithm.value]
        demo_text = get_demo_text(algorithm.value)

        fields = [("How It Works", help_entry["how_it_works"], False)]

        # Generate the worked example by running the real encode engine
        # against a demo phrase, rather than hard-coding the result here --
        # this way the example can never drift out of sync with what
        # /encode encode and /encode decode actually produce.
        try:
            example_result = encode_text(algorithm.value, demo_text)
        except ValueError:
            example_result = None

        if example_result:
            fields.append((
                "Example",
                f"Before: ```{_safe_codeblock(demo_text)}```After: ```{_safe_codeblock(example_result)}```",
                False,
            ))

        fields.append(("Is It Secure?", help_entry["security"], False))
        fields.append(("Industry Standard?", help_entry["standard"], False))

        embed = build_embed(title=f"📖 {algorithm.name} Explained", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    async def _identify_and_respond(self, interaction: discord.Interaction, text: str):
        guesses = identify_encoding(text)

        if not guesses:
            return await send_error(
                interaction,
                "Couldn't confidently identify how this text was encoded -- nothing about its shape "
                "matched a supported algorithm. Try a specific algorithm from the list instead.",
            )

        top_key, top_reason = guesses[0]
        top_name = ENCODING_ALGORITHMS[top_key]["name"]

        fields = [("Best Guess", f"`{top_name}` -- {top_reason}", False)]

        try:
            decoded = decode_text(top_key, text)
            fields.append(("Attempted Decode", f"```{_safe_codeblock(decoded)}```", False))
        except ValueError as e:
            fields.append(("Attempted Decode", f"Decoding as `{top_name}` failed: {e}", False))

        remaining = guesses[1:]
        if remaining:
            lines = [f"`{ENCODING_ALGORITHMS[key]['name']}` -- {reason}" for key, reason in remaining]
            fields.append(("Other Possibilities", _safe_codeblock("\n".join(lines), limit=900), False))

        embed = build_embed(title="🔍 Identify Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="diff", description="Compares two text files and shows what changed between them.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        original="The original/base file (e.g. the version before changes)",
        new="The new file to compare against the original (e.g. the version after changes)",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def diff_cmd(self, interaction: discord.Interaction, original: discord.Attachment, new: discord.Attachment):
        await interaction.response.defer(ephemeral=True)

        for attachment in (original, new):
            if attachment.size > MAX_DIFF_ATTACHMENT_SIZE:
                return await send_error(
                    interaction,
                    f"`{attachment.filename}` is too large to diff ({attachment.size:,} bytes -- "
                    f"the limit is {MAX_DIFF_ATTACHMENT_SIZE:,} bytes).",
                )

        try:
            original_bytes = await original.read()
            new_bytes = await new.read()
        except discord.HTTPException as e:
            return await send_error(interaction, f"Failed to download one of the attachments: {e}")

        try:
            original_text = original_bytes.decode("utf-8")
            new_text = new_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return await send_error(
                interaction,
                f"Couldn't read one of the files as UTF-8 text ({e}). `/diff` only supports plain-text "
                "files (source code, JSON, config files, etc.), not binaries.",
            )

        # Uses the first file as the base, same convention as /rollback
        # (before -> after), so added/removed lines in the output read the
        # same way a normal `diff original new` would.
        diff_lines = build_unified_diff(
            original_text, new_text,
            fromfile=original.filename,
            tofile=new.filename,
        )

        diff_filename = f"diff_{_sanitize_filename_part(original.filename)}_to_{_sanitize_filename_part(new.filename)}.diff"

        await send_diff_result(
            interaction,
            diff_lines,
            description=f"Comparing `{original.filename}` (original) → `{new.filename}` (new).",
            no_changes_message="No changes -- the two files are identical.",
            filename=diff_filename,
        )

    @app_commands.command(name="entropy", description="Estimates a password/string's strength and how long it'd take to crack.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(text="The password, key, or string to analyze -- never echoed back or logged")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def entropy_cmd(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
        try:
            result = analyze_entropy(text)
        except ValueError as e:
            return await send_error(interaction, str(e))

        bar = _entropy_bar(result.effective_bits)
        color = ENTROPY_RATING_COLORS.get(result.rating, discord.Color.blue())
        pool_desc = ", ".join(result.char_classes) if result.char_classes else "none detected"
        crack_lines = "\n".join(f"{label:<40}{duration}" for label, duration in result.crack_times)

        embed = build_embed(
            title="🔑 Entropy Analysis",
            color=color,
            fields=[
                ("Rating", f"**{result.rating}**   `{bar}`", False),
                ("Length", f"{result.length} characters", True),
                ("Character Pool", f"{result.pool_size} possible chars ({pool_desc})", True),
                (
                    "Entropy",
                    f"Effective: **{result.effective_bits:.1f} bits**\n"
                    f"Raw (pool size only, no pattern check): {result.raw_bits:.1f} bits",
                    False,
                ),
                ("Weaknesses Detected", "\n".join(f"• {w}" for w in result.weaknesses), False),
                ("Estimated Time To Crack", f"```{crack_lines}```", False),
            ],
            footer="Heuristic estimate only -- doesn't check real breach databases or dictionary words.",
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="coinflip", description="Flips a coin -- a traditional 50/50 Heads or Tails.")
    @app_commands.guilds(GUILD)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def coinflip_cmd(self, interaction: discord.Interaction):
        # random.choice() over a straight 2-item sequence -- a plain,
        # unweighted 50/50 pick, same as a traditional physical coin flip
        # (no loaded odds, no third "landed on its edge" outcome).
        result = random.choice(("Heads", "Tails"))
        embed = build_embed(
            title="🪙 Coin Flip",
            color=discord.Color.gold(),
            fields=[("Result", f"**{result}**", False)],
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
