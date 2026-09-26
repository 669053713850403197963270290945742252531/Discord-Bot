"""/obfuscate -- Luau source protection command."""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from api import config
from api.discord_helpers import (
    build_embed,
    has_role,
    is_in_guild,
    safe_defer,
    send_error,
)
from obfuscator import obfuscate
from obfuscator.config import DEFAULT_CONFIG
from obfuscator.parser import ParserDependencyError, LuauSyntaxError

GUILD = discord.Object(id=config.GUILD_ID)
MAX_SOURCE_ATTACHMENT_SIZE = DEFAULT_CONFIG.max_input_bytes
_USED_OUTPUT_NAMES: set[str] = set()


def _random_output_name() -> str:
    """Return a unique randomized filename for a protected Luau artifact."""
    for _ in range(32):
        candidate = f"Celestial_{secrets.token_hex(8)}.luau"
        if candidate not in _USED_OUTPUT_NAMES:
            _USED_OUTPUT_NAMES.add(candidate)
            return candidate

    # A collision after 32 cryptographically-random attempts is extraordinarily
    # unlikely, but keep the function total rather than failing the command.
    candidate = f"Celestial_{secrets.token_hex(16)}.luau"
    _USED_OUTPUT_NAMES.add(candidate)
    return candidate


def _safe_stem(filename: str) -> str:
    """Return a bounded, filesystem-safe display stem from an uploaded name."""
    basename = os.path.basename(filename.replace("\\", "/"))
    stem, _ = os.path.splitext(basename)
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem).strip(" .")
    return (stem or "source")[:100]


VM_COMPRESSION_DESCRIPTION = (
    "Losslessly compresses the VM payload before it is encrypted and embedded. "
    "It helps most with larger or repetitive source files; the final protected "
    "file can still be slightly larger when the compression runtime costs more "
    "than the bytes saved."
)
@dataclass(frozen=True, slots=True)
class ObfuscationOptions:
    vm_compression: bool = False


async def _run_obfuscation(
    interaction: discord.Interaction,
    *,
    source: str,
    original_filename: str,
    options: ObfuscationOptions,
) -> None:
    """Run the obfuscation pipeline with the selected command options."""
    if not await safe_defer(interaction, ephemeral=True):
        return

    selected_config = DEFAULT_CONFIG.__class__(
        **{
            **{field: getattr(DEFAULT_CONFIG, field) for field in DEFAULT_CONFIG.__dataclass_fields__},
            "vm_compression": options.vm_compression,
        }
    )

    try:
        result = obfuscate(source, config=selected_config)
    except (LuauSyntaxError, ParserDependencyError, ValueError) as exc:
        return await send_error(interaction, str(exc))
    except Exception as exc:
        # Do not leak parser internals or source content into a Discord
        # response, but keep the traceback in the bot process logs.
        print(f"/obfuscate failed for {original_filename!r}: {exc!r}")
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
        print(f"/obfuscate staging failed for {original_filename!r}: {exc!r}")
        return await send_error(interaction, "Failed to prepare the protected file for upload.")

    original_name = _safe_stem(original_filename)
    if options.vm_compression:
        payload_source_bytes = result.stats.vm_compression_source_bytes
        payload_bytes = result.stats.vm_compression_payload_bytes
        baseline_bytes = result.stats.vm_compression_baseline_output_bytes
        saved_bytes = result.stats.vm_compression_saved_bytes
        source_size = result.stats.input_bytes
        complexity = result.stats.source_complexity
        if source_size < 1_024:
            size_class = "very small"
        elif source_size < 8_192:
            size_class = "small"
        elif source_size < 65_536:
            size_class = "medium"
        else:
            size_class = "large"
        if complexity <= 25:
            complexity_class = "low"
        elif complexity <= 50:
            complexity_class = "moderate"
        elif complexity <= 75:
            complexity_class = "high"
        else:
            complexity_class = "very high"
        if payload_source_bytes > 0 and result.stats.vm_compression_applied:
            payload_delta = (1 - (payload_bytes / payload_source_bytes)) * 100
            payload_text = f"Payload: {payload_source_bytes:,} B → {payload_bytes:,} B ({payload_delta:.1f}% smaller)."
        elif payload_source_bytes > 0:
            payload_text = "Payload: compression was evaluated but the encoded payload was not smaller, so the codec was not applied."
        else:
            payload_text = "Payload: 0 B (no payload bytes to compress)."
        if baseline_bytes > 0:
            final_delta = (abs(saved_bytes) / baseline_bytes) * 100
            if saved_bytes > 0:
                final_text = f"Final protected file: {baseline_bytes:,} B → {result.stats.output_bytes:,} B ({final_delta:.1f}% smaller)."
            elif saved_bytes < 0:
                final_text = f"Final protected file: {baseline_bytes:,} B → {result.stats.output_bytes:,} B ({final_delta:.1f}% larger)."
            else:
                final_text = f"Final protected file: {result.stats.output_bytes:,} B (no size change)."
        else:
            final_text = f"Final protected file: {result.stats.output_bytes:,} B."
        context = f"This was a {size_class} source ({source_size:,} B) with {complexity_class} structural complexity ({complexity}/100)."
        if saved_bytes > 0:
            benefit_text = "Compression benefited this build because the complete protected artifact is smaller than the identical uncompressed build."
        elif saved_bytes == 0:
            benefit_text = "Compression did not change the complete protected artifact size for this build."
        else:
            benefit_text = "Compression did not benefit this build because its runtime/metadata overhead outweighed the payload savings."
        compression_summary = (
            "Enabled. " + payload_text + " " + final_text + " " + context + " " + benefit_text
        )
    else:
        compression_summary = "Disabled. No compression codec was added to the VM payload."
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
            (
                "🗜️ VM Compression",
                compression_summary,
                False,
            ),
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
                "⚙️ Build Options",
                f"VM Compression: {'Enabled' if options.vm_compression else 'Disabled'}",
                False,
            ),
            (
                "ℹ️ Option Details",
                (
                    f"**VM Compression:** {VM_COMPRESSION_DESCRIPTION}"
                    if options.vm_compression
                    else "**VM Compression:** Disabled. No compression codec was added to the payload."
                ),
                False,
            ),
            (
                "🔒 Protection Pipeline",
                "AST validation → source complexity profiling → adaptive build-budget selection → "
                "byte-preserving payload capture → Intense VM Structure → state-machine virtualization → "
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


class Obfuscate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="obfuscate",
        description="Protect a Luau source file with optional VM compression.",
    )
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        file="Luau source file to obfuscate",
        vm_compression="Enable lossless VM payload compression (default: false)",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def obfuscate_cmd(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        vm_compression: bool = False,
    ):
        filename_lower = file.filename.lower()
        if not filename_lower.endswith((".lua", ".luau")):
            return await send_error(interaction, "Upload a `.lua` or `.luau` source file.")

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

        options = ObfuscationOptions(
            vm_compression=bool(vm_compression),
        )
        await _run_obfuscation(
            interaction,
            source=source,
            original_filename=file.filename,
            options=options,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Obfuscate(bot))
