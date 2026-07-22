# Discord-Bot

A Discord bot backed by a GitHub-hosted `Users.json` whitelist database, with
key generation/redemption, HWID locking, moderation, reaction roles, and a
persistent control panel. Originally a single ~4,100-line `main.py` +
~1,600-line `bot_api.py`; this tree is that same bot split into a proper
package, with all 62 commands (47 slash commands + 15 context
menus) preserved.

## Layout

```
Discord-Bot/
├── src/
│   ├── start.py              # entry point
│   ├── keep_alive.py          # tiny Flask server so host platforms see an open port
│   ├── api/                   # shared library code (no Discord commands live here)
│   │   ├── config.py          # env-driven constants (Discord IDs, GitHub repo, secrets)
│   │   ├── github.py          # GitHub Contents API + Users cache, valid keys, stored script
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
├── storage/                    # permittedKeys.txt, storedscript.lua, test scripts for createpanel
├── .env.example
├── requirements.txt
└── README.md
```

`api/` and `commands/` are plain top-level packages (not namespaced under
`src`) -- `start.py` puts `src/` on `sys.path` itself, so `from api import
config` and `from commands.whitelist import WhitelistModal` work the same
way from any cog without needing `src` to be a package itself.