"""
providers package -- one module per third-party file-hosting/URL-shortening
service (e-z.host today, more later). Each module owns its own HTTP client
code and its own *APIError exception; the shared persistence layer
(storage/shortened-urls.json) lives in api/github.py instead, since that
file is written to by every provider alike.
"""