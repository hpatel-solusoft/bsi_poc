# semantic_layer/appworks_auth.py
# ----------------------------------------------------------------
# AppWorks Gateway & Authentication (OTDS + SAML Flow)
# ----------------------------------------------------------------

import os
import re
import logging
import requests
from datetime import datetime
from typing import Any, Dict, Optional
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OTDS_URL = os.getenv("OTDS_URL")
SOAP_URL = os.getenv("SOAP_GATEWAY_URL")
REST_URL = os.getenv("APPWORKS_URL")   # e.g. http://host:81/.../OSABSIACM
USER     = os.getenv("APPWORKS_USER")
PASS     = os.getenv("APPWORKS_PASS")

_SAML_TOKEN: Optional[str] = None


def perform_login() -> bool:
    global _SAML_TOKEN

    if not all([OTDS_URL, SOAP_URL, REST_URL, USER, PASS]):
        logger.error("Missing required environment variables in .env")
        return False

    try:
        logger.info(f"[Auth] Requesting OTDS Ticket: {OTDS_URL}")
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

        soap_resp = requests.post(SOAP_URL, headers={"Content-Type": "text/xml"}, data=soap_envelope, timeout=15)
        soap_resp.raise_for_status()

        match = re.search(r"<[^>]*?AssertionArtifact[^>]*?>(.*?)</[^>]*?AssertionArtifact>", soap_resp.text, re.DOTALL)
        if not match:
            logger.error("SAML AssertionArtifact not found in SOAP response.")
            return False

        _SAML_TOKEN = match.group(1).strip()
        logger.info("AppWorks Authentication Successful.")
        return True

    except Exception as e:
        logger.error(f"Authentication Failure: {str(e)}")
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

    # Derive the base without the last namespace segment
    # REST_URL: http://host/.../api/OSABSIACM
    rest_base = REST_URL.rstrip("/")
    # api_root: http://host/.../api
    api_root = rest_base.rsplit("/", 1)[0]
    # primary_ns: OSABSIACM
    primary_ns = rest_base.rsplit("/", 1)[-1]

    # Detect which namespace the endpoint belongs to
    endpoint_ns = clean.split("/")[0]

    if endpoint_ns == primary_ns:
        # Strip leading namespace, append to REST_URL
        path_after_ns = clean[len(primary_ns):].lstrip("/")
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

    rest_base  = REST_URL.rstrip("/")
    primary_ns = rest_base.rsplit("/", 1)[-1]

    # Derive the app root: http://host:81/home/BSIDev/app
    # REST_URL structure: <app_root>/entityRestService/api/<ns>
    # Strip "/entityRestService/api/<ns>" to get app_root
    app_root = rest_base
    for suffix in (f"/entityRestService/api/{primary_ns}",
                   f"/entityRestService/api",
                   f"/entityRestService"):
        if app_root.endswith(suffix):
            app_root = app_root[: -len(suffix)]
            break

    list_base = f"{app_root}/entityservice/{primary_ns}"

    # endpoint may or may not be prefixed with the namespace
    endpoint_ns = clean.split("/")[0]
    if endpoint_ns == primary_ns:
        path_after_ns = clean[len(primary_ns):].lstrip("/")
        return f"{list_base}/{path_after_ns}"
    else:
        return f"{list_base}/{clean}"


def fetch_list(endpoint: str, params: dict = None, _retry: bool = True) -> dict:
    """
    Fetch an AppWorks LIST endpoint (entityservice path).

    Use this for any /lists/ query — e.g.:
        /entities/Allegations/lists/Allegations_All?Allegations_AllegationsType$Identity.Id=114689

    The entityservice base is derived automatically from APPWORKS_URL.
    Authentication (SAMLart) is injected the same way as fetch().
    """
    global _SAML_TOKEN

    if not _SAML_TOKEN:
        logger.info("[Auth] No AppWorks token available; performing lazy login.")
        if not perform_login():
            raise ConnectionError("Unauthorized: AppWorks login failed.")

    url = _build_list_url(endpoint)
    
    # Merge SAML token with other parameters
    q_params = {"SAMLart": _SAML_TOKEN}
    if params:
        q_params.update(params)
        
    headers = {"SAMLart": _SAML_TOKEN, "Accept": "application/json"}
    cookies = {"SAMLart": _SAML_TOKEN}

    try:
        logger.info(f"[REST-LIST] GET {url}")
        resp = requests.get(url, params=q_params, headers=headers, cookies=cookies, timeout=20)

        if resp.status_code == 401 and _retry:
            logger.warning("Session expired (401). Retrying authentication...")
            _SAML_TOKEN = None
            return fetch_list(endpoint, params=params, _retry=False)

        if resp.status_code == 404:
            return {}

        resp.raise_for_status()
        return resp.json()

    except Exception as e:
        logger.error(f"REST-LIST Request Failed [{endpoint}]: {str(e)}")
        raise ConnectionError(f"AppWorks List API Error: {str(e)}")


def fetch(endpoint: str, method: str = "GET", params: dict = None, payload: dict = None, _retry: bool = True) -> dict:
    """
    General purpose AppWorks REST fetcher.
    Auto-builds URLs, handles SAML injection, and retries 401s once.
    """
    global _SAML_TOKEN

    if not _SAML_TOKEN:
        logger.info("[Auth] No AppWorks token available; performing lazy login.")
        if not perform_login():
            raise ConnectionError("Unauthorized: AppWorks login failed.")

    # Guard: prevent list-based IDs
    if "items/[" in endpoint or "items/%5B" in endpoint:
        logger.error(f"Invalid API call detected: {endpoint}")
        raise ValueError(
            "AppWorks REST API /items/{id} does not support list-based IDs. "
            "Use /lists/ endpoints for filtering and searching."
        )

    url = _build_url(endpoint)
    
    # Merge SAML token with other parameters
    q_params = {"SAMLart": _SAML_TOKEN}
    if params:
        q_params.update(params)

    headers = {
        "SAMLart": _SAML_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        logger.info(f"[REST] {method} {url}")
        resp = requests.request(method, url, params=q_params, headers=headers, json=payload, timeout=20)

        if resp.status_code == 401 and _retry:
            logger.warning("Session expired (401). Retrying authentication...")
            _SAML_TOKEN = None
            return fetch(endpoint, method, params=params, payload=payload, _retry=False)

        if resp.status_code == 404:
            return {}

        resp.raise_for_status()
        return resp.json()

    except Exception as e:
        logger.error(f"REST Request Failed [{endpoint}]: {str(e)}")
        raise ConnectionError(f"AppWorks API Error: {str(e)}")
