"""
api.providers.errors -- the one thing every provider module in this package
shares: a common exception base.

Each provider still raises its own named exception (EZHostAPIError,
TinyURLAPIError, CatboxAPIError, ...) so error messages and any
provider-specific attributes (like EZHostAPIError.status) stay intact and
call sites that care about one specific provider can still catch narrowly.
But commands/url.py's impl functions dispatch through
api/providers/registry.py to whichever provider the caller picked, so they
need one exception type to catch generically without an if/elif per
provider -- that's what ProviderAPIError is for.
"""


class ProviderAPIError(Exception):
    """Base class for every provider module's own `<Provider>APIError`.

    `str(error)` is always meant to already be a user-presentable message
    (see each provider module's own exception class for specifics), so a
    generic call site can do:

        try:
            result = await provider_info["module"].shorten_url(url)
        except ProviderAPIError as e:
            return await send_error(interaction, f"Failed to shorten that link: {e}")

    without needing to know which provider actually raised it.
    """
