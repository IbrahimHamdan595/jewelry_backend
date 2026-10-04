import ipaddress

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.auth_audit import get_client_ip


def client_ip_key(request: Request) -> str:
    """Rate-limit bucket for a request: the visitor's address, not the proxy's.

    slowapi's stock `get_remote_address` is `request.client.host`. Behind
    Render's load balancer that is the same internal address for every
    visitor, so "5/minute" on /auth/login was ONE bucket shared by the whole
    internet (NEX-47). `get_client_ip` already takes the leftmost
    X-Forwarded-For entry — the original client; the rightmost is the proxy —
    so it is reused here instead of parsing the header a second time.

    Not done with uvicorn's `--proxy-headers`: that lives in the start
    command, which we do not control on every host.

    TRUST: the leftmost entry is only as honest as the ingress in front of us.
    It is safe to key on ONLY because Render's ingress overwrites
    X-Forwarded-For — the argument `get_client_ip` spells out, and one that
    must be re-checked for any other host. Where a client-supplied header is
    passed through instead, a caller can name a fresh bucket on every request
    and this limit stops nothing.

    Anything that is not a single well-formed IP (empty, "unknown", an
    address with a port, garbage) falls back to the socket address: a
    malformed header must never mint its own bucket. Addresses are returned
    in canonical form so one IPv6 address cannot be spelled into several.
    """
    claimed = get_client_ip(request)
    if claimed:
        try:
            return str(ipaddress.ip_address(claimed))
        except ValueError:
            pass
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip_key)
