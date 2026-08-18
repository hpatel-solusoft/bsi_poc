# semantic_layer/appworks_auth.py
# ----------------------------------------------------------------
# AppWorks Gateway & Authentication (OTDS + SAML Flow)
# ----------------------------------------------------------------

import contextvars
import logging
import os
import re
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# NOTE: no logging.basicConfig() here. This is a leaf/library module
# (imported transitively by dispatcher.py, appworks_utils.py, every
# appworks/*.py feature module) — it only ever gets a logger, never
# configures one. logging.basicConfig() configures the process-wide
# ROOT logger and only takes effect on its first call; a library module
# calling it means whichever module happens to import first silently
# decides the log format for the entire application. That's exactly
# what a bare, unguarded call here used to do — see api/server.py's own
# logging.basicConfig() call (the application's actual entry point,
# and the only place in this codebase that should ever call it for a
# running service) for the real configuration and the full explanation.
logger = logging.getLogger(__name__)

OTDS_URL = os.getenv("OTDS_URL")
SOAP_URL = os.getenv("SOAP_GATEWAY_URL")
REST_URL = os.getenv("APPWORKS_URL")  # e.g. http://host:81/.../OSABSIACM
USER = os.getenv("APPWORKS_USER")
PASS = os.getenv("APPWORKS_PASS")

_SAML_TOKEN: Optional[str] = None


class AppworksSessionExpiredError(ConnectionError):
    """
    Raised when an AppWorks call made with a CALLER-supplied SAMLart
    token (see set_request_token below) is rejected with HTTP 401.

    Deliberately never retried and never falls back to perform_login():
    a caller's own AppWorks session expiring is not this service's
    credential to refresh — silently retrying under the service
    account's identity would complete the investigator's request as
    someone else. api/server.py catches this specifically and returns
    HTTP 401, so the frontend can prompt the investigator to
    re-authenticate with AppWorks and retry, instead of showing a
    generic 500.
    """


# ------------------------------------------------------------------
# Per-request AppWorks identity
# ------------------------------------------------------------------
# Every request that can trigger an AppWorks call now carries the
# CALLING investigator's own AppWorks SAMLart token on its body
# (AuthFieldsMixin.token — see api/models.py). api/server.py calls
# set_request_token(req.token) as the first line of every POST route
# handler, before any tool call can run. fetch()/fetch_list() below
# read it from here and use it AS-IS for that call — they never derive,
# extend, or cache a caller-supplied token, and never fall back to the
# service-account login (perform_login) while one is present for this
# request.
#
# A ContextVar (not a plain module global, which is what _SAML_TOKEN
# above still is) is what makes this both request-scoped and
# concurrency-safe: every route in this codebase is a sync `def`, and
# Starlette runs sync route functions via anyio.to_thread.run_sync,
# which copies the CALLING async task's context into the thread for
# that one call. Each HTTP request is its own asyncio task with its
# own context, so two investigators' requests running concurrently can
# never see each other's token, and nothing here needs an explicit
# reset between requests — a later call to set_request_token from a
# different request cannot leak into or overwrite this one's context.
#
# perform_login()'s username/password flow is NOT removed — it remains
# the fallback for calls made with no request-scoped token at all,
# which today means exactly one path: etl/run_sync.py's CLI/batch
# ingest, which has no investigator session to borrow a token from.
# Every live, investigator-triggered call always has a token by the
# time it reaches fetch()/fetch_list() (AuthFieldsMixin makes `token` a
# required field on every request model), so in practice the
# service-account fallback below is only ever exercised from the CLI.
_REQUEST_TOKEN: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "appworks_request_token", default=None
)


def set_request_token(token: Optional[str]) -> None:
    """
    Record the calling investigator's AppWorks SAMLart token for every
    fetch()/fetch_list() call made for the remainder of THIS request.

    Call this once, as the first line of an API route handler that can
    reach an AppWorks call — see api/server.py. Safe to call more than
    once for the same request (e.g. /reload_all re-entering /intake,
    /similar_cases, etc. as plain Python calls) — later calls with the
    same token are a no-op in effect.
    """
    _REQUEST_TOKEN.set(token)


def get_request_token() -> Optional[str]:
    """
    The current request's caller-supplied SAMLart token, or None if none
    is set for this call stack (e.g. the CLI/batch ingest path, or any
    code running outside an HTTP request entirely).
    """
    return _REQUEST_TOKEN.get()


def clear_request_token() -> None:
    """
    Reset to "no caller token for this context". Not required for
    cross-request isolation (see the ContextVar note above) — provided
    for call sites that want to be explicit about scoping (e.g. a
    finally block), and for tests that reuse a single process/thread
    across multiple simulated "requests".
    """
    _REQUEST_TOKEN.set(None)


def perform_login() -> bool:
    """
    Authenticate as the AppWorks SERVICE ACCOUNT (OTDS ticket -> SAML
    artifact, via APPWORKS_USER/APPWORKS_PASS) and cache the resulting
    SAML token in the module-level _SAML_TOKEN.

    This is the FALLBACK identity only. fetch()/fetch_list() call this
    solely when get_request_token() returns None — i.e. there is no
    caller-supplied SAMLart token for the current request/call stack.
    In production that is exactly the CLI/batch ingest path
    (etl/run_sync.py); every live, investigator-triggered request
    already carries its own token (see the ContextVar note above) and
    never reaches this function.
    """
    global _SAML_TOKEN

    if not all([OTDS_URL, SOAP_URL, REST_URL, USER, PASS]):
        logger.error("Missing required environment variables in .env")
        return False

    try:
        logger.info("[Auth] Requesting OTDS Ticket: %s", OTDS_URL)
        otds_resp = requests.post(OTDS_URL, json={"userName": USER, "password": PASS}, timeout=15)
        otds_resp.raise_for_status()
        ticket = otds_resp.json().get("ticket")
        if not ticket:
            logger.error("OTDS response missing 'ticket'.")
            return False

        logger.info("[Auth] Requesting SAML Artifact via SOAP")
        soap_envelope = f"""<SOAP:Envelope xmlns:SOAP="http://schemas.xmlsoap.org/soap/envelope/">
            <SOAP:Header>
                <OTAuthentication xmlns="urn:api.bpm.opentext.com">
                    <AuthenticationToken>{ticket}</AuthenticationToken>
                </OTAuthentication>
            </SOAP:Header>
            <SOAP:Body>
                <samlp:Request xmlns:samlp="urn:oasis:names:tc:SAML:1.0:protocol" MajorVersion="1" MinorVersion="1"
                               IssueInstant="{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                               RequestID="auth-{int(datetime.now().timestamp())}">
                    <samlp:AuthenticationQuery>
                        <saml:Subject xmlns:saml="urn:oasis:names:tc:SAML:1.0:assertion">
                            <saml:NameIdentifier Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"/>
                        </saml:Subject>
                    </samlp:AuthenticationQuery>
                </samlp:Request>
            </SOAP:Body>
        </SOAP:Envelope>"""

        soap_resp = requests.post(
            SOAP_URL, headers={"Content-Type": "text/xml"}, data=soap_envelope, timeout=15
        )
        soap_resp.raise_for_status()

        match = re.search(
            r"<[^>]*?AssertionArtifact[^>]*?>(.*?)</[^>]*?AssertionArtifact>", soap_resp.text, re.DOTALL
        )
        if not match:
            logger.error("SAML AssertionArtifact not found in SOAP response.")
            return False

        _SAML_TOKEN = match.group(1).strip()
        logger.info("AppWorks Authentication Successful.")
        return True

    except Exception as e:
        logger.error("Authentication Failure: %s", e)
        return False


def _build_url(endpoint: str) -> str:
    """
    Build the full REST URL for an AppWorks endpoint.

    Handles two namespace cases:
      • Same namespace (OSABSIACM):
          REST_URL = http://host/...api/OSABSIACM
          endpoint = /OSABSIACM/entities/... → strip leading namespace, append rest
          endpoint = /entities/...           → append directly

      • Cross-namespace (SolusoftACMConfig):
          endpoint = /SolusoftACMConfig/entities/...
          The base URL must have OSABSIACM replaced with SolusoftACMConfig.
          e.g. http://host/.../api/SolusoftACMConfig/entities/EntityType/items/1

    This fixes the bug where SolusoftACMConfig endpoints were being
    appended TO the OSABSIACM base, producing invalid double-namespace URLs.
    """
    clean = endpoint.lstrip("/")

    if REST_URL is None:
        raise RuntimeError("APPWORKS_URL environment variable is not configured")

    # Derive the base without the last namespace segment
    # REST_URL: http://host/.../api/OSABSIACM
    rest_base = REST_URL.rstrip("/")
    # api_root: http://host/.../api
    api_root = rest_base.rsplit("/", 1)[0]
    # primary_ns: OSABSIACM
    primary_ns = rest_base.rsplit("/", 1)[-1]

    # Detect which namespace the endpoint belongs to
    endpoint_ns = clean.split("/")[0]

    if clean.startswith("entityRestService/api/"):
        # Endpoint already includes the raw AppWorks REST service prefix.
        # Rebuild it relative to the app root so we don't duplicate namespaces.
        app_root = rest_base
        for suffix in (
            f"/entityRestService/api/{primary_ns}",
            "/entityRestService/api",
            "/entityRestService",
        ):
            if app_root.endswith(suffix):
                app_root = app_root[: -len(suffix)]
                break
        return f"{app_root}/{clean}"
    if endpoint_ns == primary_ns:
        # Strip leading namespace, append to REST_URL
        path_after_ns = clean[len(primary_ns) :].lstrip("/")
        return f"{rest_base}/{path_after_ns}"
    elif "/" in clean and not clean.startswith("entities"):
        # Cross-namespace: e.g. SolusoftACMConfig/entities/...
        # Replace primary_ns with the endpoint's namespace in the base
        return f"{api_root}/{clean}"
    else:
        # No namespace prefix — append directly to REST_URL
        return f"{rest_base}/{clean}"


def _build_list_url(endpoint: str) -> str:
    """
    Build the full URL for AppWorks LIST (search/filter) endpoints.

    The AppWorks platform exposes two distinct HTTP services:
      - entityRestService/api/  — used for items, relationships, childEntities
      - entityservice/          — used for /lists/ filtered queries

    The API guide confirms list queries use entityservice:
      http://host:81/home/BSIDev/app/entityservice/OSABSIACM/entities/Allegations/lists/Allegations_All?...

    REST_URL   = http://host:81/home/BSIDev/app/entityRestService/api/OSABSIACM
    list base  = http://host:81/home/BSIDev/app/entityservice/OSABSIACM
    """
    clean = endpoint.lstrip("/")

    if REST_URL is None:
        raise RuntimeError("APPWORKS_URL environment variable is not configured")

    rest_base = REST_URL.rstrip("/")
    primary_ns = rest_base.rsplit("/", 1)[-1]

    # Derive the app root: http://host:81/home/BSIDev/app
    # REST_URL structure: <app_root>/entityRestService/api/<ns>
    # Strip "/entityRestService/api/<ns>" to get app_root
    app_root = rest_base
    for suffix in (f"/entityRestService/api/{primary_ns}", "/entityRestService/api", "/entityRestService"):
        if app_root.endswith(suffix):
            app_root = app_root[: -len(suffix)]
            break

    list_base = f"{app_root}/entityservice/{primary_ns}"

    # endpoint may or may not be prefixed with the namespace
    endpoint_ns = clean.split("/")[0]
    if clean.startswith("entityRestService/api/"):
        app_root = rest_base
        for suffix in (
            f"/entityRestService/api/{primary_ns}",
            "/entityRestService/api",
            "/entityRestService",
        ):
            if app_root.endswith(suffix):
                app_root = app_root[: -len(suffix)]
                break
        return f"{app_root}/{clean}"
    if endpoint_ns == primary_ns:
        path_after_ns = clean[len(primary_ns) :].lstrip("/")
        return f"{list_base}/{path_after_ns}"
    else:
        return f"{list_base}/{clean}"


def fetch_list(endpoint: str, params: Optional[dict] = None, _retry: bool = True) -> dict:
    """
    Fetch an AppWorks LIST endpoint (entityservice path).

    Use this for any /lists/ query — e.g.:
        /entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id=114689

    The entityservice base is derived automatically from APPWORKS_URL.

    Token resolution (see the ContextVar note above perform_login):
      1. A caller-supplied token from set_request_token(), if one is set
         for this request — used as-is, never cached, never retried on
         401 (see AppworksSessionExpiredError).
      2. Otherwise the service-account token from perform_login(),
         lazily fetched and cached in _SAML_TOKEN, with the original
         retry-once-on-401 behaviour unchanged.
    """
    global _SAML_TOKEN

    caller_token = get_request_token()
    if caller_token:
        token = caller_token
    else:
        if not _SAML_TOKEN:
            logger.info("[Auth] No caller token and no cached service-account token; performing lazy login.")
            if not perform_login():
                raise ConnectionError("Unauthorized: AppWorks login failed.")
        token = _SAML_TOKEN

    url = _build_list_url(endpoint)

    # SAMLart goes in the header ONLY — see fetch()'s matching comment
    # for why (confirmed against a live Postman request against this
    # exact endpoint: header-only succeeds, header+query-param 404s).
    # `params` here is any actual filter/query params the caller passed
    # in — SAMLart is deliberately NOT added to them.
    q_params = dict(params) if params else None

    headers = {"SAMLart": token, "Accept": "application/json"}

    try:
        logger.info("[REST-LIST] GET %s", url)
        resp = requests.get(url, params=q_params, headers=headers, timeout=20)

        if resp.status_code == 401:
            if caller_token:
                logger.warning("AppWorks rejected the caller-supplied SAMLart token (401) for %s", url)
                raise AppworksSessionExpiredError(
                    "AppWorks rejected the current SAMLart token (401). Re-authenticate "
                    "with AppWorks and retry with a fresh token."
                )
            if _retry:
                logger.warning("Service-account session expired (401). Retrying authentication...")
                _SAML_TOKEN = None
                return fetch_list(endpoint, params=params, _retry=False)
            raise ConnectionError("Unauthorized: AppWorks service-account session expired and relogin failed.")

        if resp.status_code == 404:
            if caller_token:
                # See fetch()'s matching comment: with a caller-supplied
                # token, 404 is ambiguous between "no such record" and
                # "this token is stale/expired/out of scope".
                logger.warning(
                    "AppWorks returned 404 for %s using a caller-supplied token. "
                    "This may mean no matching records exist, OR that the token is "
                    "stale/expired/out of scope for this query — AppWorks does not "
                    "distinguish the two at this endpoint. If results were expected, "
                    "get a fresh SAMLart and retry before assuming there is no data.",
                    url,
                )
            return {}

        resp.raise_for_status()
        return resp.json()

    except AppworksSessionExpiredError:
        raise
    except Exception as e:
        logger.error("REST-LIST Request Failed [%s]: %s", endpoint, e)
        raise ConnectionError(f"AppWorks List API Error: {str(e)}")


def fetch(
    endpoint: str,
    method: str = "GET",
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
    _retry: bool = True,
) -> dict:
    """
    General purpose AppWorks REST fetcher.
    Auto-builds URLs, handles SAML injection, and retries 401s once.

    Token resolution (see the ContextVar note above perform_login):
      1. A caller-supplied token from set_request_token(), if one is set
         for this request — used as-is, never cached, never retried on
         401 (see AppworksSessionExpiredError).
      2. Otherwise the service-account token from perform_login(),
         lazily fetched and cached in _SAML_TOKEN, with the original
         retry-once-on-401 behaviour unchanged.
    """
    global _SAML_TOKEN

    caller_token = get_request_token()
    if caller_token:
        token = caller_token
    else:
        if not _SAML_TOKEN:
            logger.info("[Auth] No caller token and no cached service-account token; performing lazy login.")
            if not perform_login():
                raise ConnectionError("Unauthorized: AppWorks login failed.")
        token = _SAML_TOKEN

    # Guard: prevent list-based IDs
    if "items/[" in endpoint or "items/%5B" in endpoint:
        logger.error("Invalid API call detected: %s", endpoint)
        raise ValueError(
            "AppWorks REST API /items/{id} does not support list-based IDs. "
            "Use /lists/ endpoints for filtering and searching."
        )

    url = _build_url(endpoint)

    # SAMLart goes in the header ONLY. It used to also be merged into
    # q_params (i.e. sent a second time as a query-string value) — that
    # is what a live side-by-side test caught: the exact same token,
    # against this exact endpoint, succeeds (200, real data) via Postman
    # sending it as a header alone, and 404s through this function
    # sending it as both a header AND a query param. The SAML artifact
    # contains '+', '/', and '=' — characters that are correctly
    # percent-encoded by `requests` itself, but a class of enterprise
    # Java gateways in front of AppWorks (WebSphere/WebLogic/Tomcat) are
    # known to mis-decode them when they land in a query-string value,
    # which can manifest as a silent 404 rather than a 401/403. Header
    # delivery has no such ambiguity. `params` here is any actual
    # filter/query params the caller passed in — SAMLart is deliberately
    # NOT added to them.
    q_params = dict(params) if params else None

    headers = {"SAMLart": token, "Content-Type": "application/json", "Accept": "application/json"}

    try:
        logger.info("[REST] %s %s", method, url)
        resp = requests.request(method, url, params=q_params, headers=headers, json=payload, timeout=20)

        if resp.status_code == 401:
            if caller_token:
                logger.warning("AppWorks rejected the caller-supplied SAMLart token (401) for %s %s", method, url)
                raise AppworksSessionExpiredError(
                    "AppWorks rejected the current SAMLart token (401). Re-authenticate "
                    "with AppWorks and retry with a fresh token."
                )
            if _retry:
                logger.warning("Service-account session expired (401). Retrying authentication...")
                _SAML_TOKEN = None
                return fetch(endpoint, method, params=params, payload=payload, _retry=False)
            raise ConnectionError("Unauthorized: AppWorks service-account session expired and relogin failed.")

        if resp.status_code == 404:
            if caller_token:
                # With a caller-supplied token, a 404 here is ambiguous in
                # a way it usually isn't for the service account: it could
                # genuinely mean "no such record", OR it could mean "this
                # token is stale/invalid/out of scope" — many AppWorks
                # deployments (and REST APIs generally) return 404 rather
                # than 401/403 for an unauthorized item lookup, precisely
                # to avoid confirming the record exists. Logged at
                # warning (not the usual silent `return {}`) so this is
                # visible without needing to reproduce and inspect
                # manually — see the log line immediately above this one
                # for the exact URL that returned it.
                logger.warning(
                    "AppWorks returned 404 for %s %s using a caller-supplied token. "
                    "This may mean the record genuinely doesn't exist, OR that the "
                    "token is stale/expired/out of scope for this case — AppWorks does "
                    "not distinguish the two at this endpoint. If this case is known-good, "
                    "get a fresh SAMLart and retry before assuming the case is missing.",
                    method,
                    url,
                )
            return {}

        resp.raise_for_status()
        return resp.json()

    except AppworksSessionExpiredError:
        raise
    except Exception as e:
        logger.error("REST Request Failed [%s]: %s", endpoint, e)
        raise ConnectionError(f"AppWorks API Error: {str(e)}")
