"""
/genpass -- cryptographically secure password & passphrase generator.

Adapted from a web-based password-generator spec into a Discord-native
command. A few deliberate translations from that spec, noted here once
instead of scattered across comments below:

- The web version's per-setting "help icon (question mark)" becomes each
  option's own @app_commands.describe() text -- Discord already renders
  that as inline help under every option in the command builder, so no
  extra UI is needed to replicate it.
- The "toggle to reveal a length text box" control collapses into a
  single optional `length` option: omitting it *is* "the toggle being
  off", and falls back to a random 8-26 length exactly like the web
  version's default (see api.passwords.random_length()).
- The web spec's separate "Generation" / "Check Length" / "Estimate Crack
  Time" dropdown modes are folded into one result: /genpass always shows
  the generated value(s) alongside length/word-count and a crack-time
  estimate, reusing the exact same entropy/crack-time math /entropy uses
  (api.entropy.analyze_entropy for random-mode passwords,
  api.passwords.passphrase_entropy_bits + api.entropy.crack_times_for_bits
  for passphrases, since a passphrase's real strength comes from word
  count, not character variety).
- An `amount` option adds bulk generation on top of the web spec (which
  only ever produced one value at a time): 1-10 values per call, all
  drawn from the exact same settings, shown as one numbered list. Random
  mode shares a single length draw across the whole batch (so every value
  in a batch is directly comparable) but each value is still independently
  random; the strength summary shown is the *weakest* value in the batch,
  since that's the one someone grabbing blindly from the list could
  actually end up with.
- The whole result -- value(s), strength breakdown, and the Regenerate
  button -- renders as one Components V2 Container instead of an
  embed+View pair, matching how this codebase's other interactive results
  are built (see commands.whitelist's WhitelistView/DeleteUserConfirmView).
  The web spec's "Copy to Clipboard" button has no bot-side equivalent
  worth keeping: every value already sits in its own ```code block```,
  and Discord already renders a built-in copy button on every code block,
  so a second, bot-drawn Copy button was pure redundancy.

Cryptographic randomness + no-log guarantee: every random choice happens
in api.passwords via `secrets` (a CSPRNG), and the result is never
logged, cached, or written anywhere -- it only exists in this ephemeral
message.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import ActionRow, Button, Container, LayoutView, TextDisplay

from api import config
from api.discord_helpers import has_role, is_in_guild, safe_respond, send_error, default_ui_error
from api.entropy import analyze_entropy, crack_times_for_bits, rating_for
from api.passwords import (
    generate_random_password, generate_passphrase, passphrase_entropy_bits,
    random_length, load_wordlist, DEFAULT_WORD_COUNT, DEFAULT_SEPARATOR,
)

GUILD = discord.Object(id=config.GUILD_ID)

MODE_RANDOM = "random"
MODE_PASSPHRASE = "passphrase"

MIN_AMOUNT = 1
MAX_AMOUNT = 10

# Mirrors commands.utility.ENTROPY_RATING_COLORS so /genpass and /entropy
# read as the same visual system -- duplicated locally rather than
# imported cross-cog, same as this codebase's other small per-file
# private helpers (e.g. utility.py's own _safe_codeblock).
RATING_COLORS = {
    "Very Weak": discord.Color.dark_red(),
    "Weak": discord.Color.red(),
    "Reasonable": discord.Color.orange(),
    "Strong": discord.Color.blue(),
    "Very Strong": discord.Color.teal(),
    "Excellent": discord.Color.purple(),  # doc's "Overkill/Military Grade" tier
}

# Components V2 Containers have no dedicated embed-style footer slot, so
# "-# " (Discord's small-subtext markdown) stands in for the old embed
# footer instead.
NO_LOG_FOOTER = (
    "Generated in-memory with a CSPRNG (secrets) -- never logged, cached, or sent anywhere. "
    "This message is the only copy."
)


def _entropy_bar(bits: float, cap: float = 128.0, width: int = 10) -> str:
    """Same bar renderer as commands.utility._entropy_bar, duplicated here
    so /genpass's result stays visually identical to /entropy's."""
    filled = max(0, min(width, round((bits / cap) * width)))
    return "▰" * filled + "▱" * (width - filled)


def _safe_codeblock(value: str, limit: int = 1000) -> str:
    value = value.replace("```", "``\u200b`")
    if len(value) > limit:
        value = value[:limit] + "… (truncated)"
    return value


@dataclass
class GenPassSettings:
    """Every option /genpass was called with, replayed unchanged by the
    Regenerate button -- except `length`, which stays None (fresh random
    8-26 length each regenerate) whenever it was omitted originally."""
    mode: str
    amount: int
    length: Optional[int]
    word_count: int
    uppercase: bool
    lowercase: bool
    numbers: bool
    symbols: bool
    extended_charset: bool
    exclude_ambiguous: bool
    easy_to_read: bool
    min_numbers: int
    min_symbols: int
    separator: str
    capitalize_words: bool


def _generate(settings: GenPassSettings) -> Tuple[List[str], Dict[str, Any]]:
    """Generates `settings.amount` value(s) from `settings` plus the stats
    needed to render them. Raises ValueError on bad option combinations
    (caught by callers and shown via send_error) -- every call in a batch
    shares the same settings, so a bad combination always fails on the
    first value rather than partway through the batch."""
    if settings.mode == MODE_PASSPHRASE:
        values = [
            generate_passphrase(
                word_count=settings.word_count,
                separator=settings.separator,
                capitalize_words=settings.capitalize_words,
            )
            for _ in range(settings.amount)
        ]
        bits = passphrase_entropy_bits(settings.word_count)
        stats: Dict[str, Any] = {
            "kind": MODE_PASSPHRASE,
            "rating": rating_for(bits),
            "bits": bits,
            "crack_times": crack_times_for_bits(bits),
            "word_count": settings.word_count,
            "wordlist_size": len(load_wordlist()),
        }
        return values, stats

    # Random-character mode. The length draw (fixed, or one random 8-26
    # roll) is shared across the whole batch so every value is directly
    # comparable -- only the characters themselves vary per value.
    length = settings.length if settings.length is not None else random_length()
    values = [
        generate_random_password(
            length=length,
            uppercase=settings.uppercase,
            lowercase=settings.lowercase,
            numbers=settings.numbers,
            symbols=settings.symbols,
            extended=settings.extended_charset,
            exclude_ambiguous=settings.exclude_ambiguous,
            easy_to_read=settings.easy_to_read,
            min_numbers=settings.min_numbers,
            min_symbols=settings.min_symbols,
        )
        for _ in range(settings.amount)
    ]

    # Every enabled character class is guaranteed to appear at least once
    # in every value (see generate_random_password's "mandatory" chars),
    # so pool_size/char_classes are stable across the batch -- only
    # effective_bits can drift slightly per value if the live weak-pattern
    # scan happens to catch something. Showing the weakest of the batch
    # keeps the summary honest for whichever value someone actually uses.
    results = [analyze_entropy(v) for v in values]
    worst = min(results, key=lambda r: r.effective_bits)
    stats = {
        "kind": MODE_RANDOM,
        "rating": worst.rating,
        "bits": worst.effective_bits,
        "crack_times": worst.crack_times,
        "pool_size": worst.pool_size,
        "char_classes": worst.char_classes,
        "length": length,
        "length_fixed": settings.length is not None,
    }
    return values, stats


class GenPassLayout(LayoutView):
    """Components V2 result layout for /genpass. The generated value(s),
    the strength breakdown, and the Regenerate button all live inside one
    Container -- same pattern as this codebase's other interactive
    results (see commands.whitelist.WhitelistView). Non-persistent -- like
    the codebase's other short-lived confirmation views (e.g.
    DeleteUserConfirmView), this only needs to survive as long as the
    ephemeral message itself is realistically still being acted on."""

    def __init__(self, settings: GenPassSettings):
        super().__init__(timeout=300)
        self.settings = settings
        self.values: List[str] = []
        self.stats: Dict[str, Any] = {}

    def _title(self) -> str:
        noun = "Passphrase" if self.settings.mode == MODE_PASSPHRASE else "Password"
        if self.settings.amount == 1:
            return f"🔑 Generated {noun}"
        return f"🔑 Generated {self.settings.amount} {noun}s"

    def _values_block(self) -> str:
        if len(self.values) == 1:
            return f"```{_safe_codeblock(self.values[0])}```"
        return "\n".join(
            f"**{i}.** ```{_safe_codeblock(value)}```"
            for i, value in enumerate(self.values, start=1)
        )

    def _stats_lines(self) -> List[str]:
        s = self.stats
        bulk = len(self.values) > 1
        bar = _entropy_bar(s["bits"])
        rating_label = "Weakest Rating" if bulk and s["kind"] == MODE_RANDOM else "Rating"
        lines = [f"**{rating_label}:** {s['rating']}   `{bar}`"]

        if s["kind"] == MODE_PASSPHRASE:
            lines.append(f"**Words:** {s['word_count']} per passphrase (from a {s['wordlist_size']:,}-word list)")
            lines.append(f"**Entropy:** {s['bits']:.1f} bits per passphrase")
        else:
            length_note = "random, 8-26" if not s["length_fixed"] else "fixed"
            pool_desc = ", ".join(s["char_classes"]) if s["char_classes"] else "none detected"
            lines.append(f"**Length:** {s['length']} characters ({length_note})")
            lines.append(f"**Character Pool:** {s['pool_size']} possible chars ({pool_desc})")
            entropy_note = " (weakest in the batch)" if bulk else ""
            lines.append(f"**Entropy:** {s['bits']:.1f} bits{entropy_note}")

        crack_lines = "\n".join(f"{label:<40}{duration}" for label, duration in s["crack_times"])
        lines.append(f"**Estimated Time To Crack:**\n```{crack_lines}```")
        return lines

    def build(self):
        """(Re)builds this view's components from current state. Call
        after any state change, then edit_message/followup.send(view=self)."""
        self.clear_items()

        color = RATING_COLORS.get(self.stats.get("rating"), discord.Color.blue())
        container = Container(
            TextDisplay(f"### {self._title()}"),
            TextDisplay(self._values_block()),
            TextDisplay("\n".join(self._stats_lines())),
            TextDisplay(f"-# {NO_LOG_FOOTER}"),
            accent_color=color,
        )

        regenerate_button = Button(label="Regenerate", emoji="🔄", style=discord.ButtonStyle.primary)
        regenerate_button.callback = self.on_regenerate
        container.add_item(ActionRow(regenerate_button))

        self.add_item(container)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="GenPassLayout")

    async def on_regenerate(self, interaction: discord.Interaction):
        try:
            values, stats = _generate(self.settings)
        except ValueError as e:
            return await send_error(interaction, str(e))
        self.values = values
        self.stats = stats
        self.build()
        await interaction.response.edit_message(view=self)


class GenPass(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="genpass", description="Generates a cryptographically secure password or passphrase.")
    @app_commands.guilds(GUILD)
    @app_commands.describe(
        mode="Random characters, or a memorable word-based passphrase (e.g. correct-horse-battery-staple).",
        amount=f"How many values to generate at once, {MIN_AMOUNT}-{MAX_AMOUNT}. Default 1.",
        length="Password length, 1-128 (random mode). Omit for a random length between 8-26 each time.",
        word_count="Number of words in the passphrase, 3-10 (passphrase mode). Default 5.",
        uppercase="Include uppercase letters A-Z (random mode). Default on.",
        lowercase="Include lowercase letters a-z (random mode). Default on.",
        numbers="Include digits 0-9 (random mode). Default on.",
        symbols="Include symbols like !@#$%^&* (random mode). Default on.",
        extended_charset="Add foreign-language letters/symbols (accented Latin, Greek, Cyrillic, currency) on top of the above (random mode).",
        exclude_ambiguous="Remove easily-confused characters like 0/O, l/1/I, and | (random mode).",
        easy_to_read="Simplify to unambiguous letters+digits only -- no symbols/extended -- for easier manual typing (random mode).",
        min_numbers="Force at least this many digits, 0-10 (random mode, requires numbers enabled).",
        min_symbols="Force at least this many symbols, 0-10 (random mode, requires symbols enabled).",
        separator="Character(s) joining passphrase words, e.g. '-' or '_' (passphrase mode). Default '-'.",
        capitalize_words="Capitalize the first letter of each passphrase word (passphrase mode).",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Random Characters", value=MODE_RANDOM),
        app_commands.Choice(name="Passphrase (word-based)", value=MODE_PASSPHRASE),
    ])
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def genpass_cmd(
        self,
        interaction: discord.Interaction,
        mode: Optional[app_commands.Choice[str]] = None,
        amount: Optional[app_commands.Range[int, MIN_AMOUNT, MAX_AMOUNT]] = None,
        length: Optional[app_commands.Range[int, 1, 128]] = None,
        word_count: Optional[app_commands.Range[int, 3, 10]] = None,
        uppercase: bool = True,
        lowercase: bool = True,
        numbers: bool = True,
        symbols: bool = True,
        extended_charset: bool = False,
        exclude_ambiguous: bool = False,
        easy_to_read: bool = False,
        min_numbers: Optional[app_commands.Range[int, 0, 10]] = None,
        min_symbols: Optional[app_commands.Range[int, 0, 10]] = None,
        separator: Optional[str] = None,
        capitalize_words: bool = False,
    ):
        settings = GenPassSettings(
            mode=mode.value if mode else MODE_RANDOM,
            amount=amount if amount is not None else 1,
            length=length,
            word_count=word_count if word_count is not None else DEFAULT_WORD_COUNT,
            uppercase=uppercase,
            lowercase=lowercase,
            numbers=numbers,
            symbols=symbols,
            extended_charset=extended_charset,
            exclude_ambiguous=exclude_ambiguous,
            easy_to_read=easy_to_read,
            min_numbers=min_numbers or 0,
            min_symbols=min_symbols or 0,
            separator=(separator.strip() or DEFAULT_SEPARATOR)[:10] if separator else DEFAULT_SEPARATOR,
            capitalize_words=capitalize_words,
        )

        try:
            values, stats = _generate(settings)
        except ValueError as e:
            return await send_error(interaction, str(e))

        layout = GenPassLayout(settings)
        layout.values = values
        layout.stats = stats
        layout.build()
        await safe_respond(interaction, view=layout, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GenPass(bot))