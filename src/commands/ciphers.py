import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_error, build_embed
from api.ciphers import CIPHER_ALGORITHMS, CIPHER_CHOICES, cipher_text, decipher_text, identify_cipher, IDENTIFY_CHOICE_VALUE
from api.cipher_help import CIPHER_HELP, DEMO_PLAINTEXT, get_demo_key

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

    # All three classic-cipher commands (formerly the standalone /cipher,
    # /decipher, and /cipherhelp) now live as subcommands under a single
    # /cipher group: /cipher encrypt, /cipher decrypt, /cipher help. The
    # guild restriction moves to the group itself -- per discord.py, a
    # group's subcommands can't carry their own @app_commands.guilds(...)
    # default, so decorating the group here makes every child inherit it.
    cipher_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="cipher",
            description="Classic cipher tools -- encrypt, decrypt (or identify), and learn how a cipher works.",
        )
    )

    @cipher_group.command(name="encrypt", description="Encrypts text with a classic cipher (Caesar, Vigenère, Playfair, and more).")
    @app_commands.describe(
        text="The text to cipher",
        algorithm="Which cipher to use",
        key="Key/keyword for the cipher (optional for some -- blank uses the default or generates one)",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in CIPHER_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def cipher_encrypt(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: Optional[str] = None,
    ):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await send_error(
                interaction,
                "`Identify` only applies to `/cipher decrypt` -- it guesses which classic cipher produced "
                "existing ciphertext. Pick a specific cipher here to encrypt with.",
            )

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

    @cipher_group.command(name="decrypt", description="Decrypts text with a classic cipher, or estimates which one was used.")
    @app_commands.describe(
        text="The ciphertext to decipher",
        algorithm="Which cipher to decrypt with, or Identify to guess which one was used",
        key="Key/keyword the text was ciphered with (required unless that cipher has no key)",
    )
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in CIPHER_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def cipher_decrypt(
        self,
        interaction: discord.Interaction,
        text: str,
        algorithm: app_commands.Choice[str],
        key: Optional[str] = None,
    ):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await self._identify_and_respond(interaction, text)

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

    @cipher_group.command(
        name="help",
        description="Explains how a classic cipher works, with an example, and whether it's actually secure.",
    )
    @app_commands.describe(algorithm="Which cipher/decipher algorithm to learn about")
    @app_commands.choices(algorithm=[app_commands.Choice(name=name, value=value) for name, value in CIPHER_CHOICES])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def cipher_help(self, interaction: discord.Interaction, algorithm: app_commands.Choice[str]):
        if algorithm.value == IDENTIFY_CHOICE_VALUE:
            return await send_error(
                interaction,
                "`Identify` isn't a cipher itself -- it's a `/cipher decrypt` mode that guesses which classic "
                "cipher produced existing ciphertext by looking at its shape and letter patterns. Pick a "
                "specific cipher from the list to learn about that one.",
            )

        entry = CIPHER_ALGORITHMS[algorithm.value]
        help_entry = CIPHER_HELP[algorithm.value]

        fields = [("How It Works", help_entry["how_it_works"], False)]

        # Generate the worked example by running the real cipher engine
        # against a fixed demo phrase, rather than hard-coding ciphertext
        # here -- this way the example can never drift out of sync with
        # what /cipher encrypt and /cipher decrypt actually produce.
        try:
            example_result, used_key = cipher_text(algorithm.value, DEMO_PLAINTEXT, get_demo_key(algorithm.value))
        except ValueError:
            example_result, used_key = None, None

        if example_result:
            fields.append((
                "Example",
                f"Plain: ```{DEMO_PLAINTEXT}```Enciphered: ```{_safe_codeblock(example_result)}```",
                False,
            ))
            key_field = _key_field(algorithm.value, used_key, generated=False)
            if key_field:
                fields.append(key_field)

        fields.append(("Is It Secure?", help_entry["security"], False))
        fields.append(("Industry Standard?", help_entry["standard"], False))
        if entry.get("note"):
            fields.append(("Note", entry["note"], False))

        embed = build_embed(title=f"📖 {entry['name']} Explained", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)

    async def _identify_and_respond(self, interaction: discord.Interaction, text: str):
        guesses = identify_cipher(text)

        if not guesses:
            return await send_error(
                interaction,
                "Couldn't confidently identify which cipher produced this text -- nothing about its shape or "
                "letter statistics matched a supported algorithm. Try a specific cipher from the list instead.",
            )

        top_key, top_reason = guesses[0]
        top_name = CIPHER_ALGORITHMS[top_key]["name"]

        fields = [("Best Guess", f"`{top_name}` -- {top_reason}", False)]

        # A Caesar guess's reason may embed the specific shift found via
        # brute force (e.g. "key `7`") -- reuse that instead of falling
        # back to the generic default shift of 3 when attempting a decode.
        suggested_key = None
        shift_match = re.search(r"key `(\d+)`", top_reason)
        if top_key == "caesar" and shift_match:
            suggested_key = shift_match.group(1)

        try:
            decoded, _ = decipher_text(top_key, text, suggested_key)
            fields.append(("Attempted Decode", f"```{_safe_codeblock(decoded)}```", False))
        except ValueError as e:
            fields.append(("Attempted Decode", f"Deciphering as `{top_name}` failed: {e}", False))

        remaining = guesses[1:]
        if remaining:
            lines = [f"`{CIPHER_ALGORITHMS[key]['name']}` -- {reason}" for key, reason in remaining]
            fields.append(("Other Possibilities", _safe_codeblock("\n".join(lines), limit=900), False))

        embed = build_embed(title="🔍 Identify Result", color=discord.Color.blue(), fields=fields)
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ciphers(bot))
