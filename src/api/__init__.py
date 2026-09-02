"""
api package -- shared constants, GitHub Contents-API helpers, validation
utilities, and Discord helper functions used across every cog.

This used to be a single bot_api.py; it's split by concern so each file
stays a manageable size:

    config.py          env-driven constants (Discord IDs, GitHub repo, secrets)
    github.py          GitHub Contents API + Users.json cache, permitted keys, stored script
    users.py           user-record lookups/building + buyer role revocation
    keys.py            key generation + input validation
    time_utils.py       date formatting/parsing + temp-whitelist expiration
    hashing.py         /hash algorithm utilities
    transforms.py       /transform's stylized-Unicode text styles
    encoding.py         /encode encode and /encode decode's algorithms + Identify heuristic
    ciphers.py          /cipher encrypt and /cipher decrypt's classical cipher algorithms + Identify heuristic
    encryption.py       /encrypt and /decrypt's modern authenticated-encryption algorithms
    qrcode_gen.py       /qrcode generate's encoding + Pillow rendering (solid/rainbow, styles)
    discord_helpers.py embeds, interaction responders, permission checks
    alerts.py           staff Alerts channel logging (send_alert/alert_embed)
    providers/          third-party paste/file-hosting/URL-shortening API clients (ez_host.py,
                        is_gd.py, tinyurl.py, catbox.py, litterbox.py, pastebin.py,
                        pastee_dev.py, pastey_gg.py, rubis.py) plus the registry.py that maps
                        /url shorten, /paste, and /file's `provider` choices to them

Everything below is re-exported here too, so cogs can do either
`from api import github` or `from api.github import fetch_users_with_sha`.
"""

from . import config
from .config import (
    DISCORD_TOKEN, GITHUB_TOKEN, EZ_HOST_API_KEY,
    GUILD_ID, REQUIRED_ROLE_ID, REGISTRATION_CHANNEL_ID, REACTION_ROLE_CHANNEL_ID,
    PANEL_CHANNEL_ID, BUYER_ROLE_ID, ALERTS_CHANNEL_ID,
    LOCAL_TZ, RESET_HWID_COOLDOWN,
    OWNER, REPO, FILE_PATH, BRANCH, RAW_URL, API_URL,
    STORAGE_REPO, STORAGE_BRANCH,
    PERMITTED_KEYS_FILE_PATH, STORED_SCRIPT_FILE_PATH,
    HEADERS,
)

from .github import (
    GitHubAPIError,
    fetch_raw_text, fetch_api_file, get_current_sha,
    fetch_users_with_sha, fetch_api_text_and_sha, commit_content, commit_users,
    get_cached_users, cached_users_updated_at,
    set_users_cache, refresh_users_cache,
    register_refresh_task, next_cache_refresh,
    register_bot_loop, trigger_cache_refresh_threadsafe,
    list_commits, get_commit,
    fetch_rate_limit,
    fetch_permitted_keys_with_sha, commit_permitted_keys,
    remove_permitted_key, remove_permitted_keys, remove_first_n_permitted_keys,
    fetch_stored_script, fetch_stored_script_with_sha, commit_stored_script,
    inject_script_key, validate_stored_script,
    fetch_shortened_urls_with_sha, commit_shortened_urls, update_shortened_urls,
    get_shortened_urls, save_shortened_url,
)

# Every provider's own `<Provider>APIError` (EZHostAPIError, TinyURLAPIError,
# ...) subclasses this -- see api/providers/errors.py. Re-exported alongside
# EZHostAPIError below for backward compatibility with anything still doing
# `from api import EZHostAPIError`.
from .providers.errors import ProviderAPIError
from .providers.ez_host import EZHostAPIError

from .users import (
    find_user_by_discord_id, find_user_by_hwid, find_user_by_key,
    remove_user_by_discord_id, build_user_entry,
    revoke_buyer_role, find_removed_discord_ids,
)

from .keys import (
    generate_key, generate_unique_key, generate_unique_keys,
    parse_key_length_range, is_valid_hwid, is_valid_discord_id, is_valid_url, is_valid_date,
)

from .time_utils import (
    format_join_date, parse_join_date, format_discord_timestamp,
    format_expiration_note, parse_expiration_note, is_notes_locked,
    humanize_timeleft, hwid_reset_cooldown_remaining,
)

from .hashing import get_available_hash_algorithms, hash_text, SHAKE_OUTPUT_BYTES

from .transforms import TRANSFORM_FORMAT_CHOICES, transform_text

from .encoding import ENCODING_ALGORITHMS, ENCODING_CHOICES, IDENTIFY_CHOICE_VALUE, encode_text, decode_text, identify_encoding

from .ciphers import (
    CIPHER_ALGORITHMS, CIPHER_CHOICES, cipher_text, decipher_text,
    identify_cipher, IDENTIFY_CHOICE_VALUE as CIPHER_IDENTIFY_CHOICE_VALUE,
)

from .encryption import ENCRYPTION_ALGORITHMS, ENCRYPTION_CHOICES, encrypt_text, decrypt_text

from .qrcode_gen import (
    QROptions, QRResult, generate_qr, parse_color, swatch_emoji,
    SCALE_MIN, SCALE_MAX, DEFAULT_SCALE, STYLES, DEFAULT_STYLE,
    ERROR_CORRECTION_LEVELS, DEFAULT_ERROR_CORRECTION, RAINBOW_DEFAULT_ERROR_CORRECTION,
    PRESET_COLORS, MAX_TEXT_LENGTH,
)

from .discord_helpers import (
    build_embed, success_embed, error_embed,
    safe_respond, send_success, send_error, edit_or_send_error,
    notify_user, notify_permission_error,
    dms_enabled, set_dms_enabled, persist_dms_enabled_state, reconcile_dms_enabled,
    has_role, is_in_guild, can_moderate,
    file_success_layout, status_layout,
)

from .webhook_sync import sync_webhook_url

from .alerts import (
    send_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
)