"""/obfuscate -- Luau source protection command."""

from __future__ import annotations

import asyncio
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
    edit_or_send_error,
    send_error,
)
from obfuscator import obfuscate
from obfuscator.config import DEFAULT_CONFIG
from obfuscator.parser import ParserDependencyError, LuauSyntaxError
from api.protected_artifacts import (
    ProtectedArtifactError,
    create_protected_artifact,
    render_loader,
    validate_public_endpoint,
)

GUILD = discord.Object(id=config.GUILD_ID)
MAX_SOURCE_ATTACHMENT_SIZE = DEFAULT_CONFIG.max_input_bytes
_USED_OUTPUT_NAMES: set[str] = set()


def _random_output_name(extension: str = ".luau") -> str:
    """Return a unique randomized filename while preserving the input extension."""
    extension = str(extension or ".luau").lower()
    if extension not in {".lua", ".luau", ".txt"}:
        extension = ".luau"
    for _ in range(32):
        candidate = f"Celestial_{secrets.token_hex(8)}{extension}"
        if candidate not in _USED_OUTPUT_NAMES:
            _USED_OUTPUT_NAMES.add(candidate)
            return candidate

    # A collision after 32 cryptographically-random attempts is extraordinarily
    # unlikely, but keep the function total rather than failing the command.
    candidate = f"Celestial_{secrets.token_hex(16)}{extension}"
    _USED_OUTPUT_NAMES.add(candidate)
    return candidate


def _loader_obfuscation_config():
    """Build a source-transform-only profile for the generated bootstrap.

    The loader must execute directly in the target Luau environment, so it is
    intentionally not virtualized. Local renaming, encrypted string literals,
    integer-expression rewriting, and comment stripping obscure the endpoint,
    per-build ID, bearer token, and loader structure without adding a second VM.
    """
    values = {
        field: getattr(DEFAULT_CONFIG, field)
        for field in DEFAULT_CONFIG.__dataclass_fields__
    }
    values.update(
        semantic_safe=False,
        rename_locals=True,
        encrypt_strings=True,
        obfuscate_integer_constants=True,
        scramble_control_flow=False,
        strip_comments=True,
        virtualize=False,
        vm_compression=False,
        noise_blocks=0,
    )
    return DEFAULT_CONFIG.__class__(**values)


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
    """Run the obfuscation pipeline while updating one message in place."""
    loop = asyncio.get_running_loop()
    original_name = _safe_stem(original_filename)

    def _progress_step(stage: str, explicit_step: str | None = None) -> str:
        if explicit_step:
            return explicit_step

        stage_steps = {
            "Preparing source": "1 / 5",
            "Parsing & validating Luau": "2 / 5",
            "Preparing source transformations": "2 / 5",
            "Re-validating protected source": "2 / 5",
            "Injecting protected scaffolding": "3 / 5",
            "Compiling & virtualizing": "3 / 5",
            "Building randomized VM": "3 / 5",
            "Falling back safely": "3 / 5",
            "Evaluating VM compression": "4 / 5",
            "Finalizing protected payload": "4 / 5",
            "Obfuscating the bootstrap loader": "5 / 5",
            "Attaching protected loader": "5 / 5",
        }
        return stage_steps.get(stage, "3 / 5")

    async def update_progress(stage: str, detail: str, *, step: str | None = None) -> None:
        fields = [
            ("📊 Progress", _progress_step(stage, step), True),
            ("📄 Source File", f"`{original_name}`", True),
            ("⚙️ Current Stage", stage, False),
            ("ℹ️ Details", detail, False),
        ]
        embed = build_embed(
            title="🛡️ Obfuscating...",
            description=(
                f"`{original_name}` is being processed by the **Celestial Luau Obfuscator**.\n\n"
                "This message will update in place as each major stage completes. The delivered file uses server-backed protection and needs network access at runtime."
            ),
            color=discord.Color.blurple(),
            fields=fields,
            footer="Celestial Obfuscator • Processing in progress",
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
            await interaction.edit_original_response(content=None, embed=embed)
        except discord.NotFound:
            print(f"/obfuscate progress message disappeared while processing {original_filename!r}")
        except discord.HTTPException as exc:
            print(f"/obfuscate progress edit failed for {original_filename!r}: {exc!r}")

    def pipeline_progress(stage: str, detail: str) -> None:
        """Bridge synchronous pipeline progress back onto the bot event loop."""
        future = asyncio.run_coroutine_threadsafe(
            update_progress(stage, detail),
            loop,
        )
        try:
            future.result(timeout=15)
        except Exception:
            # Progress must never make the actual obfuscation fail.
            pass

    await update_progress(
        "Preparing source",
        "The uploaded attachment is being read and decoded before validation begins.",
        step="1 / 5",
    )

    selected_config = DEFAULT_CONFIG.__class__(
        **{
            **{field: getattr(DEFAULT_CONFIG, field) for field in DEFAULT_CONFIG.__dataclass_fields__},
            "vm_compression": options.vm_compression,
        }
    )

    await update_progress(
        "Parsing & validating Luau",
        "Checking syntax before any protection transforms or VM generation are performed.",
        step="2 / 5",
    )

    try:
        result = await asyncio.to_thread(
            obfuscate,
            source,
            config=selected_config,
            progress_callback=pipeline_progress,
        )
    except (LuauSyntaxError, ParserDependencyError, ValueError) as exc:
        return await edit_or_send_error(interaction, str(exc))
    except Exception as exc:
        # Do not leak parser internals or source content into a Discord
        # response, but keep the traceback in the bot process logs.
        print(f"/obfuscate failed for {original_filename!r}: {exc!r}")
        return await edit_or_send_error(
            interaction,
            "Obfuscation failed unexpectedly. Check the bot logs for details.",
        )

    await update_progress(
        "Generating protected artifact",
        "The VM payload, encrypted constants, anti-tamper data, and integrity metadata are being finalized.",
        step="4 / 5",
    )

    extension = os.path.splitext(os.path.basename(original_filename.replace("\\", "/")))[1].lower()
    filename = _random_output_name(extension)
    payload_bytes = bytes(result.source)
    if not payload_bytes:
        return await edit_or_send_error(interaction, "Obfuscation produced an empty output file.")

    # A server-backed loader is intentionally attached instead of embedding the
    # encrypted program. The actual VM artifact is encrypted under an HKDF-derived
    # per-build key and stored in a private bucket; no game/license check is used.
    try:
        endpoint_base = validate_public_endpoint(config.LICENSE_SERVER_BASE_URL)
        await update_progress(
            "Encrypting & storing build",
            "Encrypting this universal build with a server-derived per-build key and storing it in private Supabase Storage.",
            step="4 / 5",
        )
        artifact = await asyncio.to_thread(create_protected_artifact, payload_bytes)
        loader_source = render_loader(endpoint_base, artifact["build_id"], artifact["token"])
        await update_progress(
            "Obfuscating the bootstrap loader",
            "Renaming loader locals, encrypting embedded strings, and rewriting numeric constants before attachment.",
            step="5 / 5",
        )
        loader_result = await asyncio.to_thread(
            obfuscate,
            loader_source,
            config=_loader_obfuscation_config(),
        )
        output_bytes = bytes(loader_result.source)
        if not output_bytes:
            raise ValueError("Loader obfuscation produced an empty file.")

        # These metadata values must be recoverable at runtime, but should not
        # remain readable as source literals in the Discord attachment.
        expected_hidden_values = (
            artifact["build_id"].encode("utf-8"),
            artifact["token"].encode("utf-8"),
            (endpoint_base.rstrip("/") + "/obfuscator/artifact").encode("utf-8"),
        )
        if any(value and value in output_bytes for value in expected_hidden_values):
            raise ValueError("Loader obfuscation left sensitive bootstrap metadata readable.")
    except ProtectedArtifactError as exc:
        print(f"/obfuscate artifact protection setup failed for {original_filename!r}: {exc.reason}")
        if exc.reason == "public_https_artifact_endpoint_required":
            return await edit_or_send_error(
                interaction,
                "Server-protected downloads require `LICENSE_SERVER_BASE_URL` to be set to the public HTTPS URL of this bot (for example, your Render service URL).",
            )
        if exc.reason == "artifact_protection_not_configured":
            return await edit_or_send_error(
                interaction,
                "Server-side artifact protection is not configured. Set `CELESTIAL_ARTIFACT_MASTER_KEY` to a high-entropy secret and restart the service.",
            )
        if exc.reason == "artifact_storage_not_configured":
            return await edit_or_send_error(
                interaction,
                "Protected artifact storage is not configured. Create the private Supabase Storage bucket named by `SUPABASE_PROTECTED_ARTIFACTS_BUCKET` and verify the server's Supabase credentials.",
            )
        return await edit_or_send_error(
            interaction,
            "The protected artifact could not be encrypted or saved. Check the bot logs and Supabase Storage configuration.",
        )
    except Exception as exc:
        print(f"/obfuscate artifact protection/loader generation failed for {original_filename!r}: {exc!r}")
        return await edit_or_send_error(
            interaction,
            "The protected build or its obfuscated loader could not be finalized. Check the bot logs for details and retry the build.",
        )

    # Use a real temporary file rather than a shared BytesIO in the interaction
    # response. discord.File objects are single-use, and a retry path can
    # otherwise leave a previously-consumed buffer at EOF and make Discord
    # receive a 0-byte attachment.
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix="celestial-obf-", suffix=extension)
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
            return await edit_or_send_error(
                interaction,
                "Failed to stage the protected file for upload.",
            )
    except OSError as exc:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        print(f"/obfuscate staging failed for {original_filename!r}: {exc!r}")
        return await edit_or_send_error(
            interaction,
            "Failed to prepare the protected file for upload.",
        )

    await update_progress(
        "Attaching protected loader",
        f"`{filename}` is obfuscated and staged ({len(output_bytes):,} bytes); it is being added to this message.",
        step="5 / 5",
    )

    discord_file = discord.File(temp_path, filename=filename)

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
                final_text = f"Stored obfuscated payload: {baseline_bytes:,} B → {result.stats.output_bytes:,} B ({final_delta:.1f}% smaller)."
            elif saved_bytes < 0:
                final_text = f"Stored obfuscated payload: {baseline_bytes:,} B → {result.stats.output_bytes:,} B ({final_delta:.1f}% larger)."
            else:
                final_text = f"Stored obfuscated payload: {result.stats.output_bytes:,} B (no size change)."
        else:
            final_text = f"Stored obfuscated payload: {result.stats.output_bytes:,} B."
        context = f"This was a {size_class} source ({source_size:,} B) with {complexity_class} structural complexity ({complexity}/100)."
        if saved_bytes > 0:
            benefit_text = "Compression benefited this build because the stored obfuscated payload is smaller than the identical uncompressed build."
        elif saved_bytes == 0:
            benefit_text = "Compression did not change the stored obfuscated payload size for this build."
        else:
            benefit_text = "Compression did not benefit this build because its runtime/metadata overhead outweighed the payload savings."
        compression_summary = (
            "Enabled. " + payload_text + " " + final_text + " " + context + " " + benefit_text
        )
    else:
        compression_summary = "Disabled. No compression codec was added to the VM payload."

    backend_name = result.stats.vm_backend
    if result.stats.vm_fallback:
        backend_details = (
            "Legacy semantic-safe source VM was used. "
            f"Compiler fallback reason: {result.stats.vm_fallback_reason}"
        )
    else:
        backend_details = (
            f"Functions: {result.stats.bytecode_functions:,} • "
            f"Constants: {result.stats.bytecode_constants:,} • "
            f"Registers: {result.stats.bytecode_registers:,}"
        )
    if result.stats.vm_fallback:
        pipeline_description = (
            "AST validation → source complexity profiling → adaptive build-budget selection → "
            "byte-preserving payload capture → adaptive Intense VM Structure → state-machine virtualization → "
            "control-flow decoys → dead-code insertion → multi-layer payload encryption → "
            "distributed anti-tamper validation → integrity verification"
        )
    else:
        pipeline_description = (
            "AST validation → Luau-to-custom-bytecode compilation → encrypted constant pool → "
            "adaptive Intense VM Structure → randomized register/stack VM → randomized dispatch → "
            "control-flow decoys → dead-code insertion → encrypted bytecode payload → integrity verification"
        )

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
            ("📏 Source → Protected Payload", f"{result.stats.input_bytes:,} B → {result.stats.output_bytes:,} B", True),
            ("📎 Attached Loader", f"{len(output_bytes):,} B • obfuscated • requires HTTPS to the Celestial artifact server", True),
            (
                "🛡️ Loader Obfuscation",
                f"{loader_result.stats.renamed_locals:,} locals renamed • "
                f"{loader_result.stats.encrypted_strings:,} strings encrypted • "
                f"{loader_result.stats.obfuscated_constants:,} constants rewritten",
                False,
            ),
            ("🔐 Server-Side Protection", "AES-256-GCM encrypted storage • HKDF-derived per-build key • token-gated retrieval • no game/license binding", False),
            ("🧠 Source Complexity", f"{result.stats.source_complexity}/100", True),
            ("⚙️ VM Backend", backend_name, True),
            ("🗜️ VM Compression", compression_summary, False),
            ("🔤 Local Renaming", f"{result.stats.renamed_locals:,}", True),
            ("🔐 Encrypted Strings", f"{result.stats.encrypted_strings:,}", True),
            ("🧮 Obfuscated Constants", f"{result.stats.obfuscated_constants:,}", True),
            ("🔀 Scrambled Conditions", f"{result.stats.scrambled_conditions:,}", True),
            ("⚙️ VM Instructions", f"{result.stats.vm_instructions:,}", True),
            ("🧩 Runtime Layers", f"{result.stats.runtime_layers:,}", True),
            ("🧾 Bytecode Details", backend_details, False),
            (
                "🧰 Runtime Protection Metrics",
                (
                    f"Decoder variants: {result.stats.decoder_variants:,} • "
                    f"Opaque edges: {result.stats.opaque_edges:,}\n"
                    f"Control-flow decoys: {result.stats.control_flow_decoys:,} • "
                    f"Payload blocks: {result.stats.payload_blocks:,}\n"
                    f"VM micro-ops: {result.stats.micro_ops:,} • "
                    f"Dead-code blocks: {result.stats.dead_code_blocks:,}\n"
                    f"Anti-tamper checks: {result.stats.anti_tamper_checks:,} • "
                    f"Payload layers: {result.stats.payload_layers:,}\n"
                    f"String pool entries: {result.stats.string_pool_parts:,} • "
                    f"String decoders: {result.stats.string_decoder_variants:,}"
                ),
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
            ("🔒 Protection Pipeline", pipeline_description, False),
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
        # Replace the same ephemeral progress message with the finalized
        # embed + attachment. Discord.py supports adding new files through the
        # attachments parameter of edit_original_response().
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            attachments=[discord_file],
        )

        # Verify that Discord accepted the expected file size. If it reports
        # an unexpected 0-byte attachment, retry the edit with a fresh File
        # object rather than creating a second message.
        try:
            message = await interaction.original_response()
            attachments = getattr(message, "attachments", ())
        except (discord.NotFound, discord.HTTPException):
            attachments = ()

        if attachments and getattr(attachments[0], "size", len(output_bytes)) == 0:
            print(
                f"/obfuscate Discord reported a 0-byte attachment for {filename!r}; "
                f"expected {len(output_bytes):,} bytes; retrying in-place edit once"
            )
            retry_file = discord.File(temp_path, filename=filename)
            await interaction.edit_original_response(
                content=None,
                embed=embed,
                attachments=[retry_file],
            )
    except discord.NotFound:
        return
    except discord.HTTPException as exc:
        print(f"/obfuscate upload/edit failed for {filename!r}: {exc!r}")
        return await edit_or_send_error(
            interaction,
            "The protected file could not be uploaded to Discord.",
        )
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
        if not filename_lower.endswith((".lua", ".luau", ".txt")):
            return await send_error(interaction, "Upload a `.lua`, `.luau`, or `.txt` file containing valid Luau source.")

        if file.size > MAX_SOURCE_ATTACHMENT_SIZE:
            return await send_error(
                interaction,
                f"`{file.filename}` is too large to obfuscate ({file.size:,} bytes; "
                f"the limit is {MAX_SOURCE_ATTACHMENT_SIZE:,} bytes).",
            )

        progress_embed = build_embed(
            title="🛡️ Obfuscating...",
            description=(
                f"`{_safe_stem(file.filename)}` is being processed by the **Celestial Luau Obfuscator**.\n\n"
                "The message will update in place as the source is validated, virtualized, and packaged."
            ),
            color=discord.Color.blurple(),
            fields=[
                ("📊 Progress", "1 / 5", True),
                ("📄 Source File", f"`{_safe_stem(file.filename)}`", True),
                ("⚙️ Current Stage", "Downloading source", False),
                ("ℹ️ Details", "Reading the uploaded attachment and preparing it for validation.", False),
            ],
            footer="Celestial Obfuscator • Processing in progress",
        )
        if interaction.client.user:
            progress_embed.set_author(
                name="Celestial Security",
                icon_url=interaction.client.user.display_avatar.url,
            )
        else:
            progress_embed.set_author(name="Celestial Security")
        progress_embed.timestamp = discord.utils.utcnow()

        try:
            await interaction.response.send_message(embed=progress_embed, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

        try:
            raw = await file.read()
        except discord.HTTPException as exc:
            return await edit_or_send_error(interaction, f"Failed to download `{file.filename}`: {exc}")

        try:
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return await edit_or_send_error(interaction, "The uploaded file is not valid UTF-8 source text.")

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
