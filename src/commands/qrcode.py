"""
/qrcode -- QR code generator + scanner, remade from a standalone tkinter
tool (Title / Text-URL / Scale / Color / Transparent / Rainbow) into a
native slash command group. `generate` keeps the old tool's options and
behavior, plus a set of additions that fall naturally out of the same
encode-then-render pipeline: a `style` option for data-module shape, an
`error_correction` level, a 2-color gradient (`gradient_color` +
`gradient_direction`), independent styling/coloring for the 3 corner
finder-pattern "eyes" (`eye_frame_style`/`eye_ball_style`/
`eye_frame_color`/`eye_ball_color`), and a center logo (`logo` +
`logo_shape`/`logo_size`/`logo_background`). `decode` is the reverse
direction, reading a QR code back out of an uploaded image.

All of the actual encoding/decoding/rendering work lives in
api.qrcode_gen -- this file is just the Discord-facing thin layer: option
parsing, validation errors, and building the result. See that module's
docstring for the scan-reliability reasoning behind several of the
choices made here (why some eye-frame styles carry a stronger warning
than others -- that's from measured testing, not a guess -- why rainbow/
logo codes get a higher error-correction floor, why decoding tries
several passes before giving up, etc).
"""

import io
import re
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Container, File, LayoutView, TextDisplay

from api import config
from api.discord_helpers import has_role, is_in_guild, send_error, build_embed, safe_respond
from api.qrcode_gen import (
    QROptions, QRResult, generate_qr, parse_color, swatch_emoji,
    SCALE_MIN, SCALE_MAX, DEFAULT_SCALE, DEFAULT_STYLE,
    ERROR_CORRECTION_LEVELS, DEFAULT_ERROR_CORRECTION, RAINBOW_DEFAULT_ERROR_CORRECTION,
    LOGO_DEFAULT_ERROR_CORRECTION,
    PRESET_COLORS, MAX_TEXT_LENGTH,
    EYE_FRAME_STYLES, DEFAULT_EYE_FRAME_STYLE,
    EYE_BALL_STYLES, DEFAULT_EYE_BALL_STYLE,
    GRADIENT_DIRECTIONS, DEFAULT_GRADIENT_DIRECTION,
    LOGO_SHAPES, DEFAULT_LOGO_SHAPE, LOGO_SIZE_MIN, LOGO_SIZE_MAX, DEFAULT_LOGO_SIZE_PERCENT,
    QRDecodeResult, QRContentInfo, decode_qr, classify_qr_content,
)

GUILD = discord.Object(id=config.GUILD_ID)

# internal style value -> display label, used for both the `style` choice
# list and /qrcode help's reference text.
STYLE_LABELS = {
    "square": "Square (classic)",
    "rounded": "Rounded",
    "dots": "Dots",
}

# internal error-correction key -> display label (ERROR_CORRECTION_LEVELS
# itself maps key -> (qrcode constant, recovery %), which isn't a display
# string -- kept separate since this is purely a Discord-facing concern).
EC_LABELS = {"L": "Low", "M": "Medium", "Q": "Quartile", "H": "High"}

# internal eye-frame/eye-ball style value -> display label, same
# autocomplete-free `choices` convention as STYLE_LABELS above. "circle"
# and "leaf" carry a (!) since -- per api.qrcode_gen's empirical testing
# -- they're meaningfully less reliable against classical QR decoders
# than the other options; this is just a label-level nudge, the real
# explanation surfaces as a result warning when picked.
EYE_FRAME_LABELS = {
    "square": "Square (default, most reliable)",
    "rounded": "Rounded",
    "circle": "Circle (!)",
    "leaf": "Leaf (!)",
}
EYE_BALL_LABELS = {
    "square": "Square (default)",
    "rounded": "Rounded",
    "circle": "Circle",
    "dot": "Dot",
    "diamond": "Diamond",
    "leaf": "Leaf",
}

# internal gradient-direction value -> display label.
GRADIENT_DIRECTION_LABELS = {
    "top_to_bottom": "Top to bottom",
    "bottom_to_top": "Bottom to top",
    "left_to_right": "Left to right",
    "right_to_left": "Right to left",
    "diagonal": "Diagonal (default, top-left to bottom-right)",
    "diagonal_reverse": "Diagonal (top-right to bottom-left)",
    "radial": "Radial (center outward)",
}

# internal logo-shape value -> display label.
LOGO_SHAPE_LABELS = {
    "square": "Square",
    "circle": "Circle (default)",
    "rounded": "Rounded square",
}

# Same convention as commands.utility.MAX_DIFF_ATTACHMENT_SIZE -- a
# friendly cap on any image attachment this cog reads directly (the image
# uploaded to /qrcode decode, and the logo uploaded to /qrcode generate),
# generous enough for a full-resolution phone photo (api.qrcode_gen
# downscales/resizes large images itself before scanning or compositing,
# so this is about a sane upload size, not processing performance).
MAX_IMAGE_ATTACHMENT_SIZE = 15 * 1024 * 1024  # 15 MiB


def _sanitize_filename(name: str) -> str:
    """Keeps a user-supplied `title` filesystem/Discord-safe for use as the
    attachment filename -- same sanitizing convention as
    commands.utility._sanitize_filename_part, duplicated locally per this
    codebase's per-cog private-helper pattern."""
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip())
    return base[:40] or "qrcode"


def _safe_codeblock(value: str, limit: int = 300) -> str:
    # Same truncate/escape convention as commands.utility._safe_codeblock
    # and commands.genpass._safe_codeblock -- kept short here since the
    # image itself is the actual deliverable, this is just a confirmation
    # preview of what got encoded.
    value = value.replace("```", "``\u200b`")
    if len(value) > limit:
        value = value[:limit] + "… (truncated)"
    return value


async def qr_color_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """
    Suggests preset colors (by name or hex) as `color` is typed -- the
    closest native equivalent to the old tool's "Choose" color-picker
    button, since Discord has no built-in color-picker component. Purely
    a convenience, mirroring commands.utility.hash_algorithm_autocomplete:
    never restrictive -- any hex code or CSS color name typed instead is
    still accepted and validated server-side by api.qrcode_gen.parse_color.
    """
    query = current.lower().strip()
    matches = [
        (label, hex_code) for label, hex_code in PRESET_COLORS
        if not query or query in label.lower() or query in hex_code.lower()
    ]
    return [app_commands.Choice(name=f"{label} ({hex_code})", value=hex_code) for label, hex_code in matches[:25]]


def _qr_result_layout(*, title: str, filename: str, result: QRResult, options: QROptions, ec_auto: bool) -> LayoutView:
    """Components V2 result for /qrcode generate: a stats summary above
    the attached image, in that order (same file-after-text convention as
    api.discord_helpers.file_success_layout) -- a bespoke layout rather
    than that shared helper since this needs a custom heading plus a
    multi-line stats block instead of a plain description string."""
    if options.rainbow:
        color_desc = "🌈 Rainbow gradient"
    elif options.gradient_color is not None:
        r1, g1, b1 = options.color
        r2, g2, b2 = options.gradient_color
        direction_label = GRADIENT_DIRECTION_LABELS.get(options.gradient_direction, options.gradient_direction)
        color_desc = f"`#{r1:02X}{g1:02X}{b1:02X}` → `#{r2:02X}{g2:02X}{b2:02X}` ({direction_label})"
    else:
        r, g, b = options.color
        color_desc = f"{swatch_emoji(options.color)} `#{r:02X}{g:02X}{b:02X}`"

    ec_pct = ERROR_CORRECTION_LEVELS[result.error_correction][1]
    if ec_auto and options.rainbow:
        auto_note = " (auto-selected for Rainbow)"
    elif ec_auto and options.logo_bytes is not None:
        auto_note = " (auto-selected for Logo)"
    else:
        auto_note = ""

    lines = [
        f"**Encoded:** ```{_safe_codeblock(options.text)}```",
        f"**Version:** {result.version}  •  **Grid:** {result.modules_count}×{result.modules_count} modules",
        (
            f"**Color:** {color_desc}  •  **Style:** {STYLE_LABELS[options.style]}  •  "
            f"**Background:** {'Transparent' if options.transparent else 'White'}"
        ),
        f"**Error Correction:** {EC_LABELS[result.error_correction]} (~{ec_pct}% recoverable){auto_note}",
        f"**Image Size:** {result.width}×{result.height}px",
    ]

    # Only shown when something's actually non-default, so a plain
    # /qrcode generate call's summary looks exactly like it did before
    # this feature existed.
    eyes_customized = (
        options.eye_frame_style != DEFAULT_EYE_FRAME_STYLE
        or options.eye_ball_style != DEFAULT_EYE_BALL_STYLE
        or options.eye_frame_color is not None
        or options.eye_ball_color is not None
    )
    if eyes_customized:
        frame_desc = EYE_FRAME_LABELS[options.eye_frame_style]
        ball_desc = EYE_BALL_LABELS[options.eye_ball_style]
        if options.eye_frame_color:
            r, g, b = options.eye_frame_color
            frame_desc += f" `#{r:02X}{g:02X}{b:02X}`"
        if options.eye_ball_color:
            r, g, b = options.eye_ball_color
            ball_desc += f" `#{r:02X}{g:02X}{b:02X}`"
        lines.append(f"**Eyes:** Frame={frame_desc}  •  Ball={ball_desc}")

    if result.logo_applied:
        plate_note = "" if options.logo_background else "  •  No background plate"
        lines.append(
            f"**Logo:** {LOGO_SHAPE_LABELS[options.logo_shape]}, "
            f"{result.effective_logo_size_percent}% size{plate_note}"
        )

    if result.warnings:
        lines.append("\n".join(f"-# ⚠️ {w}" for w in result.warnings))

    container = Container(
        TextDisplay(f"### 🔲 {title}"),
        TextDisplay("\n".join(lines)),
        accent_color=discord.Color.gold() if options.rainbow else discord.Color.blurple(),
    )

    layout = LayoutView(timeout=None)
    layout.add_item(container)
    layout.add_item(File(f"attachment://{filename}"))
    return layout


def _qr_content_type_field_value(info: QRContentInfo) -> str:
    """Renders a classify_qr_content() result as one embed field's value:
    the plain-language "what happens when this scans" summary, followed by
    whatever specific fields were parsed out of it (SSID, phone number,
    coordinates, etc.), if any."""
    lines = [info.summary]
    lines.extend(f"**{key}:** {value}" for key, value in info.details)
    return "\n".join(lines)


def _qr_decode_result_embed(image: discord.Attachment, result: QRDecodeResult) -> discord.Embed:
    """Result summary for /qrcode decode. A plain embed rather than the
    Components V2 layout /qrcode generate uses -- there's no new file
    being produced here, so the source image is linked back via its own
    CDN URL as a thumbnail instead of being re-uploaded.

    Each decoded payload gets two fields: the raw content, and a
    classify_qr_content()-driven guess at what a device would actually do
    with it (open a link, offer to join Wi-Fi, save a contact, etc.) --
    the same thing a phone's camera app would show instead of raw text."""
    count = len(result.contents)
    fields = []
    for i, content in enumerate(result.contents, start=1):
        suffix = "" if count == 1 else f" #{i}"
        info = classify_qr_content(content)
        fields.append((f"Decoded Content{suffix}", f"```{_safe_codeblock(content, limit=900)}```", False))
        fields.append((f"{info.label}{suffix}", _qr_content_type_field_value(info), False))

    unreadable = result.detected - count
    footer = (
        f"{unreadable} additional QR pattern{'s' if unreadable != 1 else ''} detected but "
        "couldn't be read (damaged, obstructed, or too small)."
    ) if unreadable > 0 else None

    return build_embed(
        title=f"🔍 {count} QR Code{'s' if count != 1 else ''} Decoded",
        color=discord.Color.green(),
        fields=fields,
        thumbnail=image.url,
        footer=footer,
    )



class QRCode(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /qrcode generate and /qrcode help live as subcommands of a single
    # /qrcode group (mirrors the /encode and /cipher group pattern in
    # utility.py/ciphers.py). The guild restriction moves to the group
    # itself -- per discord.py, a group's subcommands can't carry their
    # own @app_commands.guilds(...) default, so decorating the group here
    # makes every child inherit it.
    qrcode_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="qrcode",
            description="Generate custom QR codes, or decode one back to text from an image.",
        )
    )

    @qrcode_group.command(name="generate", description="Generates a QR code image from text or a URL.")
    @app_commands.describe(
        text=f"The text or URL to encode (up to {MAX_TEXT_LENGTH} characters).",
        title="Optional label shown above the result and used for the file name.",
        scale=f"Pixel size per QR module, {SCALE_MIN}-{SCALE_MAX}. Default {DEFAULT_SCALE}.",
        color="Foreground/gradient-start color -- hex code (#FF0000) or name (red). Start typing for presets. Ignored if rainbow is on.",
        transparent="Use a transparent background instead of white.",
        rainbow="Color each module with a rainbow gradient instead of a solid color (overrides color/gradient_color).",
        style="Shape of each QR module. The 3 corner finder patterns follow eye_frame_style/eye_ball_style instead.",
        error_correction="How much damage the code can sustain and still scan. Default Medium (auto-raised for Rainbow/Logo).",
        gradient_color="Second color -- setting this turns on a 2-color gradient with `color` (ignored if rainbow is on).",
        gradient_direction="Direction the gradient sweeps. Default Diagonal (top-left to bottom-right).",
        eye_frame_style="Shape of the 3 corner finder patterns' outer ring. Default Square -- see a picked style's (!) note.",
        eye_ball_style="Shape of the 3 corner finder patterns' inner center. Default Square.",
        eye_frame_color="Override color for eye frames (rings) only -- hex or name. Default: follows the body color/gradient.",
        eye_ball_color="Override color for eye balls (centers) only -- hex or name. Default: follows the body color/gradient.",
        logo="An image to place in the center of the code. Auto-raises error_correction to High if not set explicitly.",
        logo_shape="Shape to crop the logo (and its background plate) to. Default Circle.",
        logo_size=f"Logo size as % of the full image, {LOGO_SIZE_MIN}-{LOGO_SIZE_MAX}. Default {DEFAULT_LOGO_SIZE_PERCENT}. Reduced further on small codes to avoid the finder patterns.",
        logo_background="Draw a solid plate behind the logo so it doesn't sit directly against dark modules. Default on.",
        public="Post visibly in this channel instead of only to you. Default off.",
    )
    @app_commands.choices(
        style=[app_commands.Choice(name=label, value=value) for value, label in STYLE_LABELS.items()],
        error_correction=[
            app_commands.Choice(name=f"{EC_LABELS[key]} (~{pct}% recoverable)", value=key)
            for key, (_, pct) in ERROR_CORRECTION_LEVELS.items()
        ],
        gradient_direction=[
            app_commands.Choice(name=label, value=value) for value, label in GRADIENT_DIRECTION_LABELS.items()
        ],
        eye_frame_style=[app_commands.Choice(name=label, value=value) for value, label in EYE_FRAME_LABELS.items()],
        eye_ball_style=[app_commands.Choice(name=label, value=value) for value, label in EYE_BALL_LABELS.items()],
        logo_shape=[app_commands.Choice(name=label, value=value) for value, label in LOGO_SHAPE_LABELS.items()],
    )
    @app_commands.autocomplete(
        color=qr_color_autocomplete,
        gradient_color=qr_color_autocomplete,
        eye_frame_color=qr_color_autocomplete,
        eye_ball_color=qr_color_autocomplete,
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def qrcode_generate(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, MAX_TEXT_LENGTH],
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        scale: Optional[app_commands.Range[int, SCALE_MIN, SCALE_MAX]] = None,
        color: Optional[str] = None,
        transparent: bool = False,
        rainbow: bool = False,
        style: Optional[app_commands.Choice[str]] = None,
        error_correction: Optional[app_commands.Choice[str]] = None,
        gradient_color: Optional[str] = None,
        gradient_direction: Optional[app_commands.Choice[str]] = None,
        eye_frame_style: Optional[app_commands.Choice[str]] = None,
        eye_ball_style: Optional[app_commands.Choice[str]] = None,
        eye_frame_color: Optional[str] = None,
        eye_ball_color: Optional[str] = None,
        logo: Optional[discord.Attachment] = None,
        logo_shape: Optional[app_commands.Choice[str]] = None,
        logo_size: Optional[app_commands.Range[int, LOGO_SIZE_MIN, LOGO_SIZE_MAX]] = None,
        logo_background: bool = True,
        public: bool = False,
    ):
        await interaction.response.defer(ephemeral=not public)

        ec_auto = error_correction is None
        if error_correction:
            ec_value = error_correction.value
        elif logo is not None:
            ec_value = LOGO_DEFAULT_ERROR_CORRECTION
        elif rainbow:
            ec_value = RAINBOW_DEFAULT_ERROR_CORRECTION
        else:
            ec_value = DEFAULT_ERROR_CORRECTION

        style_value = style.value if style else DEFAULT_STYLE
        gradient_direction_value = gradient_direction.value if gradient_direction else DEFAULT_GRADIENT_DIRECTION
        eye_frame_style_value = eye_frame_style.value if eye_frame_style else DEFAULT_EYE_FRAME_STYLE
        eye_ball_style_value = eye_ball_style.value if eye_ball_style else DEFAULT_EYE_BALL_STYLE
        logo_shape_value = logo_shape.value if logo_shape else DEFAULT_LOGO_SHAPE

        try:
            # Always parsed (even under rainbow, where it'll just be
            # unused) so a typo'd color is still caught rather than
            # silently swallowed just because rainbow happened to make it
            # irrelevant. The other color options stay None when unset
            # (rather than defaulting to a placeholder), since None is
            # exactly what tells QROptions/generate_qr "not overridden".
            rgb = parse_color(color or "#000000")
            gradient_rgb = parse_color(gradient_color) if gradient_color is not None else None
            eye_frame_rgb = parse_color(eye_frame_color) if eye_frame_color is not None else None
            eye_ball_rgb = parse_color(eye_ball_color) if eye_ball_color is not None else None
        except ValueError as e:
            return await send_error(interaction, str(e))

        logo_bytes: Optional[bytes] = None
        if logo is not None:
            if logo.content_type and not logo.content_type.startswith("image/"):
                return await send_error(
                    interaction, f"`{logo.filename}` doesn't look like an image ({logo.content_type})."
                )
            if logo.size > MAX_IMAGE_ATTACHMENT_SIZE:
                return await send_error(
                    interaction,
                    f"`{logo.filename}` is too large to use as a logo ({logo.size:,} bytes -- the "
                    f"limit is {MAX_IMAGE_ATTACHMENT_SIZE:,} bytes).",
                )
            try:
                logo_bytes = await logo.read()
            except discord.HTTPException as e:
                return await send_error(interaction, f"Failed to download that logo attachment: {e}")

        options = QROptions(
            text=text,
            scale=scale if scale is not None else DEFAULT_SCALE,
            color=rgb,
            transparent=transparent,
            rainbow=rainbow,
            style=style_value,
            error_correction=ec_value,
            gradient_color=gradient_rgb,
            gradient_direction=gradient_direction_value,
            eye_frame_style=eye_frame_style_value,
            eye_ball_style=eye_ball_style_value,
            eye_frame_color=eye_frame_rgb,
            eye_ball_color=eye_ball_rgb,
            logo_bytes=logo_bytes,
            logo_shape=logo_shape_value,
            logo_size_percent=logo_size if logo_size is not None else DEFAULT_LOGO_SIZE_PERCENT,
            logo_background=logo_background,
        )

        try:
            result = generate_qr(options)
        except ValueError as e:
            return await send_error(interaction, str(e))

        # Only the command layer knows whether `color` was explicitly
        # typed vs. left at its default, so this note (unlike the rest of
        # result.warnings) is built here rather than inside generate_qr.
        # (gradient_color/eye colors/logo don't need the same treatment --
        # they're passed straight through as None when unset, so
        # generate_qr can and does already tell "unset" from "explicitly
        # chosen" for those on its own.)
        if rainbow and color is not None:
            result.warnings.insert(0, "`color` was ignored because `rainbow` is enabled.")

        display_title = (title or "").strip() or "QR Code"
        filename = f"{_sanitize_filename(title) if title else 'qrcode'}.png"

        file = discord.File(io.BytesIO(result.png_bytes), filename=filename)
        layout = _qr_result_layout(
            title=display_title, filename=filename, result=result, options=options, ec_auto=ec_auto,
        )
        await interaction.followup.send(view=layout, file=file, ephemeral=not public)

    @qrcode_group.command(name="decode", description="Reads the data encoded in a QR code image.")
    @app_commands.describe(
        image="An image containing a QR code (PNG, JPG, WEBP, etc.).",
        public="Post visibly in this channel instead of only to you. Default off.",
    )
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def qrcode_decode(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        public: bool = False,
    ):
        await interaction.response.defer(ephemeral=not public)

        if image.content_type and not image.content_type.startswith("image/"):
            return await send_error(
                interaction, f"`{image.filename}` doesn't look like an image ({image.content_type})."
            )
        if image.size > MAX_IMAGE_ATTACHMENT_SIZE:
            return await send_error(
                interaction,
                f"`{image.filename}` is too large to scan ({image.size:,} bytes -- the limit is "
                f"{MAX_IMAGE_ATTACHMENT_SIZE:,} bytes).",
            )

        try:
            image_bytes = await image.read()
        except discord.HTTPException as e:
            return await send_error(interaction, f"Failed to download that attachment: {e}")

        try:
            result = decode_qr(image_bytes)
        except ValueError as e:
            return await send_error(interaction, str(e))

        embed = _qr_decode_result_embed(image, result)
        await interaction.followup.send(embed=embed, ephemeral=not public)

    @qrcode_group.command(name="help", description="Quick reference for /qrcode generate's options.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def qrcode_help(self, interaction: discord.Interaction):
        preset_lines = "\n".join(f"{label} -- `{hex_code}`" for label, hex_code in PRESET_COLORS)
        ec_lines = "\n".join(
            f"**{key}** ({EC_LABELS[key]}) -- ~{pct}% recoverable"
            for key, (_, pct) in ERROR_CORRECTION_LEVELS.items()
        )
        embed = build_embed(
            title="🔲 /qrcode generate -- Reference",
            color=discord.Color.blurple(),
            fields=[
                (
                    "Text / URL",
                    f"Up to {MAX_TEXT_LENGTH} characters. Longer text needs a higher QR version "
                    "(a denser grid) to fit, and very long text combined with a strict error-"
                    "correction level may not fit at all.",
                    False,
                ),
                ("Scale", f"Pixel size per module, {SCALE_MIN}-{SCALE_MAX}. Higher = larger, crisper image.", False),
                ("Title", "Optional -- shown above the result and used for the downloaded file's name.", False),
                ("Color Presets", preset_lines, False),
                (
                    "Style",
                    "Square (classic), Rounded, or Dots -- for the data modules only. The 3 corner "
                    "finder patterns have their own `eye_frame_style`/`eye_ball_style` instead (see below).",
                    False,
                ),
                ("Error Correction", ec_lines, False),
                (
                    "Transparent / Rainbow",
                    "`transparent` swaps the white background for alpha (keep the file as PNG -- "
                    "converting to JPG turns transparent areas solid black). `rainbow` colors each "
                    "module along a red-to-violet diagonal gradient and overrides `color`/`gradient_color`.",
                    False,
                ),
                (
                    "Gradient",
                    "Set `gradient_color` to turn `color` into a 2-color gradient's start. "
                    "`gradient_direction` controls the sweep -- Top↔Bottom, Left↔Right, either "
                    "diagonal, or Radial (center outward). Ignored if `rainbow` is on.",
                    False,
                ),
                (
                    "Eyes (the 3 corner finder patterns)",
                    "`eye_frame_style` shapes the outer ring (Square/Rounded/Circle/Leaf), "
                    "`eye_ball_style` the inner center (Square/Rounded/Circle/Dot/Diamond/Leaf). "
                    "`eye_frame_color`/`eye_ball_color` optionally override just that part's color. "
                    "Circle/Leaf frames look great but are measurably less reliable against strict "
                    "scanners (including this bot's own `/qrcode decode`) -- Square and Rounded read "
                    "back perfectly in testing and are the safe picks.",
                    False,
                ),
                (
                    "Logo",
                    f"Attach an image with `logo` to place it in the center. `logo_shape` crops it "
                    f"(and its optional white backing plate, `logo_background`) to Square/Circle/"
                    f"Rounded. `logo_size` sets it as {LOGO_SIZE_MIN}-{LOGO_SIZE_MAX}% of the image "
                    "(auto-reduced further on small codes so it can't overlap the corner eyes). "
                    "Error correction is automatically raised to High unless you pick a level yourself.",
                    False,
                ),
            ],
            footer="Custom colors, gradients, rainbow codes, styled eyes, and logos can all reduce scan reliability -- always worth a test scan.",
        )
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(QRCode(bot))
