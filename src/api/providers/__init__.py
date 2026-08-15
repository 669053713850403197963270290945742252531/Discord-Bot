"""
api.providers -- thin async wrappers around third-party link/file/paste
hosts (e-z.host today; whatever gets added alongside it later). Each
provider gets its own module here (its own base URL, auth header shape,
and response quirks), kept separate from api/github.py's persistence layer
-- these modules only know how to talk to their provider's HTTP API, not
how or where the result gets stored.
"""