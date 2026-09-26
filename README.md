# Discord-Bot

A Discord bot backed by a GitHub-hosted `Users` whitelist database, with
key generation/redemption, HWID locking, moderation, reaction roles,
password/passphrase and classic-cipher tools, modern authenticated
encryption, string entropy analysis, a QR code generator, and a persistent
control panel. Originally a single ~4,100-line `main.py` + ~1,600-line
`bot_api.py`, refactored into this package -- and extended since with
newer commands (classic ciphers + Identify, `/entropy`, bulk `/genpass`,
modern `/encrypt`/`/decrypt`, `/qrcode`, and more). 84 commands total
today (69 slash commands + 15 user context menus) -- `start.py` logs the
live count on every boot rather than this README (or any hardcoded
constant in the code) needing to be hand-updated as commands are added or
removed.

## Layout

```
Discord-Bot/
├── src/
│   ├── start.py                 # entry point
│   ├── keep_alive.py            # tiny Flask server so host platforms see an open port
│   ├── api/                     # shared library code (no Discord commands live here)
│   │   ├── __init__.py          # re-exports everything below, so cogs can `from api import X`
│   │   ├── config.py            # env-driven constants (Discord IDs, GitHub repo, secrets)
│   │   ├── github.py            # GitHub Contents API + Users cache, valid keys, stored script
│   │   ├── users.py             # user-record lookups/building + buyer role revocation
│   │   ├── keys.py              # key generation + input validation
│   │   ├── time_utils.py        # date formatting/parsing + temp-whitelist expiration
│   │   ├── hashing.py           # /hash algorithm utilities
│   │   ├── transforms.py        # /transform's stylized-Unicode text styles
│   │   ├── encoding.py          # /encode encode & /encode decode's algorithms + Identify heuristic
│   │   ├── ciphers.py           # /cipher encrypt & /cipher decrypt's classical cipher algorithms (Caesar, Vigenère, Playfair, Atbash, Rail Fence, Columnar Transposition, Baconian, Pigpen, Polybius Square, Simple Substitution, ROT47, Affine, Autokey, Beaufort, Trithemius, Bifid, Hill) + Identify heuristic
│   │   ├── encryption.py        # /encrypt & /decrypt's modern authenticated encryption (AES-256-GCM, ChaCha20-Poly1305, Blowfish, Triple DES, ECC/ECIES, One-Time Pad)
│   │   ├── entropy.py           # /entropy password/string strength + crack-time estimation
│   │   ├── passwords.py         # /genpass generation logic (random + EFF-wordlist passphrase)
│   │   ├── data/                # bundled data for /genpass's Passphrase mode
│   │   │   ├── README.md        # source/license note (CC BY 3.0, EFF) for the wordlist below
│   │   │   └── eff_large_wordlist.txt  # EFF Long Wordlist (7,776 entries) for Diceware-style passphrases
│   │   ├── qrcode_gen.py        # /qrcode generate's encoding + Pillow rendering (solid/rainbow, styles) & /qrcode decode's OpenCV scanning
│   │   ├── discord_helpers.py   # embeds, interaction responders, permission checks, shared Components V2 layouts
│   │   ├── alerts.py            # staff Alerts channel logging (send_alert/alert_embed)
│   │   ├── redirect_resolver.py # /url unshorten's SSRF-guarded live redirect-chain follower
│   │   ├── ssrf_guard.py        # blocks outbound requests to private/loopback/link-local hosts
│   │   └── providers/           # third-party URL-shortening/paste/file-hosting API clients -- see "Multi-provider..." below
│   │       ├── registry.py      # maps /url shorten|update, /paste, /file's `provider` choices to modules + capability flags
│   │       ├── errors.py        # shared ProviderAPIError base every provider's own error class subclasses
│   │       ├── util.py          # shared session handling, extract_short_code(), require_key()
│   │       ├── languages.py     # /paste's `language` autocomplete suggestion list (~137 entries)
│   │       ├── ez_host.py       # e-z.host (shorten/paste/file) -- confirmed; default provider for all three
│   │       ├── is_gd.py         # is.gd/v.gd (shorten) -- confirmed; also backs /url unshorten's is.gd Lookup API fallback
│   │       ├── tinyurl.py       # TinyURL (shorten + update) -- confirmed
│   │       ├── catbox.py        # Catbox (file) -- confirmed
│   │       ├── litterbox.py     # Litterbox (file) -- confirmed
│   │       ├── paste_ee.py      # paste.ee (paste) -- confirmed
│   │       ├── pastey_gg.py     # pastey.gg (paste) -- best-effort/unverified, see its own module docstring
│   │       └── rubis.py         # Rubiš by Numelon (paste) -- best-effort/unverified, see its own module docstring
│   └── commands/                # one cog per file -- each is a discord.py extension
│       ├── __init__.py          # empty -- just makes this a package
│       ├── info.py              # /botstatus, /myinfo, /avatar, /ratelimits
│       ├── utility.py           # /hash, /transform, /encode (encode/decode/help subcommands), /diff, /entropy, /coinflip
│       ├── ciphers.py           # /cipher (encrypt/decrypt/help subcommands) -- classic cipher encode/decode + Identify
│       ├── encryption.py        # /encrypt, /decrypt -- modern authenticated encryption
│       ├── genpass.py           # /genpass -- password/passphrase generator, single or bulk (up to 10)
│       ├── moderation.py        # /ban, /checkban, /unban, /kick, /mute, /unmute, /purge, /ghostping, /dm, /slowmode, /togglelock, /togglelockdown
│       ├── whitelist.py         # /whitelist, /bulkwhitelist, /register, /editwhitelist, /edituser, /viewwhitelist, /fetchuser, /unwhitelist, /clearnotes, /clearregistrations, /checkregistration, /hwidhelp
│       ├── keys_hwid.py         # /key generate|validate|fetch|clear, /tempwhitelist, /checktemp, /extend, /forceresethwid, /resethwidcooldown
│       ├── database.py          # /dbsearch, /export, /upload, /rollback, /commithistory, /fetchcommit, /verifydata
│       ├── panel.py             # /createpanel + the persistent ControlPanelView
│       ├── access.py            # /toggleaccess, /tempaccess, /togglealerts (whitelist/moderation subcommands)
│       ├── reaction_roles.py    # /reactionrole
│       ├── afk.py               # /afk (set/clear/mod clear/mod check subcommands) -- AFK status with ping/reply notifications
│       ├── qrcode.py            # /qrcode (generate/decode/help subcommands) -- QR code generator + scanner
│       ├── warnings.py          # /warnings (add/inspect/clear/delete subcommands) -- durable member warning history
│       ├── url.py               # /url (shorten/update/unshorten/clear subcommands), /paste, /file -- see "Multi-provider..." below
│       ├── obfuscate.py         # /obfuscate -- AST-based Luau source protection
│       └── context_menus.py     # the 15 right-click "user" context menu commands
│   └── obfuscator/               # AST/compiler backend used by /obfuscate
│       ├── parser.py             # Tree-sitter Luau parser adapter
│       ├── ast.py                # lossless source-range AST helpers
│       ├── lexer.py              # lexical safety utilities
│       ├── generator.py          # validated non-overlapping source edits
│       ├── pipeline.py           # staged/reparsed compiler pipeline
│       ├── bytecode_compiler.py  # conservative Luau -> custom-bytecode compiler
│       ├── bytecode_vm.py        # polymorphic register/stack VM backend
│       ├── virtual_machine.py    # legacy semantic-safe source VM fallback
│       └── transforms/            # scope rename, strings, MBA, CF, semantic noise
├── storage/                      # permittedKeys.txt, storedscript.lua, test scripts for createpanel
├── .env.example
├── requirements.txt
└── README.md
```

`api/` and `commands/` are plain top-level packages (not namespaced under
`src`) -- `start.py` puts `src/` on `sys.path` itself, so `from api import
config` and `from commands.whitelist import WhitelistModal` work the same
way from any cog without needing `src` to be a package itself.

## Cipher, encoding, and encryption coverage

`/cipher encrypt` & `/cipher decrypt` (classic, pen-and-paper ciphers -- `api/ciphers.py`):
Caesar, Atbash, Simple Substitution, Vigenère, Playfair, Rail Fence,
Columnar Transposition, Baconian, Pigpen, Polybius Square, ROT47, Affine,
Autokey, Beaufort, Trithemius, Bifid, Hill, and Identify (a heuristic that
guesses which of these produced a given ciphertext, using index-of-coincidence
and chi-squared frequency analysis -- see the module docstring for its
limits: keyed ciphers can only ever be suggested, never auto-solved).

`/encode encode` & `/encode decode` (reversible text encodings, no secrecy -- `api/encoding.py`):
Base64, Base64URL, Base32, Base58, Base85, URL Encode, Quoted-Printable,
SAML Encode, Pretty JSON, UTF-8/16/32, Hexadecimal, Binary, Decimal (code
points), Unicode Escape, HTML Entities, ROT13, Morse Code, Braille,
Phonetic Alphabet (NATO), Emoji, and Identify.

`/encrypt` & `/decrypt` (real, keyed encryption -- `api/encryption.py`):
AES-256-GCM, ChaCha20-Poly1305, Blowfish, Triple DES, ECC (ECIES over
SECP256R1), and a true One-Time Pad. All are passphrase/key-based; leaving
the key blank generates a strong one for you (which you must save to
decrypt). AES and ChaCha20 are authenticated -- a wrong key or tampered
ciphertext always fails cleanly rather than returning garbage. Blowfish and
Triple DES are legacy 64-bit-block ciphers included for compatibility, not
recommended for anything that needs to stay secret.

## QR code generation

`/qrcode generate` (`api/qrcode_gen.py`): remade from a standalone tkinter
tool into a native command. Encodes text/a URL (up to 2,000 characters,
auto-sized to the smallest QR version that fits) with a solid color or a
diagonal rainbow gradient, an optional transparent background, a module
`style` (square/rounded/dots -- the 3 corner finder patterns always stay
solid squares regardless of style, since that's what a scanner locks onto
first), and a selectable error-correction level (L/M/Q/H, auto-raised to Q
for rainbow codes to help offset lower-contrast hues). `color` takes a hex
code or CSS name with autocompleted presets; `/qrcode help` is a quick
reference for all of it. Every render is validated up front (rejects a
color too close to invisible against its background) and the result
surfaces any scan-reliability caveats (rainbow, a light custom color, or a
transparent background flattened to JPG) so whoever generates one knows to
test-scan it.

`/qrcode decode` (`api/qrcode_gen.py`): the reverse direction -- reads a
QR code back out of an uploaded image via OpenCV. Tries a multi-code pass
first (so more than one QR code in the same image all get read), falls
back to a single-code pass for cases the multi-code detector alone misses
(e.g. this bot's own `dots` style), and as a last resort retries both
against an adaptive-threshold version of the image for low-contrast or
unevenly-lit photos. A transparent background is composited onto white
before scanning rather than naively dropped, since dropping it would
otherwise turn every transparent pixel solid black -- indistinguishable
from the code's own dark modules. The result lists every payload it
managed to decode, and flags if any additional QR-shaped patterns were
spotted but couldn't be read (damaged, obstructed, or too small).

Alongside the raw decoded text, each result also gets a predicted-result
field guessing what a phone's camera would actually *do* with that
payload -- the same well-known QR "smart" content conventions camera apps
special-case instead of showing raw text: a website link, a Wi-Fi network
(SSID/security/password parsed out, password spoiler-tagged), a contact
card (vCard/MECARD), a calendar event, a map location, an email/phone/SMS
shortcut, an authenticator (2FA) setup link, or -- if nothing matches --
plain text.

## Multi-provider link shortening, pastes, and file hosting

`/url shorten`, `/url update`, `/url unshorten`, `/url clear`, `/paste`,
and `/file` (`commands/url.py`) each work through a choice of third-party
provider rather than being hardcoded to one. `api/providers/registry.py`'s
`PROVIDERS` dict is the single source of truth for this: it maps each
command's kind (`shorten`/`paste`/`file`) to every provider that can
fulfill it, plus capability flags (`supports_alias`, `supports_update`,
`requires_expiry`, `supports_access_key`, `supports_logstats`,
`supports_tags`, `supports_expires_at`, `supports_description`,
`supports_visibility`, `supports_expires`) that gate which provider-only
extras are valid for a given provider -- picking an option a provider
doesn't support is rejected with a clear message rather than silently
ignored or hidden. e-z.host (`api/providers/ez_host.py`) is the default
provider on all three commands, matching this feature's original
single-provider behavior.

| Command(s) | Providers | Provider-only extras |
|---|---|---|
| `/url shorten`, `/url update` | E-Z, is.gd (+v.gd), TinyURL | `alias` (is.gd, TinyURL); `logstats` (is.gd -- click-stat logging, **not** the same thing as destination lookup, see below); `tags`/`expires_at`/`description` (TinyURL, confirmed paid-plan-only -- silently ineffective, not an error, on a free-plan token); `/url update` only ever lists TinyURL, the one provider with `supports_update=True` (and itself confirmed **paid-plan-only** -- a free-plan token gets rejected outright) |
| `/paste` | E-Z, paste.ee, pastey.gg, Rubiš | `access_key` (paste.ee, pastey.gg -- bring your own key instead of this bot's configured default); `visibility`; `expires` (paste.ee -- one shared option name, routed to each provider's own differently-formatted field under the hood); `language` gets autocomplete suggestions from `api/providers/languages.py` (~137 entries, Python/Lua/Luau/JS-family prioritized) but isn't a restricted choice -- every provider treats it as an unvalidated passthrough string, so a spelling that isn't in the suggestion list still works fine |
| `/file` | E-Z, Catbox, Litterbox | `expiry` (Litterbox only, one of `1h`/`12h`/`24h`/`72h` -- Litterbox's API requires one, so it defaults to `1h` if the option's left blank while Litterbox is selected) |

Every successful create is persisted to `storage/shortened-urls.json`
(`api/github.py`) the moment the provider responds, under that provider's
own namespace alongside every other provider that's ever been used --
most providers only ever hand back a deletion credential once, at
creation, so it has to be captured immediately or it's gone for good. A
few providers (Litterbox, is.gd, free-tier TinyURL) never hand one back
at all; the success reply's "save this in case you need to delete it"
note simply doesn't appear in that case rather than showing a blank
field.

`/url unshorten` checks that local store across *every* provider first
(not just whichever one created the link), since `extract_short_code()`
and the store lookup are both provider-agnostic. If a link isn't found
there and it's an is.gd/v.gd link specifically, it calls is.gd's own
dedicated URL Lookup API (`api/providers/is_gd.py`'s `lookup_url()`)
instead of visiting the link live -- is.gd's own API docs ask consumers
to prefer that over repeatedly following redirects, and unlike this
bot's own store, it works for *any* is.gd/v.gd link, not just one this
bot created. This is a genuinely different feature from is.gd's
`logstats` option above -- `logstats` only toggles click-statistics
logging visible on is.gd's own site and has nothing to do with resolving
a destination; see `is_gd.py`'s module docstring for the full
distinction, written up after an earlier draft of this feature conflated
the two. Anything that isn't in the local store and isn't an is.gd/v.gd
link falls back to actually following the redirect chain live
(`api/redirect_resolver.py`, SSRF-guarded per hop via
`api/ssrf_guard.py`).

**Confirmed vs. best-effort:** e-z.host, is.gd, TinyURL, Catbox,
Litterbox, and paste.ee are all implemented against each
provider's own published API documentation. **pastey.gg and Rubiš are
best-effort/unverified** -- neither publishes an API reference this
bot's own tooling could actually fetch (both docs sites are JS-rendered
single-page apps), so `api/providers/pastey_gg.py` and
`api/providers/rubis.py` are educated-guess implementations with
defensive, multiple-field-name response parsing, clearly flagged as such
in their own module docstrings. Treat `/paste` against either provider
as experimental until it's been run against a real account and the
module corrected to match reality.

Optional per-provider API keys (`.env.example`) -- unlike
`EZ_HOST_API_KEY`, none of these are required at boot; a deployment that
never sets one just can't use that specific provider, and gets a clear,
named error the moment someone actually picks it, not a startup failure:
`TINYURL_API_KEY`, `CATBOX_USERHASH` (optional even when using Catbox --
uploads are anonymous by default), and `PASTE_EE_API_KEY`
## Luau source obfuscation

`/obfuscate` accepts a required `.lua` or `.luau` source-file attachment. The command runs immediately with one optional boolean argument: **VM Compression** (default `false`). The successful result reports the selected option state. When VM Compression is enabled, the result reports the measured payload change and compares the final protected file against an identical build with compression disabled. The message also classifies the source by size and structural complexity so it can distinguish a real artifact-size benefit from a payload reduction that was too small to offset runtime/metadata overhead.

The pipeline has a production **semantic-safe** default profile. It validates the Luau AST but does not perform source-to-source rewriting of the uploaded program unless an experimental transformation is explicitly enabled in configuration. Local renaming, control-flow rewriting, MBA literal rewriting, string-pool rewriting, comment removal, and synthetic source noise remain opt-in experimental transformations because syntax-only rewriting cannot prove preservation for every form of Luau (callbacks, metatables, overloaded behavior, custom environments, executor APIs, and debug-sensitive code).

Virtualization now uses a two-tier backend. The first backend is a conservative **Luau-to-custom-bytecode compiler** that lowers supported statements and expressions into a compact instruction set with constant-pool entries, nested prototypes, lexical environment links, explicit control-flow targets, and multiple-return operations. The generated artifact contains the custom bytecode and a polymorphic **register/stack VM**; it does not embed the original source text for supported programs.

The compiler intentionally supports a well-defined subset instead of pretending to reproduce every Luau feature immediately. Constructs that cannot be represented safely are rejected by the compiler and automatically routed to `virtual_machine.py`, the existing semantic-safe encrypted source-payload VM. This keeps `/obfuscate` functional for scripts outside the custom backend's current coverage while letting supported scripts use genuine program virtualization.

Every custom-VM build gets a randomized runtime surface: virtual opcode values, encoded instruction fields, record guards, payload keys, decoder variants, constant encodings, register/stack runtime names, and adaptive structural budgets vary per build. **Intense VM Structure** is an automatic obfuscation factor on every build; it inserts multiple opaque relay stages around the bytecode interpreter without changing the program's observable behavior. Control-flow decoys, dead handlers, payload encryption, constant-pool encryption, and distributed integrity checks further increase the amount of generated runtime state that must be understood.

**VM Compression** uses a lossless LZ-style codec over the serialized VM bytecode before encryption when it actually reduces that representation. The command compares the final protected artifact with an otherwise identical uncompressed build so the reported result distinguishes genuine file-size savings from cases where decompression/runtime metadata costs more than the compressed payload saved.

The legacy source VM retains its existing distributed anti-tamper and payload-integrity layers as the automatic fallback. The custom bytecode backend also verifies instruction records and encrypted constant data before dispatch. Neither backend can make runtime observation impossible: an execution environment controlled by an analyst can instrument the VM or the program after the relevant state has been reconstructed.

The obfuscator requires `tree-sitter==0.25.2` and `tree-sitter-luau==1.2.0`.