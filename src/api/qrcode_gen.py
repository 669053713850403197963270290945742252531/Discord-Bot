"""
QR code encoding/decoding + rendering for /qrcode. Ports the old standalone
tkinter QR Code Generator (Title / Text-URL / Scale / Color / Transparent /
Rainbow) into a reusable, Discord-independent module -- this file only
deals with "given these options, produce PNG bytes" (and, for decoding,
"given these image bytes, produce text") -- the same api/-vs-commands/
split as every other feature in this bot, and the same one-file-covers-
both-directions convention as api/encoding.py and api/ciphers.py.

The `qrcode` package is used purely for its encoding engine (Reed-Solomon
error correction, module placement, automatic version selection) -- its
own bundled image factories are never touched. Every pixel is instead
drawn here with Pillow, module by module, so foreground color can vary
per-module (solid color or a rainbow gradient) and each module's shape can
follow the chosen `style` -- something the stock qrcode->PIL renderer has
no support for.

Scan-reliability notes (why some things below are validated/warned rather
than just rendered):
  - QR scanners key off dark/light *contrast*, not hue, so a light custom
    color against a non-transparent (white) background can render a code
    that's technically correct but effectively invisible to a scanner --
    generate_qr() rejects that combination outright rather than silently
    producing a dead code.
  - Rainbow mode swaps every dark module's color independently, so a
    lighter hue (yellow, in particular) can read as "background" to some
    scanners. That can't be fully engineered around from here, so instead
    the caller defaults to a higher error-correction level for rainbow
    codes (more redundancy to absorb misread modules) and the result
    surfaces a warning so whoever generated it knows to test-scan it.
  - The three big 7x7 "finder pattern" eyes are drawn independently from
    the data modules (their own `eye_frame_style`/`eye_ball_style`,
    defaulting to plain squares) rather than always following `style`,
    since those are what a scanner's detector locks onto first --
    aggressively rounding or dotting them (especially both parts at once)
    is a common way a "fun" styled QR code ends up undetectable. Unlike
    data modules, each eye is rendered as one merged shape (outer ring,
    a punched-out "hole", then the inner ball) rather than tiny per-
    module tiles, which is both simpler and what every real styled-QR
    scanner is actually built to expect visually.
  - A center logo necessarily covers real data/error-correction modules,
    so generate_qr() leans on the same "validate/warn, don't silently
    produce a dead code" philosophy as rainbow: it nudges the caller
    toward High error correction when a logo is set (see
    LOGO_DEFAULT_ERROR_CORRECTION), and caps how large a logo can be
    (see _max_logo_percent()) so it can't be sized into the finder
    patterns themselves, shrinking it and surfacing a warning rather than
    producing something that looks fine but doesn't scan.
  - With `logo_background` on, the clear zone behind the logo is a solid
    square/circle/rounded plate (padded past the logo's own edges -- see
    LOGO_PLATE_PADDING_RATIO) filled white, so the logo reads clearly
    against busy modules. With it off, that geometric plate is skipped
    entirely -- only the modules actually sitting under an opaque logo
    pixel are cleared (see _logo_clear_mask()), so the surrounding dots
    are left drawn all the way up to the logo's real silhouette instead
    of stopping short at an invisible circle/square/rounded boundary
    around it. A non-rectangular logo (a plain icon on a transparent
    PNG) ends up looking like the dots wrap around its actual shape,
    rather than sitting outside a plain geometric hole.

Decode-side notes (why decode_qr() below tries several passes instead of
one detector call):
  - OpenCV's multi-code detector (used first, since it's the only one of
    the two that can read more than one code per image) and its
    single-code detector don't always agree -- empirically the multi-code
    path can fail on this module's own `style="dots"` renders that the
    single-code path still reads fine, so both are tried before giving up.
  - A transparent background (alpha channel) has to be composited onto
    white before handing the image to OpenCV, which has no concept of
    alpha -- naively dropping the channel instead turns every transparent
    pixel solid black, which is indistinguishable from the QR code's own
    dark modules and reliably fails to decode (the same failure mode
    generate_qr()'s transparent-background warning above is about).
  - A last-resort adaptive-threshold pass (grayscale + local binarization)
    is tried if the plain image doesn't decode, since real-world photos of
    a QR code (uneven lighting, low contrast, a slight tint) are a much
    less clean input than a code this module rendered itself.

Content-classification notes (classify_qr_content(), used to guess what a
phone's camera would *do* with a decoded payload -- open a link, offer to
join a Wi-Fi network, save a contact, etc. -- rather than just showing raw
text):
  - This is pattern-matching against well-known QR "smart" payload
    conventions (WIFI:, MECARD:/VCARD, mailto:/tel:/sms:, geo:, iCalendar
    VEVENT, otpauth://, and bare URLs), the same spirit as api.encoding's
    Identify heuristic -- a best-effort guess, not a guarantee of what any
    specific scanner app will actually do.
  - A Wi-Fi payload's password is wrapped in Discord spoiler tags in the
    result rather than shown plainly, since unlike the rest of the parsed
    fields it's a credential -- the raw decoded text (already shown
    unredacted elsewhere in the result) is where anyone who needs the
    literal value can still get it.
"""

import colorsys
import io
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np
import qrcode
import qrcode.constants
import qrcode.exceptions
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, UnidentifiedImageError

# =========================================================================
# Constants
# =========================================================================

# Mirrors the original tool's "Scale (6-50)" spinner exactly -- box_size in
# qrcode terms, i.e. pixels per module.
SCALE_MIN = 6
SCALE_MAX = 50
DEFAULT_SCALE = 6

# Standard QR "quiet zone" -- 4 modules of light space required on every
# side for reliable scanning. Not exposed as an option (the original tool
# didn't expose it either); always applied.
BORDER_MODULES = 4

# Hard ceiling on the final PNG's width/height in pixels. At SCALE_MAX (50)
# and the largest possible QR version (40, 177 modules + 8 border = 185),
# an unclamped render would be 9250x9250px -- ~340MB as a raw RGBA buffer,
# easily enough to stall or OOM a small host. When a requested scale would
# exceed this, the effective scale is quietly reduced instead of erroring
# outright (the resulting file is still perfectly scannable, just smaller).
MAX_IMAGE_DIMENSION_PX = 3000

# Soft cap on encodable text length. The `qrcode` library's own version-40
# capacity ceiling (which varies by error-correction level, roughly
# 1273-2953 bytes) is the real limit and is caught as a friendly error
# below -- this is just a sane upper bound for the Discord option itself.
MAX_TEXT_LENGTH = 2000

STYLES: Tuple[str, ...] = ("square", "rounded", "dots")
DEFAULT_STYLE = "square"

# Shapes for the outer 7x7 finder-pattern ring ("eye frame") and inner 3x3
# finder-pattern center ("eye ball"), independent of `style` above (which
# only governs the data modules). Both default to "square" so a code with
# no eye options set looks pixel-equivalent to the pre-existing renderer.
EYE_FRAME_STYLES: Tuple[str, ...] = ("square", "rounded", "circle", "leaf")
DEFAULT_EYE_FRAME_STYLE = "square"

EYE_BALL_STYLES: Tuple[str, ...] = ("square", "rounded", "circle", "dot", "diamond", "leaf")
DEFAULT_EYE_BALL_STYLE = "square"

# Two-color linear/radial gradient directions. The axis-aligned options
# are what "control top and left gradients" maps to (top<->bottom,
# left<->right); "diagonal" (top-left -> bottom-right) is the default
# since it's the most common look in QR-styling tools generally.
GRADIENT_DIRECTIONS: Tuple[str, ...] = (
    "top_to_bottom", "bottom_to_top", "left_to_right", "right_to_left",
    "diagonal", "diagonal_reverse", "radial",
)
DEFAULT_GRADIENT_DIRECTION = "diagonal"

# Center-logo shape (applied to both the logo image itself and its
# optional background plate, so they always match) and size, given as a
# percentage of the QR image's full side length (not just the data
# area). Below ~10% a logo isn't legible; above ~35% it risks eating into
# the finder patterns or overwhelming the error-correction budget even at
# High -- _max_logo_percent() below tightens this further for small
# (low-version) QR codes, where the finder patterns take up
# proportionally more of the image.
LOGO_SHAPES: Tuple[str, ...] = ("square", "circle", "rounded")
DEFAULT_LOGO_SHAPE = "circle"
LOGO_SIZE_MIN = 10
LOGO_SIZE_MAX = 35
DEFAULT_LOGO_SIZE_PERCENT = 20
# How much bigger the solid background plate is than the logo itself, so
# it shows as a thin ring of quiet space around the logo rather than an
# exact silhouette underneath it -- that gap is what keeps surrounding
# dark modules from touching the logo's edge pixels. Only used when
# `logo_background` is on (a filled square/circle/rounded plate behind
# the logo); when it's off, the clear zone instead follows the logo's
# own alpha channel -- see _logo_clear_mask() and _apply_logo().
LOGO_PLATE_PADDING_RATIO = 1.16

# Defensive resolution bounds for /qrcode decode -- mirrors the encode
# side's MAX_IMAGE_DIMENSION_PX, but here it's about keeping OpenCV's
# detector fast and accurate rather than capping an output file's size.
# An oversized upload (a full-resolution phone photo) is downscaled before
# detection; a tiny one is upscaled, since the detector needs a handful of
# pixels per module to resolve the finder patterns at all.
DECODE_MAX_DIMENSION_PX = 4000
DECODE_MIN_DIMENSION_PX = 400
DECODE_UPSCALE_TARGET_PX = 800

# name -> (qrcode constant, approximate % of the code that can be damaged/
# misread and still scan correctly).
ERROR_CORRECTION_LEVELS = {
    "L": (qrcode.constants.ERROR_CORRECT_L, 7),
    "M": (qrcode.constants.ERROR_CORRECT_M, 15),
    "Q": (qrcode.constants.ERROR_CORRECT_Q, 25),
    "H": (qrcode.constants.ERROR_CORRECT_H, 30),
}
DEFAULT_ERROR_CORRECTION = "M"
# Used instead of DEFAULT_ERROR_CORRECTION whenever rainbow mode is on and
# the caller didn't explicitly pick a level -- extra redundancy to help
# offset rainbow's lighter hues reading as "background" to some scanners.
RAINBOW_DEFAULT_ERROR_CORRECTION = "Q"
# Same idea, used instead of DEFAULT_ERROR_CORRECTION whenever a center
# logo is set and the caller didn't explicitly pick a level -- a logo
# blocks out a chunk of modules, so the highest redundancy tier is used
# to compensate.
LOGO_DEFAULT_ERROR_CORRECTION = "H"

# A handful of presets covering the old tool's default plus common picks --
# used for /qrcode generate's `color` autocomplete and /qrcode help's
# reference list. Anything else typed (any hex code or CSS color name) is
# still accepted; this list is suggestions, not a restriction.
PRESET_COLORS: List[Tuple[str, str]] = [
    ("Black (default)", "#000000"),
    ("White", "#FFFFFF"),
    ("Discord Blurple", "#5865F2"),
    ("Red", "#E63946"),
    ("Orange", "#F4A261"),
    ("Gold", "#D4AF37"),
    ("Yellow", "#FFD60A"),
    ("Green", "#2A9D8F"),
    ("Blue", "#457B9D"),
    ("Purple", "#9B5DE5"),
    ("Pink", "#FF6FB0"),
]

# Names that aren't valid CSS color names (so PIL's ImageColor wouldn't
# recognize them) but are common enough in a Discord context to special-
# case. Checked case-insensitively before falling back to hex/CSS parsing.
_COLOR_ALIASES = {
    "blurple": "#5865F2",
    "discord": "#5865F2",
    "discord blurple": "#5865F2",
}


# =========================================================================
# Color parsing
# =========================================================================

def parse_color(raw: str) -> Tuple[int, int, int]:
    """
    Parses a hex code (with or without '#', 3/6/8 digit) or a CSS color
    name into an (r, g, b) tuple. Raises ValueError with a message safe to
    show directly to the user on anything unrecognized.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Provide a color -- a hex code like `#FF0000`, or a name like `red`.")

    alias = _COLOR_ALIASES.get(candidate.lower())
    if alias:
        candidate = alias
    elif all(c in "0123456789abcdefABCDEF" for c in candidate) and len(candidate) in (3, 4, 6, 8):
        # Bare hex digits with no '#' -- fill it in rather than rejecting,
        # since that's the single most likely way someone types a color.
        candidate = f"#{candidate}"

    try:
        rgb = ImageColor.getrgb(candidate)
    except ValueError:
        raise ValueError(
            f"`{raw}` isn't a recognized color -- use a hex code like `#FF0000` or a common "
            "color name like `red`. Start typing in the `color` option to see some presets."
        )
    return tuple(rgb[:3])


def _relative_luminance(rgb: Tuple[int, int, int]) -> float:
    """Quick perceptual brightness (0-255), used only to flag colors that
    would be invisible or hard to scan against a light background -- not a
    real sRGB-linear luminance calculation, which would be overkill here."""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def swatch_emoji(rgb: Tuple[int, int, int]) -> str:
    """Best-effort colored-square emoji for `rgb`, purely decorative for
    the result summary (e.g. next to the hex code shown back to the
    user)."""
    r, g, b = rgb
    hi, lo = max(r, g, b), min(r, g, b)
    if hi < 40:
        return "⬛"
    if lo > 215:
        return "⬜"
    if hi - lo < 24:
        # Low saturation (gray/brown) -- bucket by lightness rather than
        # guessing at a hue that isn't really there.
        return "⬛" if (r + g + b) / 3 < 128 else "⬜"

    hue_deg = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360
    if hue_deg < 15 or hue_deg >= 345:
        return "🟥"
    if hue_deg < 45:
        return "🟧"
    if hue_deg < 70:
        return "🟨"
    if hue_deg < 170:
        return "🟩"
    if hue_deg < 255:
        return "🟦"
    if hue_deg < 320:
        return "🟪"
    return "🟥"


# =========================================================================
# Options / result
# =========================================================================

@dataclass
class QROptions:
    text: str
    scale: int = DEFAULT_SCALE
    color: Tuple[int, int, int] = (0, 0, 0)
    transparent: bool = False
    rainbow: bool = False
    style: str = DEFAULT_STYLE
    error_correction: str = DEFAULT_ERROR_CORRECTION

    # -- Gradient -----------------------------------------------------
    # Presence of gradient_color (rather than a separate bool) is what
    # enables gradient mode, mirroring how `color` already works: `color`
    # becomes the gradient's start, gradient_color its end. Ignored (with
    # a caller-surfaced warning) if `rainbow` is also on, same precedence
    # as `color`.
    gradient_color: Optional[Tuple[int, int, int]] = None
    gradient_direction: str = DEFAULT_GRADIENT_DIRECTION

    # -- Eyes (finder patterns) ----------------------------------------
    # Shapes default to "square" (pixel-equivalent to the old renderer).
    # Colors default to None, meaning "follow whatever the data modules
    # at that spot would be" (solid color, gradient, or rainbow) rather
    # than a separate fixed color.
    eye_frame_style: str = DEFAULT_EYE_FRAME_STYLE
    eye_ball_style: str = DEFAULT_EYE_BALL_STYLE
    eye_frame_color: Optional[Tuple[int, int, int]] = None
    eye_ball_color: Optional[Tuple[int, int, int]] = None

    # -- Center logo -----------------------------------------------------
    logo_bytes: Optional[bytes] = None
    logo_shape: str = DEFAULT_LOGO_SHAPE
    logo_size_percent: int = DEFAULT_LOGO_SIZE_PERCENT
    # The area behind the logo is always kept clear of data modules (see
    # LOGO_PLATE_PADDING_RATIO) -- this only controls *how* that clear
    # zone is filled: True draws an opaque white plate (a visible card
    # behind the logo, even on a transparent-background code); False
    # lets it fall through to the code's own background instead (so a
    # transparent-background code stays genuinely transparent there too),
    # which is what makes the surrounding modules read as if they'd
    # automatically fitted themselves around the logo rather than sitting
    # on a separate white shape.
    logo_background: bool = True


@dataclass
class QRResult:
    png_bytes: bytes
    version: int
    modules_count: int          # raw modules per side, excluding the quiet zone
    matrix_size: int            # modules per side, including the quiet zone
    width: int                  # final PNG width in px
    height: int                 # final PNG height in px
    requested_scale: int
    effective_scale: int
    error_correction: str
    logo_applied: bool = False
    effective_logo_size_percent: Optional[int] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class QRDecodeResult:
    contents: List[str]   # decoded payload(s), one per successfully-read code, in detection order
    detected: int          # how many QR-shaped patterns were located, whether or not each one decoded
    width: int             # dimensions of the image actually scanned (after any resize)
    height: int


# =========================================================================
# Rendering
# =========================================================================

def _in_finder_zone(row: int, col: int, modules_count: int, border: int) -> bool:
    """
    True if (row, col) -- coordinates into the *bordered* matrix -- falls
    inside one of the three 7x7 finder-pattern eyes (plus their 1-module
    separator ring). generate_qr()'s main per-module loop skips every
    cell this returns True for; those eyes are instead drawn once each,
    as merged shapes, by _draw_finder_pattern() -- see the module
    docstring for why eyes are handled independently of `style`.
    """
    r, c = row - border, col - border
    if r < 0 or c < 0 or r >= modules_count or c >= modules_count:
        return False  # quiet zone itself -- never a dark module anyway

    zone = 8  # 7x7 finder pattern + its 1-module separator
    if r < zone and c < zone:
        return True  # top-left
    if r < zone and c >= modules_count - zone:
        return True  # top-right
    if r >= modules_count - zone and c < zone:
        return True  # bottom-left
    return False


def _draw_module(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], style: str, fill: Tuple[int, int, int, int], scale: int):
    x0, y0, x1, y1 = box
    if style == "rounded":
        radius = max(1, scale // 3)
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    elif style == "dots":
        inset = max(1, round(scale * 0.08))
        draw.ellipse((x0 + inset, y0 + inset, x1 - inset, y1 - inset), fill=fill)
    else:
        draw.rectangle(box, fill=fill)


# -------------------------------------------------------------------------
# Gradient math
# -------------------------------------------------------------------------

def _lerp_rgb(start: Tuple[int, int, int], end: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    """Linearly interpolates between two (r, g, b) colors at t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    return tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))  # type: ignore[return-value]


def _gradient_t(row: int, col: int, matrix_size: int, direction: str) -> float:
    """
    Returns how far (row, col) sits along `direction`'s gradient axis, as
    a 0..1 fraction from the start color toward the end color. The four
    axis-aligned directions sweep straight across; "diagonal"/
    "diagonal_reverse" sweep corner-to-corner; "radial" sweeps outward
    from the exact center regardless of row/col, so it looks the same
    whichever corner you'd otherwise call "the start".
    """
    n = max(1, matrix_size - 1)
    if direction == "top_to_bottom":
        return row / n
    if direction == "bottom_to_top":
        return 1 - row / n
    if direction == "left_to_right":
        return col / n
    if direction == "right_to_left":
        return 1 - col / n
    if direction == "diagonal_reverse":
        return (row + (n - col)) / (2 * n)
    if direction == "radial":
        center = n / 2
        dist = math.hypot(row - center, col - center)
        max_dist = math.hypot(center, center)
        return dist / max_dist if max_dist else 0.0
    # "diagonal" (the default) and any unrecognized value fall back here.
    return (row + col) / (2 * n)


def _module_fill(row: int, col: int, matrix_size: int, options: "QROptions") -> Tuple[int, int, int, int]:
    """
    The RGBA fill for a single dark module at (row, col), per whichever
    coloring mode is active -- rainbow takes precedence over gradient,
    which takes precedence over the plain solid `color` (same precedence
    the command layer already documents to the caller via warnings when
    more than one is set at once).
    """
    if options.rainbow:
        max_hue_deg = 300.0
        denom = max(1, 2 * (matrix_size - 1))
        t = (row + col) / denom
        r, g, b = colorsys.hsv_to_rgb((t * max_hue_deg) / 360.0, 0.85, 0.95)
        return (int(r * 255), int(g * 255), int(b * 255), 255)

    if options.gradient_color is not None:
        t = _gradient_t(row, col, matrix_size, options.gradient_direction)
        r, g, b = _lerp_rgb(options.color, options.gradient_color, t)
        return (r, g, b, 255)

    return (*options.color, 255)


# -------------------------------------------------------------------------
# Finder-pattern (eye) drawing
# -------------------------------------------------------------------------
# Each eye is drawn as a merged shape rather than per-module tiles: an
# outer 7x7 ring (frame), then a same-shape 5x5 "hole" punched in the
# background color to create the ring, then an inner 3x3 ball. Reusing
# the frame-shape helper for both the ring and its hole guarantees they
# always match (a "leaf" frame gets a leaf-shaped hole, a "circle" frame
# gets a circular hole, etc.) without duplicating the shape logic.

def _draw_eye_frame(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], style: str, fill: Tuple[int, int, int, int]):
    x0, y0, x1, y1 = box
    size = x1 - x0
    if style == "circle":
        draw.ellipse(box, fill=fill)
    elif style == "leaf":
        radius = max(2, int(size * 0.45))
        draw.rounded_rectangle(box, radius=radius, fill=fill, corners=(True, False, True, False))
    elif style == "rounded":
        # Empirically tuned (not a stylistic guess): sweeping this radius
        # against this module's own decode_qr() showed a hard reliability
        # cliff between a 12%-of-size radius (100% read back across
        # scales 6-50 and several payloads) and a 15% radius (down to
        # ~20%) -- 10% keeps a visible safety margin on the reliable side
        # of that cliff while still looking clearly rounded.
        radius = max(1, int(size * 0.10))
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    else:  # square
        draw.rectangle(box, fill=fill)


def _draw_eye_ball(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], style: str, fill: Tuple[int, int, int, int]):
    x0, y0, x1, y1 = box
    size = x1 - x0
    if style == "dot":
        # Empirically tuned, same reasoning as _draw_eye_frame's "rounded"
        # radius: a 12%-of-size inset read back cleanly most of the time
        # but not always at low scale; dropping to 6% cleared every
        # scale/payload combination tried while still reading as visibly
        # smaller than the plain "circle" style (0% inset).
        inset = max(1, int(size * 0.06))
        draw.ellipse((x0 + inset, y0 + inset, x1 - inset, y1 - inset), fill=fill)
    elif style == "circle":
        draw.ellipse(box, fill=fill)
    elif style == "diamond":
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        draw.polygon([(cx, y0), (x1, cy), (cx, y1), (x0, cy)], fill=fill)
    elif style == "leaf":
        radius = max(2, int(size * 0.45))
        draw.rounded_rectangle(box, radius=radius, fill=fill, corners=(True, False, True, False))
    elif style == "rounded":
        radius = max(1, int(size * 0.25))
        draw.rounded_rectangle(box, radius=radius, fill=fill)
    else:  # square
        draw.rectangle(box, fill=fill)


def _draw_finder_pattern(
    draw: ImageDraw.ImageDraw,
    top_left_px: Tuple[int, int],
    scale: int,
    frame_style: str,
    ball_style: str,
    frame_fill: Tuple[int, int, int, int],
    ball_fill: Tuple[int, int, int, int],
    background_fill: Tuple[int, int, int, int],
):
    """Draws one complete eye (7x7 outer frame, punched-out 5x5 hole,
    inner 3x3 ball) with its top-left corner at `top_left_px`."""
    x0, y0 = top_left_px
    outer_box = (x0, y0, x0 + 7 * scale - 1, y0 + 7 * scale - 1)
    hole_box = (x0 + scale, y0 + scale, x0 + 6 * scale - 1, y0 + 6 * scale - 1)
    ball_box = (x0 + 2 * scale, y0 + 2 * scale, x0 + 5 * scale - 1, y0 + 5 * scale - 1)

    _draw_eye_frame(draw, outer_box, frame_style, frame_fill)
    _draw_eye_frame(draw, hole_box, frame_style, background_fill)
    _draw_eye_ball(draw, ball_box, ball_style, ball_fill)


# -------------------------------------------------------------------------
# Center logo
# -------------------------------------------------------------------------

def _max_logo_percent(modules_count: int) -> int:
    """
    Largest logo size (as a % of the full image side, matching how
    logo_size_percent is defined) that's guaranteed not to overlap any of
    the three 7x7 finder patterns plus a safety gap, for a QR of this
    version. Smaller QR versions (fewer modules) have proportionally
    larger finder patterns relative to the whole image, so they get a
    lower cap -- this is what keeps a requested logo size from being
    silently sized into a finder pattern on a short, low-version payload.
    """
    border = BORDER_MODULES
    matrix_size = modules_count + 2 * border
    finder_extent = 9  # 7-module finder + 1-module separator + 1-module safety gap
    safe_half = matrix_size / 2 - (border + finder_extent)
    if safe_half <= 0:
        return LOGO_SIZE_MIN
    percent = int((safe_half * 2 / matrix_size) * 100)
    return max(LOGO_SIZE_MIN, min(LOGO_SIZE_MAX, percent))


def _shape_mask(w: int, h: int, shape: str) -> Image.Image:
    """
    Returns a plain 'L' mode mask, `w` x `h`, 255 inside `shape`
    (square/circle/rounded) and 0 outside. Used two ways below: combined
    into an existing image's alpha by _apply_shape_mask() (for the logo
    itself), and used directly as a paste mask for the reserved clear
    zone behind it, where it needs to stay independent of whatever color
    is being pasted -- a mask derived from the *fill's* alpha would go
    fully 0 for a transparent fill, silently turning the "clear this
    area" paste into a no-op right when it matters most (a transparent-
    background code with no logo_background plate).
    """
    mask = Image.new("L", (w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    if shape == "circle":
        mask_draw.ellipse((0, 0, w - 1, h - 1), fill=255)
    elif shape == "rounded":
        mask_draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(min(w, h) * 0.22), fill=255)
    else:  # square
        mask_draw.rectangle((0, 0, w - 1, h - 1), fill=255)
    return mask


def _apply_shape_mask(image: Image.Image, shape: str) -> Image.Image:
    """
    Returns a copy of `image` (must already be RGBA) with its alpha
    punched down to `shape` (square/circle/rounded). Used for the logo
    itself, so its own transparency (if any) and the shape clip combine
    rather than one overwriting the other.
    """
    w, h = image.size
    mask = _shape_mask(w, h, shape)

    result = image.copy()
    result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
    return result


def _logo_clear_mask(logo_img: Image.Image, plate_size: int) -> Image.Image:
    """
    Returns an 'L' mode mask, `plate_size` x `plate_size`, centered the
    same way the logo itself is later centered on the QR image -- but
    unlike _shape_mask() (a filled square/circle/rounded plate), this one
    is opaque only where `logo_img` (the logo, already scaled and shape-
    masked to its final on-image form) is itself opaque. Used for the
    `logo_background=False` path in _apply_logo(): only the modules
    actually sitting under a visible logo pixel get cleared, so the QR's
    own dots are left drawn right up to the logo's real silhouette
    instead of stopping short at an invisible geometric boundary around
    it.

    Two adjustments keep this from being a literal, brittle per-pixel
    trace of the source image:
      - The alpha channel is thresholded rather than used as a soft
        multiply, so a logo's anti-aliased edge doesn't leave a faint
        ring of partially-cleared modules.
      - The thresholded silhouette is dilated by a few pixels, so there's
        still a thin quiet gap right at the logo's own edge (the same
        purpose LOGO_PLATE_PADDING_RATIO serves for the plate case) --
        just following the logo's actual shape instead of padding a
        geometric footprint that ignores it.
    """
    logo_size = logo_img.width
    alpha = logo_img.getchannel("A")
    silhouette = alpha.point(lambda a: 255 if a > 24 else 0)

    dilate_px = max(2, int(round(logo_size * 0.03)))
    dilated = silhouette
    # MaxFilter only accepts small odd kernel sizes, so grow the mask a
    # few pixels at a time rather than with one huge kernel.
    for _ in range(dilate_px):
        dilated = dilated.filter(ImageFilter.MaxFilter(3))

    mask = Image.new("L", (plate_size, plate_size), 0)
    offset = (plate_size - logo_size) // 2
    mask.paste(dilated, (offset, offset))
    return mask


def _apply_logo(
    img: Image.Image,
    options: "QROptions",
    modules_count: int,
    background: Tuple[int, int, int, int],
    warnings: List[str],
) -> Tuple[Image.Image, int]:
    """
    Composites the center logo from `options.logo_bytes` onto the
    already-fully-rendered `img`, in place, returning (img, effective
    logo_size_percent actually used -- after clamping). Raises ValueError
    (safe to show to the user) if the attachment isn't a readable image.

    Whatever data modules were drawn underneath the logo have to be
    cleared before it's pasted, or a logo with any transparency of its
    own (a plain icon on a transparent PNG) would leave the original dark
    modules showing through right against -- and sometimes past -- its
    visible edge. *What* gets cleared depends on `logo_background`:
      - On: a padded, shape-masked plate (see LOGO_PLATE_PADDING_RATIO)
        filled solid white -- a clean, uniform backdrop for logos that
        expect one (a square social-media icon, for example).
      - Off: only the modules actually sitting under an opaque logo pixel
        (see _logo_clear_mask()) -- everything else nearby is left
        completely untouched, so a non-rectangular logo reads as the
        surrounding dots wrapping around its real silhouette rather than
        sitting outside a plain geometric hole that was never asked for.
    """
    try:
        logo = Image.open(io.BytesIO(options.logo_bytes))
        logo.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("That logo file doesn't look like a valid image.")
    logo = logo.convert("RGBA")

    requested_percent = max(LOGO_SIZE_MIN, min(LOGO_SIZE_MAX, options.logo_size_percent))
    max_percent = _max_logo_percent(modules_count)
    effective_percent = min(requested_percent, max_percent)
    if effective_percent < requested_percent:
        warnings.append(
            f"Logo size reduced from {requested_percent}% to {effective_percent}% -- this QR "
            "code's version is small enough that a larger logo would have overlapped one of the "
            "3 corner finder patterns, which would break scanning entirely."
        )

    side = min(img.width, img.height)
    logo_size = max(1, int(side * effective_percent / 100))

    # Fit within a logo_size x logo_size square without squashing --
    # shrink to fit, then center on a transparent square canvas.
    logo.thumbnail((logo_size, logo_size), Image.LANCZOS)
    canvas = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
    offset = ((logo_size - logo.width) // 2, (logo_size - logo.height) // 2)
    canvas.paste(logo, offset, logo)
    logo = _apply_shape_mask(canvas, options.logo_shape)

    if options.logo_background:
        plate_size = max(logo_size, int(logo_size * LOGO_PLATE_PADDING_RATIO))
        plate = Image.new("RGBA", (plate_size, plate_size), (255, 255, 255, 255))
        plate_mask = _shape_mask(plate_size, plate_size, options.logo_shape)
        px = (img.width - plate_size) // 2
        py = (img.height - plate_size) // 2
        img.paste(plate, (px, py), plate_mask)
    else:
        # A few pixels bigger than the logo itself so the dilation in
        # _logo_clear_mask() has room to grow into -- not a visible plate,
        # just the canvas the silhouette mask is centered on.
        plate_size = int(logo_size * 1.1)
        clear_fill = Image.new("RGBA", (plate_size, plate_size), background)
        # `logo` (not `canvas`) so the clear zone follows what's actually
        # about to be drawn -- e.g. a fully-opaque square logo with
        # logo_shape="circle" still clears a circle, since the shape mask
        # has already clipped its alpha down to one; only a logo with
        # genuine transparency of its own (an icon-style PNG) produces a
        # silhouette that isn't just the plain shape outline.
        clear_mask = _logo_clear_mask(logo, plate_size)
        px = (img.width - plate_size) // 2
        py = (img.height - plate_size) // 2
        img.paste(clear_fill, (px, py), clear_mask)

    lx = (img.width - logo_size) // 2
    ly = (img.height - logo_size) // 2
    img.paste(logo, (lx, ly), logo)

    return img, effective_percent


def generate_qr(options: QROptions) -> QRResult:
    """
    Encodes `options.text` and renders it to a PNG per the rest of
    `options`. Raises ValueError (safe to show to the user) for empty
    text, data that doesn't fit even at maximum QR version, an unusable
    style/error-correction/eye-style/gradient-direction/logo-shape key, a
    solid color that would be invisible against a non-transparent
    background, or a logo attachment that isn't actually a readable
    image.
    """
    text = options.text.strip()
    if not text:
        raise ValueError("There's no text/URL to encode.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"Text is too long ({len(text)} characters) -- keep it under {MAX_TEXT_LENGTH}.")

    if options.style not in STYLES:
        raise ValueError(f"Unknown style `{options.style}`.")
    if options.error_correction not in ERROR_CORRECTION_LEVELS:
        raise ValueError(f"Unknown error correction level `{options.error_correction}`.")
    if options.eye_frame_style not in EYE_FRAME_STYLES:
        raise ValueError(f"Unknown eye frame style `{options.eye_frame_style}`.")
    if options.eye_ball_style not in EYE_BALL_STYLES:
        raise ValueError(f"Unknown eye ball style `{options.eye_ball_style}`.")
    if options.gradient_direction not in GRADIENT_DIRECTIONS:
        raise ValueError(f"Unknown gradient direction `{options.gradient_direction}`.")
    if options.logo_bytes is not None and options.logo_shape not in LOGO_SHAPES:
        raise ValueError(f"Unknown logo shape `{options.logo_shape}`.")

    warnings: List[str] = []

    if options.rainbow and options.gradient_color is not None:
        warnings.append("`gradient_color` was ignored because `rainbow` is enabled.")

    if not options.rainbow and not options.transparent:
        if _relative_luminance(options.color) > 235:
            raise ValueError(
                "That color is too close to white to be visible against the white background -- "
                "pick a darker color, or enable `transparent`."
            )

    scale = max(SCALE_MIN, min(SCALE_MAX, options.scale))

    ec_constant, _ = ERROR_CORRECTION_LEVELS[options.error_correction]
    qr = qrcode.QRCode(version=None, error_correction=ec_constant, box_size=1, border=BORDER_MODULES)
    qr.add_data(text)
    try:
        qr.make(fit=True)
    except (ValueError, qrcode.exceptions.DataOverflowError):
        # qrcode raises either of these (inconsistently, depending on how
        # the version bisection lands) once the data can't fit even at
        # version 40 -- both mean the same thing to a caller, so both are
        # normalized into one friendly message.
        raise ValueError(
            "That text is too long to fit in a QR code, even at the lowest error-correction level. "
            "Try shortening it, or lowering `error_correction`."
        )

    matrix = qr.get_matrix()  # includes the quiet zone border
    matrix_size = len(matrix)
    modules_count = qr.modules_count

    effective_scale = scale
    if matrix_size * scale > MAX_IMAGE_DIMENSION_PX:
        effective_scale = max(1, MAX_IMAGE_DIMENSION_PX // matrix_size)
        warnings.append(
            f"Scale automatically reduced from {scale} to {effective_scale} to keep the image "
            f"under {MAX_IMAGE_DIMENSION_PX}px -- this text needs a large QR version, so the "
            "full requested scale would have produced an unreasonably large file."
        )

    if options.transparent:
        # Discord, browsers, and any alpha-aware viewer composite this
        # correctly -- but naively converting the PNG to JPG (or opening
        # it in a tool that drops alpha instead of compositing it) turns
        # every transparent pixel solid black instead of the background
        # it was sitting on, which reads as a broken/unscannable code.
        # Confirmed empirically while testing this renderer.
        warnings.append(
            "Transparent background: this displays and scans correctly in Discord and other "
            "alpha-aware viewers, but converting the PNG to JPG (or opening it somewhere that "
            "drops transparency instead of compositing it) can turn the background solid black. "
            "Keep it as a PNG."
        )

    background = (0, 0, 0, 0) if options.transparent else (255, 255, 255, 255)
    img = Image.new("RGBA", (matrix_size * effective_scale, matrix_size * effective_scale), background)
    draw = ImageDraw.Draw(img)

    for row in range(matrix_size):
        for col in range(matrix_size):
            if not matrix[row][col]:
                continue
            # The 3 finder-pattern eyes are drawn separately, as merged
            # shapes, after this loop -- see _draw_finder_pattern().
            if _in_finder_zone(row, col, modules_count, BORDER_MODULES):
                continue

            fill = _module_fill(row, col, matrix_size, options)
            x0, y0 = col * effective_scale, row * effective_scale
            box = (x0, y0, x0 + effective_scale - 1, y0 + effective_scale - 1)
            _draw_module(draw, box, options.style, fill, effective_scale)

    # Each eye's own fill defaults to whatever the data modules at its
    # center would be (so a gradient or rainbow still flows through it
    # visually), unless overridden with an explicit eye_frame_color /
    # eye_ball_color -- those always render as a flat solid color instead.
    eye_origins = {
        "top-left": (BORDER_MODULES, BORDER_MODULES),
        "top-right": (BORDER_MODULES, BORDER_MODULES + modules_count - 7),
        "bottom-left": (BORDER_MODULES + modules_count - 7, BORDER_MODULES),
    }
    for row0, col0 in eye_origins.values():
        base_fill = _module_fill(row0 + 3, col0 + 3, matrix_size, options)
        frame_fill = (*options.eye_frame_color, 255) if options.eye_frame_color else base_fill
        ball_fill = (*options.eye_ball_color, 255) if options.eye_ball_color else base_fill
        top_left_px = (col0 * effective_scale, row0 * effective_scale)
        _draw_finder_pattern(
            draw, top_left_px, effective_scale,
            options.eye_frame_style, options.eye_ball_style,
            frame_fill, ball_fill, background,
        )

    if options.rainbow:
        warnings.append(
            "Rainbow codes vary color module-by-module, which can be harder for some scanners to "
            "read (lighter hues like yellow have the least contrast). A higher error-correction "
            "level was used to help compensate -- test-scan this before relying on it."
        )
    elif not options.transparent and _relative_luminance(options.color) > 170:
        warnings.append(
            "That color is fairly light, which can reduce scan reliability against the white "
            "background -- test-scan this before relying on it."
        )
    if not options.rainbow and options.gradient_color is not None and not options.transparent:
        if _relative_luminance(options.gradient_color) > 235:
            warnings.append(
                "The gradient's second color is very close to white, so part of the code will have "
                "little contrast against the white background -- test-scan this before relying on it."
            )
    for label, eye_color in (("eye frame", options.eye_frame_color), ("eye ball", options.eye_ball_color)):
        if eye_color is not None and not options.transparent and _relative_luminance(eye_color) > 200:
            warnings.append(
                f"The {label} color is fairly light, which can reduce scan reliability -- "
                "test-scan this before relying on it."
            )
    if options.eye_frame_style in ("circle", "leaf"):
        # Precise, not hedged: measured directly against this module's
        # own decode_qr() across several scales and payloads before
        # shipping this feature (see _draw_eye_frame's docstring note on
        # the same measurement for "rounded"'s radius). "circle" failed
        # every single time; "leaf" was roughly a coin flip -- both are
        # a real, not theoretical, reliability difference from "square"/
        # "rounded", which read back cleanly 100% of the time in the same
        # test.
        detail = (
            "failed to read back 100% of the time" if options.eye_frame_style == "circle"
            else "was inconsistent (roughly a coin flip)"
        )
        warnings.append(
            f"`{options.eye_frame_style}` eye frames replace the finder patterns' square outline "
            f"with a rounded/circular one -- in testing against this bot's own `/qrcode decode`, "
            f"this style {detail} across several sizes and payloads. Phone camera scanners are "
            "generally more forgiving than that classical detector, but always test-scan with a "
            "real device before relying on this for anything important -- `square` and `rounded` "
            "eye frames read back reliably 100% of the time in the same test and are the safer "
            "choice if in doubt."
        )

    logo_applied = False
    effective_logo_size_percent: Optional[int] = None
    if options.logo_bytes is not None:
        img, effective_logo_size_percent = _apply_logo(img, options, modules_count, background, warnings)
        logo_applied = True
        warnings.append(
            "A center logo covers part of the QR code's data/error-correction modules -- always "
            "test-scan this with a real device before relying on it."
        )

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)

    return QRResult(
        png_bytes=buffer.getvalue(),
        version=qr.version,
        modules_count=modules_count,
        matrix_size=matrix_size,
        width=img.width,
        height=img.height,
        requested_scale=scale,
        effective_scale=effective_scale,
        error_correction=options.error_correction,
        logo_applied=logo_applied,
        effective_logo_size_percent=effective_logo_size_percent,
        warnings=warnings,
    )


# =========================================================================
# Decoding
# =========================================================================

def _load_scannable_image(image_bytes: bytes) -> np.ndarray:
    """
    Opens `image_bytes` with Pillow and prepares it for cv2.QRCodeDetector:
    flattens any alpha channel onto white (see the module docstring for
    why a naive alpha drop instead turns transparent pixels solid black),
    then rescales into a sane resolution range. Returns a BGR numpy array.
    Raises ValueError -- safe to show to the user -- if Pillow can't open
    it as an image at all.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("That doesn't look like a valid image file.")

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, img)
    img = img.convert("RGB")

    longest = max(img.size)
    if longest > DECODE_MAX_DIMENSION_PX:
        ratio = DECODE_MAX_DIMENSION_PX / longest
        img = img.resize(
            (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))), Image.LANCZOS
        )
    elif longest < DECODE_MIN_DIMENSION_PX:
        ratio = DECODE_UPSCALE_TARGET_PX / longest
        img = img.resize(
            (max(1, round(img.width * ratio)), max(1, round(img.height * ratio))), Image.LANCZOS
        )

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def decode_qr(image_bytes: bytes) -> QRDecodeResult:
    """
    Scans `image_bytes` for QR codes and returns whatever it can read.

    Tries cv2's multi-code detector first (the only one of the two that
    can read more than one code in a single image), falls back to its
    single-code detector (which empirically still reads some codes -- e.g.
    this module's own `style="dots"` renders -- that the multi-code path
    misses), and if neither finds anything, retries both against an
    adaptive-threshold pass for low-contrast or unevenly-lit photos. Raises
    ValueError (safe to show to the user) if nothing decodes.
    """
    frame = _load_scannable_image(image_bytes)
    height, width = frame.shape[:2]
    detector = cv2.QRCodeDetector()

    contents: List[str] = []
    detected = 0

    def _attempt(mat: np.ndarray) -> None:
        nonlocal detected
        try:
            ok, decoded_info, points, _ = detector.detectAndDecodeMulti(mat)
        except cv2.error:
            ok, decoded_info, points = False, (), None
        if ok:
            detected = max(detected, len(points) if points is not None else len(decoded_info))
            for text in decoded_info:
                if text and text not in contents:
                    contents.append(text)

        if not contents:
            # The multi-code path can come back empty-handed on an image
            # its single-code counterpart still reads fine -- see the
            # module docstring's decode-side notes.
            try:
                text, points, _ = detector.detectAndDecode(mat)
            except cv2.error:
                text = ""
            if text:
                detected = max(detected, 1)
                if text not in contents:
                    contents.append(text)

    _attempt(frame)

    if not contents:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        threshold = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 5
        )
        _attempt(cv2.cvtColor(threshold, cv2.COLOR_GRAY2BGR))

    if not contents:
        raise ValueError(
            "No QR code could be found in that image -- make sure it's clearly visible, "
            "in-frame, and not too small or blurry."
        )

    return QRDecodeResult(contents=contents, detected=max(detected, len(contents)), width=width, height=height)


# =========================================================================
# Content classification
# =========================================================================
# Best-effort guess at what a phone's camera or a dedicated QR scanner
# would *do* with a decoded payload, rather than just treating it as
# opaque text. See the module docstring's "Content-classification notes"
# for the reasoning.

@dataclass
class QRContentInfo:
    kind: str                                    # short internal key, e.g. "url", "wifi", "vcard"
    label: str                                   # human-facing heading, e.g. "🔗 Website / URL"
    summary: str                                  # one-line "what a device would do when scanning this"
    details: List[Tuple[str, str]] = field(default_factory=list)  # parsed key -> value pairs, if any


_URI_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

# Loose "does this look like a website address" check for payloads with no
# URI scheme at all (e.g. `example.com` or `www.example.com/path`) -- the
# same case phone cameras special-case even without an explicit `http://`.
# Requires a letters-only final label so IPs/decimals/version numbers
# (`192.168.1.1`, `3.14`, `v1.2.3`) don't false-positive as domains.
_BARE_DOMAIN_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9-]{1,63})*\.[a-zA-Z]{2,63}(?:/\S*)?$"
)


def _unescape_delimited(value: str) -> str:
    """Undoes backslash-escaping of `\\`, `;`, `,`, `:` -- used by the
    WIFI:/MECARD: payload conventions below."""
    return re.sub(r"\\([\\;,:])", r"\1", value)


def _parse_delimited_fields(body: str) -> dict:
    """
    Parses a `KEY:value;KEY2:value2;;`-style body (the WIFI:/MECARD:
    conventions) into a dict keyed by uppercased field letter, splitting
    only on *unescaped* `;` so an escaped `\\;` inside a value (e.g. a
    password containing a semicolon) isn't mistaken for a field separator.
    """
    fields = {}
    for part in re.split(r"(?<!\\);", body):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, _, value = part.partition(":")
        fields[key.strip().upper()] = _unescape_delimited(value.strip())
    return fields


def classify_qr_content(text: str) -> QRContentInfo:
    """
    Best-effort classification of a decoded QR payload -- a guess at what
    a phone's camera or a dedicated scanner app would offer to *do* with
    it (open a link, join a Wi-Fi network, save a contact, dial a number,
    add a calendar event, add a 2FA account, drop a map pin) rather than
    just displaying raw text. Never raises -- anything unrecognized falls
    through to a plain-text classification.
    """
    stripped = text.strip()
    prefix = stripped[:16].upper()

    if prefix.startswith("WIFI:"):
        fields = _parse_delimited_fields(stripped[5:])
        ssid = fields.get("S", "")
        auth_map = {"WPA": "WPA/WPA2/WPA3", "WEP": "WEP", "NOPASS": "Open (no password)"}
        auth = auth_map.get(fields.get("T", "").upper(), fields.get("T") or "Unknown")
        details = [("Network Name (SSID)", ssid or "*(none found)*"), ("Security", auth)]
        if fields.get("P"):
            details.append(("Password", f"||{fields['P']}||"))
        if fields.get("H", "").lower() == "true":
            details.append(("Hidden Network", "Yes"))
        return QRContentInfo(
            kind="wifi", label="📶 Wi-Fi Network",
            summary="Most phones prompt to join this Wi-Fi network automatically when scanned.",
            details=details,
        )

    if prefix.startswith("BEGIN:VCARD"):
        def _field(name: str) -> Optional[str]:
            m = re.search(rf"{name}[^:\n]*:(.+)", stripped, re.IGNORECASE)
            return m.group(1).strip() if m else None

        details = [
            (label, value) for label, value in (
                ("Name", _field("FN")),
                ("Organization", _field("ORG")),
                ("Phone", _field("TEL")),
                ("Email", _field("EMAIL")),
            ) if value
        ]
        return QRContentInfo(
            kind="vcard", label="👤 Contact Card (vCard)",
            summary="Most phones offer to save this as a new contact when scanned.",
            details=details,
        )

    if prefix.startswith("MECARD:"):
        fields = _parse_delimited_fields(stripped[7:])
        details = [
            (label, fields[key]) for label, key in (
                ("Name", "N"), ("Phone", "TEL"), ("Email", "EMAIL"), ("Address", "ADR"),
            ) if fields.get(key)
        ]
        return QRContentInfo(
            kind="mecard", label="👤 Contact Card (MECARD)",
            summary="Most phones offer to save this as a new contact when scanned.",
            details=details,
        )

    if prefix.startswith("BEGIN:VEVENT") or prefix.startswith("BEGIN:VCALENDAR"):
        def _field(name: str) -> Optional[str]:
            m = re.search(rf"{name}[^:\n]*:(.+)", stripped, re.IGNORECASE)
            return m.group(1).strip() if m else None

        details = [
            (label, value) for label, value in (
                ("Event", _field("SUMMARY")), ("Starts", _field("DTSTART")), ("Location", _field("LOCATION")),
            ) if value
        ]
        return QRContentInfo(
            kind="calendar", label="📅 Calendar Event",
            summary="Most phones offer to add this as a calendar event when scanned.",
            details=details,
        )

    geo_match = re.match(r"^geo:([\-0-9.]+),([\-0-9.]+)", stripped, re.IGNORECASE)
    if geo_match:
        lat, lon = geo_match.group(1), geo_match.group(2)
        return QRContentInfo(
            kind="geo", label="📍 Geographic Location",
            summary="Most phones offer to open this location in a maps app when scanned.",
            details=[("Coordinates", f"{lat}, {lon}"), ("Maps Link", f"https://maps.google.com/?q={lat},{lon}")],
        )

    if re.match(r"^mailto:", stripped, re.IGNORECASE):
        address = stripped[7:].split("?")[0].strip()
        return QRContentInfo(
            kind="email", label="✉️ Email Address",
            summary="Most phones open the default mail app, pre-addressed to send.",
            details=[("To", address or "*(none found)*")],
        )

    if re.match(r"^tel:", stripped, re.IGNORECASE):
        return QRContentInfo(
            kind="phone", label="📞 Phone Number",
            summary="Most phones prompt to dial this number when scanned.",
            details=[("Number", stripped[4:].strip() or "*(none found)*")],
        )

    sms_match = re.match(r"^sms(?:to)?:([^:?]*)", stripped, re.IGNORECASE)
    if sms_match:
        return QRContentInfo(
            kind="sms", label="💬 Text Message",
            summary="Most phones open the messaging app, pre-addressed to this number.",
            details=[("Number", sms_match.group(1).strip() or "*(none found)*")],
        )

    if re.match(r"^otpauth://", stripped, re.IGNORECASE):
        parsed = urlparse(stripped)
        account_label = unquote(parsed.path.lstrip("/")) or None
        issuer = parse_qs(parsed.query).get("issuer", [None])[0]
        details = [(label, value) for label, value in (("Issuer", issuer), ("Account", account_label)) if value]
        return QRContentInfo(
            kind="otp", label="🔐 Authenticator (2FA) Setup",
            summary=(
                "Authenticator apps (Google Authenticator, Authy, etc.) treat this as a new 2FA "
                "account to add -- it embeds a secret key, so handle it like a password."
            ),
            details=details,
        )

    scheme_match = _URI_SCHEME_RE.match(stripped)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        if scheme in ("http", "https"):
            return QRContentInfo(
                kind="url", label="🔗 Website / URL",
                summary="Most phones open this directly in a web browser when scanned.",
                details=[("Domain", urlparse(stripped).netloc or "*(unknown)*")],
            )
        # Some other registered URI scheme (custom app deep links, ftp,
        # market://, intent:, etc.) -- still a "link", just not one a
        # browser itself will necessarily handle.
        return QRContentInfo(
            kind="uri", label=f"🔗 Link ({scheme}://)",
            summary="Most phones hand this to whichever app is registered for that link type, which may not be a browser.",
            details=[],
        )

    if " " not in stripped and _BARE_DOMAIN_RE.match(stripped):
        return QRContentInfo(
            kind="url", label="🔗 Website / URL",
            summary="Looks like a web address without `http(s)://` -- most phones still detect and open this as a link.",
            details=[("Address", stripped)],
        )

    return QRContentInfo(
        kind="text", label="📝 Plain Text",
        summary="No special action detected -- this will most likely just display as plain text when scanned.",
        details=[],
    )