"""/obfuscate -- AST-based Luau source protection command."""

from __future__ import annotations

import os
import re
import secrets
import tempfile

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import build_embed, has_role, is_in_guild, safe_defer, send_error
from obfuscator import obfuscate
from obfuscator.config import DEFAULT_CONFIG
from obfuscator.parser import ParserDependencyError, LuauSyntaxError

GUILD = discord.Object(id=config.GUILD_ID)
MAX_SOURCE_ATTACHMENT_SIZE = DEFAULT_CONFIG.max_input_bytes
_USED_OUTPUT_NAMES: set[str] = set()


def _safe_stem(name: str) -> str:
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem)
    stem = stem[:48]
    return stem or "script"


def _random_output_name() -> str:
    # Keep a process-local registry so even an astronomically unlikely random
    # collision is retried instead of reusing a filename.
    while True:
        name = f"protected_{secrets.token_hex(6)}.luau"
        if name not in _USED_OUTPUT_NAMES:
            _USED_OUTPUT_NAMES.add(name)
            return name


class Obfuscate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="obfuscate", description="AST-obfuscates a Luau source file and returns a protected .luau file.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(file="Luau source file to obfuscate")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def obfuscate_cmd(self, interaction: discord.Interaction, file: discord.Attachment):
        if not await safe_defer(interaction, ephemeral=True):
            return

        if file.size > MAX_SOURCE_ATTACHMENT_SIZE:
            return await send_error(
                interaction,
                f"`{file.filename}` is too large to obfuscate ({file.size:,} bytes; "
                f"the limit is {MAX_SOURCE_ATTACHMENT_SIZE:,} bytes).",
            )

        try:
            raw = await file.read()
        except discord.HTTPException as exc:
            return await send_error(interaction, f"Failed to download `{file.filename}`: {exc}")

        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return await send_error(interaction, "The uploaded file is not valid UTF-8 Luau source.")

        try:
            result = obfuscate(source, config=DEFAULT_CONFIG)
        except (LuauSyntaxError, ParserDependencyError, ValueError) as exc:
            return await send_error(interaction, str(exc))
        except Exception as exc:
            # Do not leak parser internals or source content into a Discord
            # response, but keep the traceback in the bot process logs.
            print(f"/obfuscate failed for {file.filename!r}: {exc!r}")
            return await send_error(interaction, "Obfuscation failed unexpectedly. Check the bot logs for details.")

        filename = _random_output_name()
        output_bytes = bytes(result.source)
        if not output_bytes:
            return await send_error(interaction, "Obfuscation produced an empty output file.")

        # Use a real temporary file rather than a shared BytesIO in the
        # interaction response. discord.File objects are single-use, and a
        # retry/follow-up path can otherwise leave a previously-consumed buffer
        # at EOF and make Discord receive a 0-byte attachment.
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix="celestial-obf-", suffix=".luau")
            with os.fdopen(fd, "wb") as temp_file:
                temp_file.write(output_bytes)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            if os.path.getsize(temp_path) != len(output_bytes):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                temp_path = None
                return await send_error(interaction, "Failed to stage the protected file for upload.")

            discord_file = discord.File(temp_path, filename=filename)
        except OSError as exc:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            print(f"/obfuscate staging failed for {file.filename!r}: {exc!r}")
            return await send_error(interaction, "Failed to prepare the protected file for upload.")

        original_name = _safe_stem(file.filename)
        embed = build_embed(
            title="🛡️ Luau Protection Complete",
            description=(
                f"`{original_name}` has been successfully protected by the **Celestial Luau Obfuscator**.\n\n"
                f"[**Download the protected file**](attachment://{filename})"
            ),
            color=discord.Color.green(),
            fields=[
                ("📄 Source File", f"`{original_name}`", True),
                ("📦 Protected File", f"`{filename}`", True),
                ("📏 Size", f"{result.stats.input_bytes:,} B → {result.stats.output_bytes:,} B", True),
                ("🧠 Source Complexity", f"{result.stats.source_complexity}/100", True),
                ("🔤 Local Renaming", f"{result.stats.renamed_locals:,}", True),
                ("🔐 Encrypted Strings", f"{result.stats.encrypted_strings:,}", True),
                ("🧮 Obfuscated Constants", f"{result.stats.obfuscated_constants:,}", True),
                ("🔀 Scrambled Conditions", f"{result.stats.scrambled_conditions:,}", True),
                ("⚙️ VM Instructions", f"{result.stats.vm_instructions:,}", True),
                ("🧩 Runtime Layers", f"{result.stats.runtime_layers:,}", True),
                ("🧬 Decoder Variants", f"{result.stats.decoder_variants:,}", True),
                ("🕸️ Opaque Edges", f"{result.stats.opaque_edges:,}", True),
                ("🌐 Control-Flow Decoys", f"{result.stats.control_flow_decoys:,}", True),
                ("📚 Payload Blocks", f"{result.stats.payload_blocks:,}", True),
                ("⚡ VM Micro-Ops", f"{result.stats.micro_ops:,}", True),
                ("🧱 Dead-Code Blocks", f"{result.stats.dead_code_blocks:,}", True),
                ("🛡️ Anti-Tamper Checks", f"{result.stats.anti_tamper_checks:,}", True),
                ("🧬 Payload Layers", f"{result.stats.payload_layers:,}", True),
                ("🔐 String Pool Entries", f"{result.stats.string_pool_parts:,}", True),
                ("🧪 String Decoders", f"{result.stats.string_decoder_variants:,}", True),
                (
                    "🔒 Protection Pipeline",
                    "AST validation → source complexity profiling → adaptive build-budget selection → "
                    "byte-preserving payload capture → state-machine virtualization → "
                    "control-flow decoys → dead-code insertion → multi-layer payload encryption → "
                    "distributed anti-tamper validation → integrity verification",
                    False,
                ),
            ],
            footer="Celestial Obfuscator • Protected output generated uniquely for this build",
        )
        if interaction.client.user:
            embed.set_author(
                name="Celestial Security",
                icon_url=interaction.client.user.display_avatar.url,
            )
        else:
            embed.set_author(name="Celestial Security")
        embed.timestamp = discord.utils.utcnow()

        try:
            # The interaction has already been deferred above, so use the
            # follow-up webhook directly. This avoids the generic responder's
            # retry path reusing a single-use discord.File object.
            message = await interaction.followup.send(
                embed=embed,
                file=discord_file,
                ephemeral=True,
                wait=True,
            )

            # Discord returns the uploaded attachment metadata on webhook
            # messages. Verify that the server received the expected number of
            # bytes rather than silently accepting a 0-byte attachment.
            attachments = getattr(message, "attachments", ())
            if attachments and getattr(attachments[0], "size", len(output_bytes)) == 0:
                print(
                    f"/obfuscate Discord reported a 0-byte attachment for {filename!r}; "
                    f"expected {len(output_bytes):,} bytes; retrying upload once"
                )

                # Never reuse a discord.File instance: discord.py documents
                # File objects as single-use. Re-open the staged path so the
                # retry starts with a fresh file object at byte 0.
                try:
                    await message.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

                retry_file = discord.File(temp_path, filename=filename)
                message = await interaction.followup.send(
                    embed=embed,
                    file=retry_file,
                    ephemeral=True,
                    wait=True,
                )
                retry_attachments = getattr(message, "attachments", ())
                if retry_attachments and getattr(retry_attachments[0], "size", len(output_bytes)) == 0:
                    print(
                        f"/obfuscate retry still reported a 0-byte attachment for {filename!r}; "
                        "Discord upload did not accept the staged payload"
                    )
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            print(f"/obfuscate upload failed for {filename!r}: {exc!r}")
            return await send_error(interaction, "The protected file could not be uploaded to Discord.")
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Obfuscate(bot))
