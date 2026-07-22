# Discord-Bot

A Discord bot backed by a GitHub-hosted `Users.json` whitelist database, with
key generation/redemption, HWID locking, moderation, reaction roles, and a
persistent control panel. Originally a single ~4,100-line `main.py` +
~1,600-line `bot_api.py`; this tree is that same bot split into a proper
package by concern, with all 62 commands (47 slash commands + 15 context
menus) preserved.

## Layout

```
Discord-Bot/
├── src/
│   ├── start.py              # entry point -- run this
│   ├── keep_alive.py          # tiny Flask server so host platforms see an open port
│   ├── api/                   # shared library code (no Discord commands live here)
│   │   ├── config.py          # env-driven constants (Discord IDs, GitHub repo, secrets)
│   │   ├── github.py          # GitHub Contents API + Users.json cache, permitted keys, stored script
│   │   ├── users.py           # user-record lookups/building + buyer role revocation
│   │   ├── keys.py            # key generation + input validation
│   │   ├── time_utils.py      # date formatting/parsing + temp-whitelist expiration
│   │   ├── hashing.py         # /hash algorithm utilities
│   │   ├── transforms.py      # /transform's stylized-Unicode text styles
│   │   └── discord_helpers.py # embeds, interaction responders, permission checks
│   └── commands/              # one cog per file -- each is a discord.py extension
│       ├── info.py            # /ping, /myinfo
│       ├── utility.py         # /hash, /transform
│       ├── moderation.py      # /ban, /kick, /mute, /unmute, /unban, /purge, /ghostping, /dm
│       ├── whitelist.py       # /whitelist, /edituser, /unwhitelist, /viewwhitelist, /fetchuser, /clearnotes, ...
│       ├── keys_hwid.py       # /genkey, /getkeys, /validatekey, /tempwhitelist, /forceresethwid, ...
│       ├── database.py        # /dbsearch, /export, /upload, /rollback, /commithistory, /fetchcommit, /fetchdupes, /verifydata
│       ├── panel.py           # /createpanel, /updatescript + the persistent ControlPanelView
│       ├── access.py          # /toggleaccess, /tempaccess, /togglelock, /togglelockdown
│       ├── reaction_roles.py  # /reactionrole
│       └── context_menus.py   # the 15 right-click "user" context menu commands
├── storage/                    # permittedKeys.txt, storedscript.lua, test scripts
├── .env.example                 # template -- copy to .env and fill in
├── .env                         # your real secrets (gitignored, not committed)
├── requirements.txt
└── README.md
```

`api/` and `commands/` are plain top-level packages (not namespaced under
`src`) -- `start.py` puts `src/` on `sys.path` itself, so `from api import
config` and `from commands.whitelist import WhitelistModal` work the same
way from any cog without needing `src` to be a package itself.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in every value in .env
python src/start.py
```

`.env` needs:

- `DISCORD_TOKEN`, `GITHUB_TOKEN` -- secrets
- `GUILD_ID`, `REQUIRED_ROLE_ID`, `REGISTRATION_CHANNEL_ID`,
  `REACTION_ROLE_CHANNEL_ID`, `PANEL_CHANNEL_ID`, `BUYER_ROLE_ID`,
  `REDEEM_ALERTS_CHANNEL_ID` -- Discord IDs
- `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_BRANCH` -- where `Users.json` lives
- `GITHUB_STORAGE_REPO`, `GITHUB_STORAGE_BRANCH` -- where `storage/permittedKeys.txt`
  and `storage/storedscript.lua` live (defaults to this same repo, on `main`)

`src/start.py` will raise a clear `ValueError` on startup if anything
required is missing, rather than failing later on first use.

## A platform limit worth knowing about

Discord caps **USER**-type context menu commands at 5 per guild. All 15
context menus in `commands/context_menus.py` are guild-scoped USER commands,
so only the first 5 (`Ban User`, `Kick User`, `Mute User`, `Unmute User`,
`Whitelist User`) actually register -- the rest fail with
`CommandLimitReached` and are logged, not crash the bot. All 62 commands
still exist and every slash-command equivalent still works; this only
affects which ones get a right-click shortcut. Fixing it needs a product
decision (which 5 stay, whether some go global instead of guild-scoped for
5 more slots, or whether several get folded into one menu with a follow-up
select) -- see the comment in `context_menus.py`'s `setup()` for details.

## One thing flagged, not changed

The original `bot_api.py` had a `get_hwid()` function (reads the *host
machine's* own hardware UUID via Windows' `wmic`) that had zero call sites
anywhere in `main.py` or `bot_api.py`, and wouldn't run on a non-Windows
host anyway. It looks like dead/vestigial code rather than something that
was dropped by accident, so it wasn't carried over -- flagging it here in
case that assumption is wrong and it's needed somewhere after all.
