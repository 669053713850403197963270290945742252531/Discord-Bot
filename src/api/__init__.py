"""
api package -- shared configuration, Supabase persistence, storage, validation
utilities, and Discord helper functions used across the bot.

The bot no longer depends on GitHub for runtime persistence. License/user
records, durable bot state, shortened URLs/pastes/files, and protected game
scripts use Supabase-backed persistence.
"""

from . import config
from .config import (
    DISCORD_TOKEN, EZ_HOST_API_KEY,
    GUILD_ID, REQUIRED_ROLE_ID, REACTION_ROLE_CHANNEL_ID,
    PANEL_CHANNEL_ID, BUYER_ROLE_ID, ALERTS_CHANNEL_ID,
    LOCAL_TZ,
    SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_GAME_SCRIPTS_BUCKET,
)


# Every provider's own `<Provider>APIError` (EZHostAPIError, TinyURLAPIError,
# ...) subclasses this -- see api/providers/errors.py. Re-exported alongside
# EZHostAPIError below for backward compatibility with anything still doing
# `from api import EZHostAPIError`.
from .providers.errors import ProviderAPIError
from .providers.ez_host import EZHostAPIError

from .supabase_db import (
    fetch_users, fetch_users_with_sha, fetch_api_text_and_sha, commit_content, commit_users,
    get_license_by_key, get_license_by_discord_id, get_license_by_identifier,
    create_license, update_license, delete_license, redeem_license, set_license_games,
    get_license_game_ids, list_games, get_game, create_game, delete_game, update_game, game_allowed, complete_successful_execution,
)


from .bot_state import BotStateError, fetch_botstate, update_botstate, new_state_id

from .shortened_urls import (
    ShortenedURLStoreError, fetch_all_shortened_urls, get_shortened_urls,
    find_shortened_url_entry, save_shortened_url,
    find_matching_shortened_urls, clear_shortened_urls,
)

from .supabase_storage import SupabaseStorageError, fetch_game_script, fetch_game_script_bytes, upload_game_script, delete_game_script, get_game_script_filename

from .users import (
    find_user_by_discord_id, find_user_by_key,
    remove_user_by_discord_id, build_user_entry,
    revoke_buyer_role, find_removed_discord_ids,
)

from .keys import (
    generate_key, generate_unique_key, generate_unique_keys,
    parse_key_length_range, is_valid_discord_id, is_valid_url, is_valid_date,
)

from .time_utils import (
    format_join_date, parse_join_date, format_discord_timestamp, humanize_timeleft,
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


from .alerts import (
    send_alert, alert_embed,
    ALERT_COLOR_ADD, ALERT_COLOR_REMOVE, ALERT_COLOR_EDIT, ALERT_COLOR_TEMP, ALERT_COLOR_CAUTION,
)