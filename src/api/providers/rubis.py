"""
api.providers.rubis -- async wrapper around Rubiš by Numelon
(https://rubis.app, interactive docs at https://rubis.app/playground/),
for /paste's `rubis` provider choice.

**Create endpoint's REQUEST shape (not just its response) is now
confirmed live too (2026-08-16), correcting this module's biggest wrong
guess:** POST /scrap does not parse a JSON envelope at all. Whatever
bytes are sent as the request body become the scrap's literal raw
content, verbatim, with zero server-side field parsing -- confirmed by
sending this module's old JSON body (`{"content": ..., "language": ...,
"public": ...}`) and then fetching the resulting scrap's own raw URL,
which came back containing that exact JSON string as its content. That
single finding explains two bugs at once: `language`/`title`/
`description` were never actually being applied (they were just more
inert text inside the blob, same as `public` below), and the `public`
field this module added expecting it to control visibility never did
anything either, for the identical reason -- it was never seen as a
field, just as literal characters. **So every paste this module has ever
created has both had corrupted content (its own JSON wrapper, not the
person's actual paste text) and been silently stuck on Rubiš's private
default, regardless of what `language`/`title`/`description`/`public`
this module tried to send, because none of it was ever read as
structured data by the server.**

The fix: `text` now goes straight through as the raw request body
(`Content-Type: text/plain`, no JSON wrapper), and `language`/`title`/
`description`/`visibility` metadata moved to query parameters -- both
now confirmed correct for `text`/`title`/`description`/`visibility`
(2026-08-16 live test: raw content came back exactly as sent, and
`public` finally took effect).

`language` needed a second correction, also confirmed 2026-08-16:
Rubiš has no per-language syntax highlighting at all. Its own Compose
page (rubis.app/compose/, confirmed by fetching it -- not JS-rendered
gibberish for this one page, unlike the playground) shows exactly three
content-type buttons: **Plain / Code / Markdown**. No Lua, no Python, no
per-language picker of any kind. `language="markdown"` appeared to work
in an earlier live test not because this module correctly forwarded an
arbitrary language string, but because "markdown" happens to BE one of
Rubiš's exactly-three valid values -- pure coincidence of overlap.
`language="lua"` isn't one of the three, so it silently fell back to
Rubiš's own default (plain), which is exactly what was observed. This
module now maps this bot's full language list (api/providers/
languages.py, shared across every /paste provider) down onto Rubiš's
three real buckets via _scrap_type() below, rather than forwarding
whatever string was picked -- see that function's docstring for the
mapping and, still unconfirmed, the exact three value-strings Rubiš
itself expects ("plain"/"code"/"markdown" is this module's best-faith
guess, informed by "markdown" being confirmed correct and the other two
being the visually obvious counterparts).

Create endpoint's response shape (confirmed live 2026-08 -- see
create_paste() below):
  POST /scrap returns (no "result" wrapper, no top-level id/key/slug/
  url/link field at all -- these don't exist and checking for them was
  this module's original wrong guess):
    {
      "accessKey": "...",   -- lets anyone with it view a private scrap
      "ownerKey": "...",    -- lets its holder modify/delete the scrap
                                (this module's deletion_url)
      "createdAt": <unix ts>, "modifiedAt": <unix ts>,
      "public": false,      -- confirmed still false even when this
                                module's old JSON body tried to set it
                                true -- see this docstring's opening
                                paragraph for why (the field was never
                                read as a field at all)
      "raw": "https://api.rubis.app/v2/scrap/<id>/raw",
      "raw_with_key": "https://api.rubis.app/v2/scrap/<id>/raw?..."
                             -- same as `raw` but with (almost certainly)
                                accessKey appended as a query param;
                                cut off mid-value the one time this was
                                seen live, so that param's exact name is
                                the one remaining unconfirmed detail
    }
  The scrap id itself only ever shows up embedded in `raw`'s path, so
  create_paste() below pulls it out of there rather than off a
  dedicated field.

A scrap's human-viewable page lives at
`https://rubis.app/view/?scrap=<id>` (confirmed from live example URLs
found in the wild) -- NOT `https://rubis.app/<id>`, which was this
module's earlier wrong guess for paste_url's fallback.

Still unconfirmed: the exact three value-strings Rubiš's `language`
query param expects (see _scrap_type() below), and the `raw_with_key`
query param name noted above. rubis.app/playground/ remains a
JS-rendered single-page app this module's author can't get real content
out of via text-only fetching, so both are still "confirmed correct only
insofar as nothing's contradicted them yet" rather than verified against
Rubiš's own docs -- exactly the same status the old (wrong) JSON-body
guess had right up until it was actually tested live.

registry.py doesn't mark this provider supports_access_key -- unlike
pastee_dev.py and pastey_gg.py, "access key" doesn't mean the same thing
for Rubiš that it does for those two. There, access_key overrides this
bot's own auth token for the call. Rubiš's own terms of service
(rubis.app/terms.html) confirm Rubiš has no accounts and no per-call auth
token at all -- its "access key" is the accessKey this module already
captures off the create response (see above), a per-scrap secret needed
to *view* a private scrap after the fact, not a credential this module
would send *to* create one. Wiring it into /paste's shared `access_key`
option (documented there as "use your own API key instead of this bot's
default") would be wrong for Rubiš specifically, so that flag stays
False.

An env var only earns a place in config.py if there's somewhere to
actually get a value for it, and there isn't one here. rubis.app has no
signup, no dashboard, no "generate an API key" flow of any kind --
its homepage (fetched 2026-08-16) lists "No authentication required!" as
a plan feature and its FAQ states outright "Rubiš is completely
account-free... just start posting scraps immediately," matching the
terms-of-service finding above. Even the paid Epique Plus tier is
"available upon request" (a bespoke arrangement, not a self-serve key).
So every create_paste() call here is anonymous, full stop -- there is no
optional credential to send.

registry.py does mark this provider supports_visibility, though: Rubiš's
own terms of service confirms scraps are private-by-default and can be
"marked public, making them accessible to anyone with the link" -- a
plain two-way public/private toggle, so this module has its own two-value
_VALID_VISIBILITIES below. Treat /paste against Rubiš as experimental until
someone runs it against a real account and this module gets corrected to
match reality -- the same way storage/test_ez_host_api.py did for
e-z.host, and the same way this exact docstring just got corrected once
already.

**This bot's own default deliberately overrides Rubiš's.** Left
unset, `visibility` used to mean "let Rubiš's own private-by-default
apply" (see create_paste()'s own docstring). That's no longer this
module's behavior: `public` is now always sent explicitly, defaulting to
"true" unless the caller passes `visibility="private"`, so a paste made
through this bot is shareable by link out of the box instead of quietly
landing private and needing its `accessKey` passed around too.
"""

from typing import Dict, Optional
from urllib.parse import urlparse

import aiohttp

from api.providers.errors import ProviderAPIError
from api.providers.util import describe_network_error as _describe_network_error
from api.providers.util import get_session as _get_session_shared

BASE_URL = "https://api.rubis.app/v2"

_TIMEOUT = aiohttp.ClientTimeout(total=15)


class RubisAPIError(ProviderAPIError):
    """Raised whenever a Rubiš call fails outright (non-2xx status,
    unreachable host, non-JSON body), OR whenever the response has no raw
    content URL at all (Rubiš's confirmed response shape's one load-
    bearing field -- see this module's docstring) -- the latter case
    explicitly names this module's still-unverified pieces (see this
    module's docstring) so a genuinely unrecognized response is legible
    rather than a bare KeyError."""


_VALID_VISIBILITIES = ("public", "private")


def _scrap_type(language: str) -> str:
    """Maps this bot's full /paste `language` list (api/providers/
    languages.py, shared across every provider) down onto Rubiš's own
    three real content types -- see this module's docstring for how
    that three-way Plain/Code/Markdown split was discovered on Rubiš's
    Compose page. "markdown"/"md" (case-insensitive) -> "markdown";
    "text"/"plaintext"/"plain"/unset -> "plain"; anything else --
    meaning every actual programming language this bot's autocomplete
    offers, Lua included -- collapses to "code", since Rubiš has no
    per-language syntax highlighting to select for those, only the one
    generic Code bucket. The three returned strings ("plain"/"code"/
    "markdown") are this module's best-faith guess at Rubiš's own value
    spelling -- see this module's docstring for why "markdown" alone is
    confirmed and the other two aren't yet."""
    normalized = language.strip().lower()
    if normalized in ("markdown", "md"):
        return "markdown"
    if normalized in ("text", "plaintext", "plain", "none", ""):
        return "plain"
    return "code"


async def _get_session(session: Optional[aiohttp.ClientSession]):
    return await _get_session_shared(session, _TIMEOUT)


async def create_paste(
    text: str,
    *,
    language: str = "text",
    title: Optional[str] = None,
    description: Optional[str] = None,
    visibility: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Optional[str]]:
    """
    Creates a "scrap" (Rubiš's term for a paste) on Rubiš -- see this
    module's docstring for what's confirmed vs. still guessed about the
    request/response shape.

    `text` goes straight through as the raw POST body (confirmed --
    Rubiš stores whatever bytes it receives verbatim as the scrap's
    content, no JSON parsing at all; see this module's docstring). Since
    the body is spoken for, `language`/`title`/`description`/
    `visibility` are sent as query parameters instead (confirmed correct
    for everything but `language`'s exact value spelling -- see this
    module's docstring).

    `language` is passed through _scrap_type() (see its own docstring)
    rather than sent as-is: Rubiš has no per-language syntax
    highlighting, only three content types (Plain/Code/Markdown, per its
    own Compose page), so this module's full language list collapses
    onto those three rather than forwarding e.g. "lua" or "python"
    straight through to a param that was never going to recognize them.

    `visibility` is one of _VALID_VISIBILITIES ("public" or "private") --
    registry.py's supports_visibility=True for this provider. This bot's
    own default deliberately overrides Rubiš's (see this module's
    docstring): `public` is always sent, defaulting to "true" whenever
    `visibility` is left unset -- only an explicit `visibility="private"`
    sends "false" and falls back to Rubiš's own private-by-default
    behavior. Rubiš only has the two states, so a value outside
    _VALID_VISIBILITIES raises RubisAPIError immediately rather than
    sending Rubiš something it was never going to accept.

    Returns {"paste_url": ..., "raw_url": ..., "deletion_url": ...} --
    `paste_url` is `https://rubis.app/view/?scrap=<id>` (this module's
    docstring), with an `accessKey` query param appended when the scrap
    came back private (either Rubiš's own default, or this module's
    `visibility="private"` explicitly asking for it); `raw_url` is
    whatever raw content URL Rubiš handed back (preferring
    `raw_with_key` over `raw` so a private scrap's content is actually
    fetchable without a separate key); `deletion_url` is the scrap's
    `ownerKey` (confirmed real field name), needed to modify/delete it
    later.

    Raises RubisAPIError on a non-2xx response, a non-JSON response, an
    unrecognized `visibility` value, or a response with no raw content
    URL at all -- see this module's docstring for what a genuinely
    unrecognized response shape now looks like in practice.
    """
    if visibility is not None and visibility not in _VALID_VISIBILITIES:
        raise RubisAPIError(
            f"Rubiš only has `public`/`private` visibility (got {visibility!r}) -- Leave `visibility` blank for public (this bot's own default), or pick `public`/`private`."
        )

    # Best-faith guess for the three value-strings (see _scrap_type()'s
    # docstring for why "markdown" alone is confirmed) -- the "language"
    # key itself and title/description/public are confirmed live. `public`
    # is now always sent (see this module's docstring for why this bot
    # deliberately overrides Rubiš's own private-by-default): "true"
    # unless `visibility` is explicitly "private", so leaving `visibility`
    # unset lands on public rather than silently inheriting Rubiš's
    # server-side default.
    params: Dict[str, str] = {
        "language": _scrap_type(language),
        "public": "false" if visibility == "private" else "true",
    }
    if title:
        params["title"] = title
    if description:
        params["description"] = description

    headers = {"Content-Type": "text/plain; charset=utf-8"}

    sess, should_close = await _get_session(session)
    try:
        try:
            async with sess.post(f"{BASE_URL}/scrap", headers=headers, params=params, data=text.encode("utf-8")) as resp:
                try:
                    data = await resp.json()
                except aiohttp.ContentTypeError:
                    raise RubisAPIError(f"Rubiš returned HTTP {resp.status} with a non-JSON body.")
                if resp.status not in (200, 201):
                    raise RubisAPIError(
                        data.get("error") or data.get("message") or f"Rubiš returned HTTP {resp.status}."
                    )
        except (aiohttp.ClientError, TimeoutError) as e:
            raise RubisAPIError(f"Couldn't reach Rubiš: {_describe_network_error(e, _TIMEOUT)}")
    finally:
        if should_close:
            await sess.close()

    # Confirmed real response shape (from this exact call, previously
    # unverified -- see this module's docstring): no top-level id/key/
    # slug/url/link field at all. The scrap id only ever shows up
    # embedded in `raw`'s path (".../scrap/<id>/raw"), so it has to be
    # pulled out of there rather than read off a dedicated field.
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    raw_url = result.get("raw_with_key") or result.get("raw")
    access_key = result.get("accessKey")
    owner_key = result.get("ownerKey")
    is_public = result.get("public")

    scrap_id = None
    if raw_url:
        segments = urlparse(raw_url).path.strip("/").split("/")
        if "scrap" in segments:
            idx = segments.index("scrap")
            if idx + 1 < len(segments):
                scrap_id = segments[idx + 1]

    if not raw_url:
        # Include what Rubiš actually sent back (truncated) rather than
        # just saying "didn't recognize it" -- the whole point of this
        # module being unverified is that nobody's confirmed the real
        # field names until they hit exactly this failure, so the error
        # needs to carry enough to fix it, the same way is_gd.py's
        # non-JSON-response error now surfaces its own raw body instead
        # of discarding it.
        raise RubisAPIError(
            "Rubiš's response didn't contain anything this module recognizes as a scrap id or URL -- "
            "this integration is unverified (see api/providers/rubis.py's docstring) and likely needs "
            f"updating to match Rubiš's real response shape. Response was: {str(data)[:500]!r}"
        )

    # Confirmed pattern (see this module's docstring) -- NOT
    # https://rubis.app/{scrap_id}, which was this module's earlier
    # unconfirmed guess.
    paste_url = f"https://rubis.app/view/?scrap={scrap_id}" if scrap_id else raw_url

    # Keys off the response's own `public` field rather than the
    # `visibility` argument above, so this still does the right thing
    # whether the scrap ended up private via an explicit
    # `visibility="private"` or (when `visibility` was left unset) via
    # Rubiš's own default -- either way a bare paste_url won't actually
    # show anything to whoever it's shared with unless the access key
    # rides along. Query param name (`accessKey`) is inferred from
    # matching `raw_with_key`'s own field naming convention, not
    # independently confirmed -- `raw_with_key` got cut off mid-value the
    # one time this was seen live, so this specific param name is the
    # remaining unverified piece here.
    if is_public is False and access_key and scrap_id:
        paste_url += f"&accessKey={access_key}"

    return {
        "paste_url": paste_url,
        "raw_url": raw_url,
        # ownerKey (confirmed real field name) is what Rubiš actually
        # requires to modify/delete this scrap later -- handed back once,
        # at creation, same "capture immediately or it's gone" situation
        # every other provider's deletion_url in this package is in, even
        # though this module doesn't implement a delete/update call that
        # uses it yet.
        "deletion_url": owner_key,
    }