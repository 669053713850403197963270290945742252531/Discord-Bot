# Discord-Bot

A Discord bot backed by a GitHub-hosted `Users` whitelist database, with
key generation/redemption, HWID locking, moderation, reaction roles,
password/passphrase and classic-cipher tools, modern authenticated
encryption, string entropy analysis, and a persistent control panel.
Originally a single ~4,100-line `main.py` + ~1,600-line `bot_api.py`,
refactored into this package -- and extended since with newer commands
(classic ciphers + Identify, `/entropy`, bulk `/genpass`, modern
`/encrypt`/`/decrypt`, and more). 80 commands total today (65 slash
commands + 15 user context menus) -- `start.py` logs the live count on
every boot rather than this README (or any hardcoded constant in the code)
needing to be hand-updated as commands are added or removed.

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
│   │   └── discord_helpers.py   # embeds, interaction responders, permission checks, shared Components V2 layouts
│   └── commands/                # one cog per file -- each is a discord.py extension
│       ├── __init__.py          # empty -- just makes this a package
│       ├── info.py              # /botstatus, /myinfo, /avatar, /ratelimits
│       ├── utility.py           # /hash, /transform, /encode (encode/decode/help subcommands), /diff, /entropy, /coinflip
│       ├── ciphers.py           # /cipher (encrypt/decrypt/help subcommands) -- classic cipher encode/decode + Identify
│       ├── encryption.py        # /encrypt, /decrypt -- modern authenticated encryption
│       ├── genpass.py           # /genpass -- password/passphrase generator, single or bulk (up to 10)
│       ├── moderation.py        # /ban, /checkban, /unban, /kick, /mute, /unmute, /purge, /ghostping, /dm, /slowmode, /togglelock, /togglelockdown
│       ├── whitelist.py         # /whitelist, /bulkwhitelist, /register, /editwhitelist, /edituser, /viewwhitelist, /fetchuser, /fetchdupes, /unwhitelist, /clearnotes, /clearregistrations, /checkregistration, /hwidhelp
│       ├── keys_hwid.py         # /genkey, /getkeys, /clearkeys, /validatekey, /tempwhitelist, /checktemp, /extend, /forceresethwid, /resethwidcooldown
│       ├── database.py          # /dbsearch, /export, /upload, /rollback, /commithistory, /fetchcommit, /verifydata
│       ├── panel.py             # /createpanel, /updatescript + the persistent ControlPanelView
│       ├── access.py            # /toggleaccess, /tempaccess, /togglealerts
│       ├── reaction_roles.py    # /reactionrole
│       └── context_menus.py     # the 15 right-click "user" context menu commands
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
