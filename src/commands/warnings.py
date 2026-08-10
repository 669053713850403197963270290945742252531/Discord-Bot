"""
/warnings -- durable warning records for members.

Persisted to BotState.json's "warnings" list, the same "fetch -> mutate ->
commit" shape moderation.py's temp_bans and access.py's temp_bot_access
use (see api.github.update_botstate). Unlike those two, a warning carries
no timer/expiry -- it's just an append/read/delete record -- so there's no
reconcile_*() timer-rescheduling step here. reconcile_warnings_cache()
below exists purely to warm the in-memory autocomplete cache back up after
a restart; see that function and the cache section right below for why the
cache exists at all.

Each entry looks like:
    {
        "id": "warn_9f2a1c",          # short random id, see api.github.new_state_id
        "guild_id": "123456789",
        "discord_id": "987654321",     # the warned member
        "reason": "...",
        "moderator_id": "555555555",
        "moderator_tag": "Corrade",
        "created_at": "2026-08-09T12:00:00Z",
    }
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button, Select

from api import config
from api.discord_helpers import (
    has_role, is_in_guild, can_moderate, notify_user,
    build_embed, safe_respond, send_success, send_error, default_ui_error,
)
from api.alerts import (
    send_moderation_alert, alert_embed,
    ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT,
)
from api.github import GitHubAPIError, fetch_botstate_with_sha, update_botstate, new_state_id
from api.time_utils import format_iso, parse_iso

GUILD = discord.Object(id=config.GUILD_ID)

# =========================================================================
# In-memory warnings cache -- mirrors BotState.json's "warnings" list purely
# so /warnings delete's autocomplete (which, like every Discord autocomplete
# callback, has to answer well inside a ~3s window) has something fast to
# read instead of a live GitHub API call -- same reasoning as Users.json's
# get_cached_users()/set_users_cache() in api/github.py. update_botstate()
# already hands back the committed state on every successful write, so
# every add/clear/delete below refreshes this cache straight from that
# return value -- no separate re-fetch needed, and no risk of it drifting
# out of sync with what actually landed on GitHub.
# =========================================================================

_warnings_cache: List[Dict[str, Any]] = []


def _set_warnings_cache(warnings: List[Dict[str, Any]]) -> None:
    global _warnings_cache
    _warnings_cache = warnings


def _user_warnings(discord_id) -> List[Dict[str, Any]]:
    """Cached warnings for one user, newest first."""
    matches = [w for w in _warnings_cache if str(w.get("discord_id")) == str(discord_id)]
    matches.sort(key=lambda w: w.get("created_at") or "", reverse=True)
    return matches


async def reconcile_warnings_cache(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: warms the in-memory autocomplete cache
    from BotState.json's "warnings" list. No timer to reschedule here
    (unlike reconcile_temp_bans() etc.) -- this exists purely so
    /warnings delete's autocomplete has real suggestions immediately after
    a restart instead of sitting empty until the first /warnings
    add|inspect|clear happens to populate it.

    `state` lets a caller that's already fetched BotState.json (e.g.
    start.py's on_ready, reconciling several categories back to back) hand
    it over directly instead of this making its own redundant fetch of the
    exact same file. Falls back to fetching it itself when called on its
    own with nothing passed in."""
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for warnings cache reconciliation: {e}")
            return
    _set_warnings_cache(state.get("warnings", []))


# =========================================================================
# /warnings config -- auto-action preferences for the warning system.
#
# Controls whether reaching a set warning count *also* does something to
# the member (timeout/kick/ban) beyond just recording the warning, same
# "in-memory for fast access + mirrored to BotState.json's 'warning_config'
# key on every change" pattern as autorole.py's _autorole_enabled/
# _autorole_role_id or discord_helpers.py's _dms_enabled -- see either of
# those for the full reasoning. reconcile_warning_config() below warms it
# back up from BotState.json on restart, same as every other reconcile_*().
# =========================================================================

WARNING_ACTION_NONE = "none"
WARNING_ACTION_TIMEOUT = "timeout"
WARNING_ACTION_KICK = "kick"
WARNING_ACTION_BAN = "ban"

# Metadata for each selectable action -- emoji/label drive the /warnings
# config dropdown and summary text, verb reads naturally in "couldn't be
# {verb}" failure messages, and notify_key maps to notify_user()'s titles
# dict in discord_helpers.py (None for "none", since nothing happened to DM
# a member about).
WARNING_ACTIONS: Dict[str, Dict[str, Optional[str]]] = {
    WARNING_ACTION_NONE: {
        "emoji": "📝", "label": "Do nothing (log only)",
        "verb": "acted on", "notify_key": None,
    },
    WARNING_ACTION_TIMEOUT: {
        "emoji": "🔇", "label": "Timeout the member",
        "verb": "timed out", "notify_key": "timed_out",
    },
    WARNING_ACTION_KICK: {
        "emoji": "👢", "label": "Kick the member",
        "verb": "kicked", "notify_key": "kicked",
    },
    WARNING_ACTION_BAN: {
        "emoji": "🔨", "label": "Ban the member",
        "verb": "banned", "notify_key": "banned",
    },
}

# Discord's native member timeout tops out at 28 days. This is deliberately
# a curated dropdown of sane presets rather than a free-typed number -- far
# harder to fat-finger a wrong unit/value than a raw numeric input, and
# plenty for how this feature actually gets used in practice.
WARNING_TIMEOUT_CHOICES: List[tuple] = [
    (5, "5 minutes"), (10, "10 minutes"), (30, "30 minutes"),
    (60, "1 hour"), (360, "6 hours"), (720, "12 hours"),
    (1440, "1 day"), (4320, "3 days"), (10080, "7 days"), (40320, "28 days"),
]
_WARNING_TIMEOUT_MINUTES = {minutes for minutes, _label in WARNING_TIMEOUT_CHOICES}

# /warnings config's threshold dropdown only ever offers 1-10 -- see
# WarningConfigView.build() -- so this doubles as both the UI's range and
# reconcile_warning_config()'s validation clamp.
WARNING_THRESHOLD_MIN = 1
WARNING_THRESHOLD_MAX = 10

DEFAULT_WARNING_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "threshold": 3,
    "action": WARNING_ACTION_TIMEOUT,
    "timeout_minutes": 60,
    "reset_after_action": True,
    "notify_target": True,
}

_warning_config: Dict[str, Any] = dict(DEFAULT_WARNING_CONFIG)


def warning_config() -> Dict[str, Any]:
    """Current /warnings config, for _warn_add_impl below (or anything else
    that wants to read it, e.g. a future /botstatus line) without importing
    the private dict directly. Returns a copy so callers can't accidentally
    mutate the live config out from under /warnings config's own Save/
    Cancel bookkeeping."""
    return dict(_warning_config)


async def _persist_warning_config(message: str):
    """Mirrors the in-memory config to BotState.json. Best-effort -- logged
    rather than raised, since /warnings config's Save button has already
    updated the in-memory copy (and therefore already taken effect) by the
    time this runs; a failure here only means it would fall back to the
    last-persisted config on the next restart instead of resuming where it
    left off."""
    def _mutate(state):
        state["warning_config"] = dict(_warning_config)
        return state
    try:
        await update_botstate(_mutate, message)
    except GitHubAPIError as e:
        print(f"Failed to persist warning config to BotState.json: {e}")


async def reconcile_warning_config(bot: commands.Bot, state: Optional[Dict[str, Any]] = None):
    """Called once from on_ready: restores the auto-action config from
    BotState.json, so a restart resumes whatever staff last saved via
    /warnings config instead of silently reverting to the (disabled-by-
    default) defaults.

    `state` lets a caller that's already fetched BotState.json hand it over
    directly instead of this making its own redundant fetch -- see
    commands.moderation.reconcile_temp_bans() for the full reasoning."""
    global _warning_config
    if state is None:
        try:
            state, _sha = await fetch_botstate_with_sha()
        except GitHubAPIError as e:
            print(f"Failed to fetch BotState.json for warning config reconciliation: {e}")
            return

    saved = state.get("warning_config") or {}
    merged = dict(DEFAULT_WARNING_CONFIG)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_WARNING_CONFIG})

    # Defensive validation -- a hand-edited BotState.json (or a future
    # schema change) could hand back something out of range; clamp/replace
    # rather than let a bad value crash the next /warnings add.
    if merged["action"] not in WARNING_ACTIONS:
        merged["action"] = DEFAULT_WARNING_CONFIG["action"]
    try:
        merged["threshold"] = max(WARNING_THRESHOLD_MIN, min(WARNING_THRESHOLD_MAX, int(merged["threshold"])))
    except (TypeError, ValueError):
        merged["threshold"] = DEFAULT_WARNING_CONFIG["threshold"]
    if merged["timeout_minutes"] not in _WARNING_TIMEOUT_MINUTES:
        merged["timeout_minutes"] = DEFAULT_WARNING_CONFIG["timeout_minutes"]
    merged["enabled"] = bool(merged["enabled"])
    merged["reset_after_action"] = bool(merged["reset_after_action"])
    merged["notify_target"] = bool(merged["notify_target"])

    _warning_config = merged
    if _warning_config["enabled"]:
        print(
            "Reconciled warning auto-action config from BotState.json "
            f"(threshold={_warning_config['threshold']}, action={_warning_config['action']})."
        )


async def _apply_warning_threshold_action(
    interaction: discord.Interaction,
    user: discord.Member,
    total: int,
    cfg: Dict[str, Any],
) -> Optional[str]:
    """Carries out /warnings config's configured auto-action against `user`
    right after /warnings add just pushed their warning count to (or past)
    the configured threshold. Returns a short line describing what
    happened -- success or failure -- for _warn_add_impl to fold into its
    own response, or None if there's nothing to report. Never raises: the
    warning itself has already been recorded successfully by the time this
    runs, and a misconfigured or failed auto-action shouldn't take that
    back or crash the command that triggered it.

    Only ever called with cfg["action"] already confirmed != "none" by the
    caller -- see _warn_add_impl below."""
    meta = WARNING_ACTIONS[cfg["action"]]
    reason = f"Reached {total} warning(s) -- /warnings config auto-action threshold is {cfg['threshold']}."

    # Re-checked here (on top of _warn_add_impl's own can_moderate() call
    # for the warning itself) since role positions can change between that
    # earlier check and this one -- cheap insurance against trying to
    # kick/ban/timeout someone who technically now outranks the acting
    # moderator or the bot.
    try:
        await can_moderate(interaction, user)
    except app_commands.CheckFailure as e:
        return f"⚠️ Reached the auto-action threshold, but they couldn't be {meta['verb']}: {e}"

    try:
        if cfg["action"] == WARNING_ACTION_TIMEOUT:
            until = datetime.now(timezone.utc) + timedelta(minutes=cfg["timeout_minutes"])
            await user.timeout(until, reason=reason)
            duration_label = next(
                label for minutes, label in WARNING_TIMEOUT_CHOICES if minutes == cfg["timeout_minutes"]
            )
            summary = f"{meta['emoji']} Timed out for **{duration_label}** for reaching **{total}** warnings."
        elif cfg["action"] == WARNING_ACTION_KICK:
            await user.kick(reason=reason)
            summary = f"{meta['emoji']} Kicked for reaching **{total}** warnings."
        elif cfg["action"] == WARNING_ACTION_BAN:
            await interaction.guild.ban(user, reason=reason, delete_message_seconds=0)
            summary = f"{meta['emoji']} Banned for reaching **{total}** warnings."
        else:
            return None
    except discord.Forbidden:
        return f"⚠️ Reached the auto-action threshold, but I'm missing permissions to do that (configured action: {meta['label']})."
    except discord.HTTPException as e:
        return f"⚠️ Reached the auto-action threshold, but the auto-action failed: {e}"

    if meta["notify_key"] and cfg["notify_target"]:
        await notify_user(user, meta["notify_key"], interaction.user, reason, interaction.guild.name)

    if cfg["reset_after_action"]:
        result: Dict[str, int] = {"removed": 0}

        def _mutate(state, result=result):
            before = state.get("warnings", [])
            result["removed"] = sum(1 for w in before if str(w.get("discord_id")) == str(user.id))
            state["warnings"] = [w for w in before if str(w.get("discord_id")) != str(user.id)]
            return state

        try:
            new_state = await update_botstate(_mutate, f"Warnings auto-cleared for {user} ({user.id}) after auto-action")
            _set_warnings_cache(new_state.get("warnings", []))
            summary += " Their warning count has been reset to **0**."
        except GitHubAPIError as e:
            print(f"Failed to auto-clear warnings for {user} ({user.id}) after an auto-action: {e}")
            summary += " (Couldn't reset their warning count -- see server logs.)"

    await send_moderation_alert(interaction.client, alert_embed(
        f"{meta['emoji']} Warning Threshold Reached",
        f"{user.mention} reached **{total}** warning(s) -- {interaction.user.mention}'s `/warnings add` "
        f"triggered the configured auto-action ({meta['label']}) via `/warnings config`.",
        color=ALERT_COLOR_REMOVE,
        fields=[("Reason", reason, False)],
    ))

    return summary


class WarningConfigView(LayoutView):
    """Components V2 settings panel for /warnings config.

    Edits happen against `self.draft` -- a working copy of the live
    config -- so Cancel can discard in-progress changes and Reset to
    Defaults can restore the out-of-the-box values without touching the
    real `_warning_config` (or making any BotState.json write) until Save
    is actually pressed. Same "clear_items() then fully rebuild" approach
    as whitelist.py's WhitelistView.build(), called after every change and
    followed by an edit_message(view=self) to reflect it live."""

    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.draft: Dict[str, Any] = dict(_warning_config)
        self.pending_notice: Optional[str] = None
        self.pending_notice_color: discord.Color = discord.Color.green()
        # Populated by build() each time it (re)creates the selects, so the
        # on_*_select callbacks below have something to read .values off of.
        self.action_select: Optional[Select] = None
        self.threshold_select: Optional[Select] = None
        self.duration_select: Optional[Select] = None

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await default_ui_error(interaction, error, item, label="WarningConfigView")

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Author-only guard shared by every callback below. Returns False
        (having already told the offending user) if the click didn't come
        from whoever ran /warnings config -- same convention as
        whitelist.py's ConfirmClearLayout.confirm()/cancel(), just factored
        out once here since this view has far more callbacks than that
        one's two."""
        if interaction.user.id != self.author_id:
            await send_error(interaction, "This settings panel isn't yours -- run `/warnings config` yourself to make changes.")
            return False
        return True

    def _summary_lines(self) -> List[str]:
        d = self.draft
        if not d["enabled"]:
            return ["**Status:** 🔴 Disabled -- warnings are still recorded, but nothing else happens automatically."]

        meta = WARNING_ACTIONS[d["action"]]
        plural = "warning" if d["threshold"] == 1 else "warnings"
        lines = [
            "**Status:** 🟢 Enabled",
            f"**Trigger:** once a member reaches **{d['threshold']}** {plural}",
            f"**Action:** {meta['emoji']} {meta['label']}",
        ]
        if d["action"] == WARNING_ACTION_TIMEOUT:
            duration_label = next(label for minutes, label in WARNING_TIMEOUT_CHOICES if minutes == d["timeout_minutes"])
            lines.append(f"**Timeout duration:** {duration_label}")
        lines.append(f"**Reset warnings after it fires:** {'✅ Yes' if d['reset_after_action'] else '❌ No'}")
        lines.append(f"**DM the member when it fires:** {'✅ Yes' if d['notify_target'] else '❌ No'}")
        return lines

    def build(self):
        """(Re)builds this view's components from self.draft. Call after
        any state change, then edit_message(view=self)."""
        self.clear_items()

        if self.pending_notice:
            self.add_item(Container(TextDisplay(f"### {self.pending_notice}"), accent_color=self.pending_notice_color))
            self.pending_notice = None

        header = TextDisplay("### ⚙️ Warning Auto-Action Settings")
        intro = TextDisplay(
            "Configure what happens automatically once a member's warning count reaches a set threshold. "
            "Warnings are always recorded either way -- this only controls whether reaching the threshold "
            "*also* does something about it."
        )
        summary = TextDisplay("\n".join(self._summary_lines()))
        container = Container(header, intro, summary, accent_color=discord.Color.blurple())

        # -- Row 1: what happens
        self.action_select = Select(
            placeholder="Action -- what happens at the threshold...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label=meta["label"], value=key, emoji=meta["emoji"], default=(key == self.draft["action"]))
                for key, meta in WARNING_ACTIONS.items()
            ],
        )
        self.action_select.callback = self.on_action_select
        container.add_item(ActionRow(self.action_select))

        # -- Row 2: threshold
        self.threshold_select = Select(
            placeholder="Threshold -- how many warnings...",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=f"{n} warning" + ("" if n == 1 else "s"),
                    value=str(n),
                    default=(n == self.draft["threshold"]),
                )
                for n in range(WARNING_THRESHOLD_MIN, WARNING_THRESHOLD_MAX + 1)
            ],
        )
        self.threshold_select.callback = self.on_threshold_select
        container.add_item(ActionRow(self.threshold_select))

        # -- Row 3: timeout duration -- only meaningful (and only shown) for
        # the Timeout action, so it doesn't eat into the 5-action-row budget
        # a message gets when it'd just be dead weight for Kick/Ban/None.
        if self.draft["action"] == WARNING_ACTION_TIMEOUT:
            self.duration_select = Select(
                placeholder="Timeout duration...",
                min_values=1, max_values=1,
                options=[
                    discord.SelectOption(label=label, value=str(minutes), default=(minutes == self.draft["timeout_minutes"]))
                    for minutes, label in WARNING_TIMEOUT_CHOICES
                ],
            )
            self.duration_select.callback = self.on_duration_select
            container.add_item(ActionRow(self.duration_select))
        else:
            self.duration_select = None

        # -- Row 4: on/off toggles -- each button's own label doubles as its
        # current state, and its style (green when on, grey when off) makes
        # that state readable at a glance without needing to read the text.
        enabled_button = Button(
            label=f"Auto-Action: {'ON' if self.draft['enabled'] else 'OFF'}",
            style=discord.ButtonStyle.success if self.draft["enabled"] else discord.ButtonStyle.secondary,
        )
        enabled_button.callback = self.on_toggle_enabled

        reset_button = Button(
            label=f"Reset Warnings After: {'ON' if self.draft['reset_after_action'] else 'OFF'}",
            style=discord.ButtonStyle.success if self.draft["reset_after_action"] else discord.ButtonStyle.secondary,
        )
        reset_button.callback = self.on_toggle_reset

        notify_button = Button(
            label=f"DM Member: {'ON' if self.draft['notify_target'] else 'OFF'}",
            style=discord.ButtonStyle.success if self.draft["notify_target"] else discord.ButtonStyle.secondary,
        )
        notify_button.callback = self.on_toggle_notify

        container.add_item(ActionRow(enabled_button, reset_button, notify_button))

        # -- Row 5: commit or discard
        save_button = Button(label="💾 Save", style=discord.ButtonStyle.primary)
        save_button.callback = self.on_save
        defaults_button = Button(label="↩️ Reset to Defaults", style=discord.ButtonStyle.secondary)
        defaults_button.callback = self.on_defaults
        cancel_button = Button(label="✖️ Cancel", style=discord.ButtonStyle.danger)
        cancel_button.callback = self.on_cancel
        container.add_item(ActionRow(save_button, defaults_button, cancel_button))

        self.add_item(container)

    async def on_action_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["action"] = self.action_select.values[0]
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_threshold_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["threshold"] = int(self.threshold_select.values[0])
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_duration_select(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["timeout_minutes"] = int(self.duration_select.values[0])
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_toggle_enabled(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["enabled"] = not self.draft["enabled"]
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_toggle_reset(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["reset_after_action"] = not self.draft["reset_after_action"]
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_toggle_notify(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft["notify_target"] = not self.draft["notify_target"]
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_defaults(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.draft = dict(DEFAULT_WARNING_CONFIG)
        self.pending_notice = "↩️ Reset to defaults below -- click Save to apply, or Cancel to discard."
        self.pending_notice_color = discord.Color.blurple()
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_save(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        global _warning_config
        _warning_config = dict(self.draft)
        await _persist_warning_config(f"Warning auto-action settings updated by {interaction.user} ({interaction.user.id})")

        await send_moderation_alert(interaction.client, alert_embed(
            "⚙️ Warning Auto-Action Settings Updated",
            f"{interaction.user.mention} updated the `/warnings` auto-action settings via `/warnings config`.",
            color=ALERT_COLOR_EDIT,
            fields=[
                ("Status", "Enabled" if _warning_config["enabled"] else "Disabled", True),
                ("Threshold", str(_warning_config["threshold"]), True),
                ("Action", WARNING_ACTIONS[_warning_config["action"]]["label"], True),
            ],
        ))

        self.pending_notice = "✅ Settings saved -- these are now active."
        self.pending_notice_color = discord.Color.green()
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_cancel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.stop()
        await interaction.response.defer()
        await interaction.delete_original_response()


# =========================================================================
# /warnings add
# =========================================================================

async def _warn_add_impl(interaction: discord.Interaction, user: discord.Member, reason: str):
    try:
        await can_moderate(interaction, user)
    except app_commands.CheckFailure as e:
        return await send_error(interaction, str(e))

    await interaction.response.defer(ephemeral=True)

    entry = {
        "id": new_state_id("warn"),
        "guild_id": str(interaction.guild.id),
        "discord_id": str(user.id),
        "reason": reason,
        "moderator_id": str(interaction.user.id),
        "moderator_tag": str(interaction.user),
        "created_at": format_iso(datetime.now(timezone.utc)),
    }

    def _mutate(state, entry=entry):
        state.setdefault("warnings", []).append(entry)
        return state

    try:
        new_state = await update_botstate(_mutate, f"Warning added: {user} ({user.id})")
    except GitHubAPIError as e:
        return await send_error(interaction, f"Failed to save warning: {e}")

    _set_warnings_cache(new_state.get("warnings", []))
    total = len(_user_warnings(user.id))

    await notify_user(user, "warned", interaction.user, reason, interaction.guild.name)

    await send_moderation_alert(interaction.client, alert_embed(
        "⚠️ Member Warned",
        f"{interaction.user.mention} warned {user.mention} via `/warnings add`.",
        color=ALERT_COLOR_REMOVE,
        fields=[("Reason", reason, False), ("Total Warnings", str(total), True)],
    ))

    fields = [("Reason", reason, False), ("Total Warnings", str(total), True)]

    # /warnings config's auto-action, if configured and just reached --
    # >= rather than == so raising the threshold *after* a member already
    # had enough warnings to clear it still fires on their very next one,
    # instead of silently requiring them to rack up even more first.
    cfg = warning_config()
    if cfg["enabled"] and cfg["action"] != WARNING_ACTION_NONE and total >= cfg["threshold"]:
        action_summary = await _apply_warning_threshold_action(interaction, user, total, cfg)
        if action_summary:
            fields.append(("Auto-Action", action_summary, False))

    await send_success(interaction, f"Warned {user.mention}.", fields=fields)


# =========================================================================
# /warnings inspect
# =========================================================================

def _warning_field(w: Dict[str, Any]) -> tuple:
    ts = parse_iso(w.get("created_at"))
    when = f"<t:{int(ts.timestamp())}:F> (<t:{int(ts.timestamp())}:R>)" if ts else "Unknown time"
    moderator = f"<@{w['moderator_id']}>" if w.get("moderator_id") else (w.get("moderator_tag") or "Unknown")
    name = f"`{w.get('id', '?')}`"
    value = f"**Reason:** {w.get('reason') or 'N/A'}\n**By:** {moderator}\n**When:** {when}"
    return (name, value, False)


async def _warn_inspect_impl(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    try:
        state, _sha = await fetch_botstate_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, f"Failed to fetch warnings: {e}")

    # Live fetch above is already authoritative -- refresh the autocomplete
    # cache from it too, so a stale cache never lingers longer than it has to.
    _set_warnings_cache(state.get("warnings", []))

    warnings = _user_warnings(user.id)
    if not warnings:
        return await send_success(interaction, f"{user.mention} has no warnings.", title="✅ No Warnings")

    # Discord embeds cap out at 25 fields -- show the most recent 25 and
    # note the rest exists rather than silently dropping them with no sign
    # anything was cut.
    shown = warnings[:25]
    fields = [_warning_field(w) for w in shown]
    footer = None
    if len(warnings) > len(shown):
        footer = f"Showing the {len(shown)} most recent of {len(warnings)} total -- use /warnings delete to remove one."

    embed = build_embed(
        title=f"⚠️ Warnings for {user.display_name} ({len(warnings)})",
        color=discord.Color.orange(),
        fields=fields,
        footer=footer,
    )
    await safe_respond(interaction, embed=embed, ephemeral=True)


# =========================================================================
# /warnings clear
# =========================================================================

async def _warn_clear_impl(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    # update_botstate() may retry _mutate() more than once against a fresh
    # fetch each time (see its docstring), so the removed count is tracked
    # via a small mutable container rather than a plain local -- the last
    # attempt to actually run is the one whose count matters, and this
    # stays correct regardless of how many times that ends up being.
    result: Dict[str, int] = {"removed": 0}

    def _mutate(state, result=result):
        before = state.get("warnings", [])
        result["removed"] = sum(1 for w in before if str(w.get("discord_id")) == str(user.id))
        state["warnings"] = [w for w in before if str(w.get("discord_id")) != str(user.id)]
        return state

    try:
        new_state = await update_botstate(_mutate, f"Warnings cleared: {user} ({user.id})")
    except GitHubAPIError as e:
        return await send_error(interaction, f"Failed to clear warnings: {e}")

    _set_warnings_cache(new_state.get("warnings", []))
    removed = result["removed"]

    if removed == 0:
        return await send_success(interaction, f"{user.mention} has no warnings to clear.", title="Nothing To Clear")

    await send_moderation_alert(interaction.client, alert_embed(
        "🧹 Warnings Cleared",
        f"{interaction.user.mention} cleared **{removed}** warning(s) for {user.mention} via `/warnings clear`.",
        color=ALERT_COLOR_REMOVE,
    ))
    await send_success(interaction, f"Cleared **{removed}** warning(s) for {user.mention}.")


# =========================================================================
# /warnings delete
# =========================================================================

async def warning_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """Suggests this command's own already-picked `user` option's existing
    warnings, read from the in-memory cache above so this stays well inside
    Discord's ~3s autocomplete window -- no live GitHub fetch here, same as
    whitelisted_user_autocomplete() in whitelist.py leaning on
    get_cached_users() instead of a fresh fetch.

    Reads `user` off interaction.namespace since Discord fills a command's
    options in the order they were typed, so by the time someone's typing
    into `warning`, whatever they already picked for `user` is already
    resolved there. Returns nothing if `user` hasn't been filled in yet --
    there's nothing to suggest without knowing whose warnings to show."""
    target = interaction.namespace.user
    if not isinstance(target, (discord.Member, discord.User)):
        return []

    query = current.lower().strip()
    choices = []
    for w in _user_warnings(target.id):
        reason = (w.get("reason") or "").strip()
        label = f"{w['id']} -- {reason}" if reason else w["id"]
        if len(label) > 100:
            label = label[:97] + "..."
        haystack = f"{w['id']} {reason}".lower()
        if query and query not in haystack:
            continue
        choices.append(app_commands.Choice(name=label, value=w["id"]))
        if len(choices) >= 25:
            break
    return choices


async def _warn_delete_impl(interaction: discord.Interaction, user: discord.User, warning: str):
    await interaction.response.defer(ephemeral=True)

    query = warning.strip().lower()
    if not query:
        return await send_error(interaction, "Provide the warning to delete -- an id, or part of its reason.")

    try:
        state, _sha = await fetch_botstate_with_sha()
    except GitHubAPIError as e:
        return await send_error(interaction, f"Failed to fetch warnings: {e}")

    user_warnings = [w for w in state.get("warnings", []) if str(w.get("discord_id")) == str(user.id)]

    # An exact id match (e.g. picked straight from autocomplete) always
    # wins outright over a partial/reason match, even if that partial text
    # also happens to substring-match some other warning's reason.
    exact = [w for w in user_warnings if str(w.get("id", "")).lower() == query]
    matches = exact or [
        w for w in user_warnings
        if query in str(w.get("id", "")).lower() or query in str(w.get("reason", "")).lower()
    ]

    if not matches:
        return await send_error(interaction, f"No warning matching `{warning}` found for {user.mention}.")

    if len(matches) > 1:
        preview = "\n".join(f"`{m['id']}` -- {(m.get('reason') or 'N/A')[:60]}" for m in matches[:10])
        return await send_error(
            interaction,
            f"**{len(matches)}** warnings for {user.mention} match `{warning}` -- be more specific, "
            f"or pick one of the autocomplete suggestions:\n{preview}",
        )

    target_id = matches[0]["id"]

    def _mutate(state, target_id=target_id):
        state["warnings"] = [w for w in state.get("warnings", []) if w.get("id") != target_id]
        return state

    try:
        new_state = await update_botstate(_mutate, f"Warning deleted for {user} ({user.id}): {target_id}")
    except GitHubAPIError as e:
        return await send_error(interaction, f"Failed to delete warning: {e}")

    _set_warnings_cache(new_state.get("warnings", []))
    deleted = matches[0]

    await send_moderation_alert(interaction.client, alert_embed(
        "🗑️ Warning Deleted",
        f"{interaction.user.mention} deleted a warning for {user.mention} via `/warnings delete`.",
        color=ALERT_COLOR_REMOVE,
        fields=[("Reason", deleted.get("reason") or "N/A", False)],
    ))
    await send_success(
        interaction,
        f"Deleted warning `{deleted['id']}` for {user.mention}.",
        fields=[("Reason", deleted.get("reason") or "N/A", False)],
    )


class Warnings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # /warnings add|inspect|clear|delete all live under a single guild-
    # scoped group -- same pattern as afk.py's afk_group / access.py's
    # togglealerts_group -- so the guild restriction only needs to live
    # once, here on the top-level group, for every subcommand to inherit it.
    warnings_group = app_commands.guilds(GUILD)(
        app_commands.Group(
            name="warnings",
            description="Warn members and manage their warning history.",
        )
    )

    @warnings_group.command(name="add", description="Warns a member.")
    @app_commands.describe(user="Member to warn", reason="Reason for the warning")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def warnings_add(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        await _warn_add_impl(interaction, user, reason)

    @warnings_group.command(name="inspect", description="Gets warnings for a user.")
    @app_commands.describe(user="User to look up")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def warnings_inspect(self, interaction: discord.Interaction, user: discord.User):
        await _warn_inspect_impl(interaction, user)

    @warnings_group.command(name="clear", description="Clears all warnings for a user.")
    @app_commands.describe(user="User to clear warnings for")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def warnings_clear(self, interaction: discord.Interaction, user: discord.User):
        await _warn_clear_impl(interaction, user)

    @warnings_group.command(name="delete", description="Deletes a single warning for a user.")
    @app_commands.describe(user="User the warning belongs to", warning="Warning to delete -- its id, or part of its reason (partial or full)")
    @app_commands.autocomplete(warning=warning_autocomplete)
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def warnings_delete(self, interaction: discord.Interaction, user: discord.User, warning: str):
        await _warn_delete_impl(interaction, user, warning)

    @warnings_group.command(name="config", description="Configure automatic action taken once a member reaches a warning threshold.")
    @has_role(config.REQUIRED_ROLE_ID)
    @is_in_guild(config.GUILD_ID)
    async def warnings_config(self, interaction: discord.Interaction):
        view = WarningConfigView(interaction.user.id)
        view.build()
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Warnings(bot))