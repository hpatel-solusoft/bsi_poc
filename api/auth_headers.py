"""
Owns: extracting and validating the two values every route needs from
the CALLER but that must never appear in a request body or a URL query
string — the investigator's identity (username) and their AppWorks
SAMLart (token).

Does not own: what a route DOES with username/token once extracted —
that's each route's own business (attribution writes, threading token
to appworks_auth via execution_context). This module's only job is
getting the two values out of the request and rejecting a blank one,
exactly the job api/models.py's AuthFieldsMixin used to do for the
request body before this change.

WHY HEADERS, NOT BODY OR QUERY STRING:
  - A JSON body shows up verbatim in browser dev-tools network tabs, in
    Swagger/OpenAPI's request-body editor (easy to copy-paste and share
    by accident — this is exactly how a live SAMLart ended up pasted
    into a chat transcript during testing of this contract's first
    version), and in some proxies'/APM tools' request-body logging.
  - A query string is worse by default: it is written to virtually
    every web server, reverse proxy, and load balancer's ACCESS LOG
    automatically, and persists in browser history.
  - HTTP headers are the conventional, narrower channel for per-call
    caller-identity/credential data, and `Authorization` specifically
    gets default-on redaction in most logging/APM/tracing tooling
    (nginx/Apache access logs never include header values by default;
    Datadog, ELK, and most API gateways auto-scrub `Authorization` by
    name) precisely because it's the one header name every tool already
    recognizes. A custom body or query field gets no such protection
    unless someone remembers to configure it — headers are the
    conventional prod-grade choice specifically because the tooling
    ecosystem already assumes credentials live there.

SHAPE:
  - `Authorization: Bearer <SAMLart>` — the token, via FastAPI's
    standard HTTPBearer security scheme. This also renders as a proper
    "Authorize" lock icon in /docs instead of a raw editable field,
    which is what let the token get pasted in plain sight last time.
  - `X-BSI-Username: <username>` — a custom, namespaced header for the
    caller's identity. There's no HTTP-standard header for a bare
    username outside Basic Auth, which bundles a password we don't
    have — AppWorks already authenticated the caller via SAML.

Neither dependency touches appworks/ — get_token returns the raw
string; api/server.py and api/services/*.py are the ones that later
hand it to agent_service.agent_runner via execution_context, exactly as
before this change. This module stays pure API-layer, same as
api/models.py always was.
"""

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(
    scheme_name="AppWorksSAMLart",
    description="The caller's current AppWorks SAMLart (SAML artifact), as a Bearer credential.",
    auto_error=True,  # FastAPI itself returns 403 if the header is missing entirely
)


def get_username(
    x_bsi_username: str = Header(
        ...,
        alias="X-BSI-Username",
        description=(
            "Investigator identity. Used ONLY for attribution (audit logs, DB rows, "
            "and the agent_summary_cache entry each generating call writes) — never "
            "for authorization. AppWorks access is governed entirely by the "
            "Authorization token, not by this value."
        ),
    ),
) -> str:
    """
    FastAPI dependency: the X-BSI-Username header, required and
    non-blank. FastAPI itself returns 422 if the header is missing
    entirely (it's a required Header(...)); the check here catches the
    remaining case FastAPI's own presence check doesn't — a header sent
    but blank or whitespace-only.
    """
    if not x_bsi_username.strip():
        raise HTTPException(status_code=422, detail="X-BSI-Username header must be a non-empty string.")
    return x_bsi_username


def get_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """
    FastAPI dependency: the Authorization: Bearer <token> header,
    required and non-blank. HTTPBearer(auto_error=True) already returns
    403 if the header is missing/malformed (not "Bearer <value>" shape);
    the check here catches a Bearer header present but carrying a blank
    token.
    """
    token = credentials.credentials
    if not token or not token.strip():
        raise HTTPException(status_code=422, detail="Authorization: Bearer token must be a non-empty string.")
    return token
