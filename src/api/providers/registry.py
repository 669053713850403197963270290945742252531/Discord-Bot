"""
api.providers.registry -- one place mapping each command's `kind`
("shorten", "paste", "file") to every provider that can fulfill it, plus
the capability flags commands/url.py needs to decide which provider-only
extras (`alias`, `access_key`, `expiry`) are valid for a given provider.

Adding a provider is: write its module (mirroring an existing module's
function signatures/return shapes as closely as possible -- every module
in this package documents its own confirmed-vs-best-effort status in its
own docstring), then add one entry below. Nothing in commands/url.py needs
to change to pick it up -- its `provider` app_commands.Choice lists and
its capability checks are all built from this dict at import time via
choices_for() below.

Every provider dict's `label` is exactly what shows up in that command's
`provider` dropdown -- kept to the "Name (what it's for)" shape from the
planning doc's Services list (e.g. "Catbox (File hosting)") so the picker
reads the same whether Discord is showing 3 options or 5.
"""

from api.providers import catbox, ez_host, is_gd, litterbox, pastebin, pastee_dev, pastey_gg, rubis, tinyurl, uguu

PROVIDERS = {
    "shorten": {
        # ez_host stays first in every kind's dict -- see choices_for()'s
        # docstring for why, and commands/url.py's `provider` params for
        # why it's every command's default.
        "ez_host": {
            "module": ez_host,
            "label": "E-Z (Pastes, file hosting, URL Shortening)",
            "supports_alias": False,
            "supports_logstats": False,
        },
        "is_gd": {
            "module": is_gd,
            "label": "is.gd (URL Shortening)",
            "supports_alias": True,
            # is.gd-exclusive within "shorten" -- see is_gd.py's module
            # docstring for the "this is NOT how /url unshorten resolves
            # a destination" clarification (that's is.gd's separate,
            # always-on Lookup API, not this).
            "supports_logstats": True,
        },
        "tinyurl": {
            "module": tinyurl,
            "label": "TinyURL (URL Shortening)",
            "supports_alias": True,
            "supports_logstats": False,
            # TinyURL's `tags`/`expires_at`/`description` create-time
            # fields, and its destination-changing "Change URL" action,
            # are paid-plan-only per TinyURL's own docs/help center -- see
            # tinyurl.py's module docstring -- so this bot doesn't expose
            # any of them, free-plan-only same as every other provider.
        },
    },
    "file": {
        "ez_host": {
            "module": ez_host,
            "label": "E-Z (Pastes, file hosting, URL Shortening)",
            "requires_expiry": False,
        },
        "catbox": {
            "module": catbox,
            "label": "Catbox (File hosting)",
            "requires_expiry": False,
        },
        "litterbox": {
            "module": litterbox,
            "label": "Litterbox (Temporary file hosting)",
            "requires_expiry": True,
        },
        "uguu": {
            "module": uguu,
            # Uguu's own homepage states this outright ("Max upload size is
            # 128 MiB & files expire after 3 hours", confirmed live
            # 2026-08-18 -- see uguu.py's module docstring) -- unlike
            # Litterbox, that 3h window is fixed rather than a per-request
            # choice, so it's baked into this label (the one place /file's
            # `provider` picker actually shows it) instead of being offered
            # as an `expiry` option the way Litterbox's is.
            "label": "Uguu (Temporary file hosting, 3h)",
            "requires_expiry": False,
            # See commands.url._url_file_impl for exactly what this does.
            # Short version: uguu.se/faq.html documents Uguu's accepted
            # file types far more broadly ("any kind of file is allowed")
            # than this bot's own default image/audio/application gate,
            # but doesn't document the actual boundaries of that -- real-
            # world testing (2026-08-24) found plain-text/source files
            # (.py, .txt) and unrecognized extensions (.litematic) upload
            # fine despite not being image/audio/application, while others
            # that DO match this gate (.pdf) still get rejected by Uguu
            # itself. With no reliable list obtainable from Uguu in either
            # direction, maintaining a local allow/deny list to mirror it
            # isn't practical -- this flag skips this bot's own gate
            # entirely for Uguu and defers to Uguu's own API as the single
            # source of truth on what it accepts, whose own rejection
            # message (uguu.py's _describe_error()) already surfaces
            # cleanly to the user when it says no.
            "skips_type_gate": True,
        },
    },
    "paste": {
        "ez_host": {
            "module": ez_host,
            "label": "E-Z (Pastes, file hosting, URL Shortening)",
            "supports_access_key": False,
            "supports_visibility": False,
            "supports_expires": False,
            "supports_encrypt": False,
            "supports_views": False,
            "supports_extra_files": False,
            # Confirmed 2026-08-17 (live) -- unlike every other /paste
            # provider in this dict, e-z.host's own POST /paste rejects a
            # paste with `title`/`description` left blank. Before this was
            # confirmed, a blank one didn't even get a normal validation
            # error back -- it hit e-z.host's own broken, non-JSON
            # error-handler crash instead (see ez_host.py's module
            # docstring's "paste endpoint" note). Enforced for real in
            # ez_host.create_paste() itself (true regardless of call
            # path); flagged here too so commands/url.py's
            # _url_paste_impl can reject a blank one early, before even
            # deferring the interaction, same as every other
            # provider-capability check there.
            "requires_title": True,
            "requires_description": True,
        },
        "pastebin": {
            "module": pastebin,
            "label": "Pastebin (Pastes)",
            # Confirmed 2026-08-17 straight off pastebin.com/doc_api -- see
            # pastebin.py's own module docstring for the full writeup,
            # including why this is the one /paste provider in this package
            # that talks form-urlencoded + plain-text instead of JSON.
            #
            # `access_key` here means the same thing it does for pastee_dev
            # above (an optional api_user_key override, letting a paste be
            # attributed to a real Pastebin account instead of staying an
            # anonymous guest paste) -- NOT pastey_gg's unrelated
            # per-paste-password meaning below. See pastebin.py's own module
            # docstring.
            "supports_access_key": True,
            # public/unlisted/private -- the one /paste provider in this
            # package with all three (api.providers.rubis only has
            # public/private). See commands/url.py's own
            # _PASTE_VISIBILITY_CHOICES, built from both providers' valid-
            # value tuples together so "unlisted" shows up as a choice at
            # all.
            "supports_visibility": True,
            # api_paste_expire_date -- a small, fixed 9-value enum (see
            # pastebin.EXPIRE_DATE_HELP), NOT the same shape as pastee_dev's
            # own free-form `expiration` mini-language or pastey_gg's
            # absolute RFC3339 `expires_at` -- commands/url.py routes to
            # pastebin.create_paste's own `expire_date` kwarg specifically
            # rather than reusing either of those existing code paths.
            "supports_expires": True,
            "supports_encrypt": False,
            "supports_views": False,
            # Pastebin's own create-paste endpoint has no multi-file/
            # sections concept anywhere in its docs -- one paste is always
            # exactly one api_paste_code body.
            "supports_extra_files": False,
            # Pastebin-exclusive within /paste -- api_folder_key, which
            # (like `private` visibility above) requires an api_user_key.
            # Not a capability any other provider in this package shares,
            # so this is its own flag rather than folded into
            # supports_access_key -- commands/url.py's `folder_key` option
            # is gated on this provider key by name, same convention as
            # `views` (pastey_gg) and `encrypt` (pastee_dev) below.
            "supports_folder": True,
        },
        # Renamed from "paste_ee" (module paste_ee.py) to "pastee_dev" to
        # match the module rename below -- see pastee_dev.py's own
        # docstring for why. NOTE: this key is also the top-level
        # namespace api/github.py's shortened-urls.json storage uses for
        # every paste this provider has ever created (see that file's
        # "Shortened URLs" section header comment) -- renaming it here
        # only affects *new* pastes going forward. Any pastes already
        # stored under the old "paste_ee" key in that JSON need a
        # one-time manual rename of that top-level key to "pastee_dev"
        # in storage/shortened-urls.json (via the GitHub repo directly,
        # or a small one-off script) or they'll silently stop showing up
        # in anything that reads this provider's history.
        "pastee_dev": {
            "module": pastee_dev,
            "label": "pastee.dev (Pastes)",
            "supports_access_key": True,
            "supports_visibility": False,
            "supports_expires": True,
            # pastee.dev-exclusive within /paste -- its own documented
            # `encrypted` request field (see pastee_dev.py's create_paste()
            # docstring for exactly what this does/doesn't cover). No
            # other provider in this package documents an equivalent, so
            # this is left False everywhere else rather than guessed at.
            "supports_encrypt": True,
            "supports_views": False,
            # pastee.dev's own API accepts multiple `sections` per paste
            # -- pastee_dev.py's create_paste() now merges that with
            # /paste's file1-file4 extra-file feature via its own
            # `extra_files` kwarg (same {"content", "name", "language"}
            # shape pastey_gg below already used), so this provider shares
            # that Discord-side multi-file option with pastey_gg rather
            # than being the only one without it.
            "supports_extra_files": True,
        },
        "pastey_gg": {
            "module": pastey_gg,
            "label": "pastey.gg (Pastes)",
            # Confirmed 2026-08-16 against a fresh pull of Pastey.gg's own
            # published backend source (github.com/Pastey-gg/Echo) -- see
            # pastey_gg.py's module docstring for the full writeup.
            #
            # `access_key` here does NOT mean "use your own API key
            # instead of this bot's default" the way it does for
            # pastee_dev above -- Echo still has no account/API-key
            # concept at all for creating a paste. It's reused, unchanged,
            # as pastey.gg's own per-paste `password` field
            # (models.CreatePaste.Password in models/pastes.go), so a
            # per-call password doesn't need its own separate /paste
            # option -- see pastey_gg.py's module docstring, point 1.
            "supports_access_key": True,
            "supports_visibility": False,
            # Confirmed 2026-08-16 (same source pull): CreatePaste's
            # ExpiresAt is a real, stored `*time.Time` Echo actually
            # honors -- see pastey_gg.py's create_paste() docstring for
            # the RFC3339 shape it requires and how /paste's `expires`
            # option is normalized into that shape.
            "supports_expires": True,
            "supports_encrypt": False,
            # pastey_gg-exclusive within /paste -- Echo's validatePaste()
            # (routes/pastes.go) enforces `remaining_views` between 1 and
            # 1000 (pastey_gg.REMAINING_VIEWS_MIN/MAX). No other provider
            # in this package documents an equivalent view-limit field, so
            # /paste's `views` option is rejected up front for anything
            # else (see commands/url.py's _url_paste_impl).
            "supports_views": True,
            # pastey_gg.py's create_paste() has supported `extra_files`
            # since the 2026-08-16 rewrite (its module docstring, point
            # 4) -- see commands/url.py's own MAX_PASTE_EXTRA_FILES for
            # how /paste's fixed fileN_text/fileN_title/fileN_language
            # slots feed it, and why that slot count isn't the same thing
            # as Echo's own server-side MaxFiles.
            "supports_extra_files": True,
        },
        "rubis": {
            "module": rubis,
            "label": "Rubiš (Pastes)",
            # "access key" means something different for Rubiš than it
            # does for pastee_dev/pastey_gg below -- see rubis.py's module
            # docstring for why that keeps this False rather than True.
            "supports_access_key": False,
            "supports_visibility": True,
            "supports_expires": False,
            "supports_encrypt": False,
            "supports_views": False,
            "supports_extra_files": False,
        },
    },
}

# Every provider key for every kind, e.g. {"ez_host", "is_gd", "tinyurl",
# "catbox", "litterbox", "pastebin", "pastee_dev", "pastey_gg", "rubis"} --
# used by api/github.py's schema validation and anywhere else that needs
# to sanity-check a stored `provider` value without caring which `kind`
# it belongs to.
ALL_PROVIDER_KEYS = {key for kind in PROVIDERS.values() for key in kind}

DEFAULT_PROVIDER = "ez_host"


def choices_for(kind: str):
    """Builds the app_commands.Choice list for `kind`'s `provider` option
    (used by /url shorten, /paste, and /file), from PROVIDERS[kind]'s
    insertion order. ez_host is first in every kind's dict specifically
    so it's also first/most-visible in the picker -- it's each of those
    commands' pre-existing default, so keeping it visually first preserves
    the "muscle memory" the planning doc calls out."""
    from discord import app_commands

    return [app_commands.Choice(name=info["label"], value=key) for key, info in PROVIDERS[kind].items()]