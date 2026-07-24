# Discord-Bot

A Discord bot backed by a GitHub-hosted `Users` whitelist database, with
key generation/redemption, HWID locking, moderation, reaction roles,
password/passphrase and classic-cipher tools, string entropy analysis, and a
persistent control panel. Originally a single ~4,100-line `main.py` +
~1,600-line `bot_api.py`, refactored into this package -- and extended since
with newer commands (classic ciphers, `/entropy`, bulk `/genpass`, and more).
70 commands total today (55 slash commands + 15 user context menus).

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
│   │   ├── encoding.py          # /encode & /decode's algorithms + Identify heuristic
│   │   ├── ciphers.py           # /cipher & /decipher's classical cipher algorithms (Caesar, Vigenère, Playfair, Atbash, Rail Fence, Columnar Transposition, Baconian, Pigpen, Polybius Square, Simple Substitution)
│   │   ├── entropy.py           # /entropy password/string strength + crack-time estimation
│   │   ├── passwords.py         # /genpass generation logic (random + EFF-wordlist passphrase)
│   │   ├── data/                # bundled data for /genpass's Passphrase mode
│   │   │   ├── README.md        # source/license note (CC BY 3.0, EFF) for the wordlist below
│   │   │   └── eff_large_wordlist.txt  # EFF Long Wordlist (7,776 entries) for Diceware-style passphrases
│   │   └── discord_helpers.py   # embeds, interaction responders, permission checks, shared Components V2 layouts
│   └── commands/                # one cog per file -- each is a discord.py extension
│       ├── __init__.py          # empty -- just makes this a package
│       ├── info.py              # /botstatus, /myinfo
│       ├── utility.py           # /hash, /transform, /encode, /decode, /diff, /entropy
│       ├── ciphers.py           # /cipher, /decipher -- classic cipher encode/decode
│       ├── genpass.py           # /genpass -- password/passphrase generator, single or bulk (up to 10)
│       ├── moderation.py        # /ban, /checkban, /unban, /kick, /mute, /unmute, /purge, /ghostping, /dm
│       ├── whitelist.py         # /whitelist, /register, /editwhitelist, /edituser, /viewwhitelist, /fetchuser, /fetchdupes, /unwhitelist, /clearnotes, /clearregistrations, /checkregistration, /hwidhelp
│       ├── keys_hwid.py         # /genkey, /getkeys, /clearkeys, /validatekey, /tempwhitelist, /checktemp, /extend, /forceresethwid, /resethwidcooldown
│       ├── database.py          # /dbsearch, /export, /upload, /rollback, /commithistory, /fetchcommit, /verifydata
│       ├── panel.py             # /createpanel, /updatescript + the persistent ControlPanelView
│       ├── access.py            # /toggleaccess, /tempaccess, /togglelock, /togglelockdown
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
