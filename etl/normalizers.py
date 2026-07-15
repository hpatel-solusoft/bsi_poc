"""
Owns: the canonicalisation of raw AppWorks field values into the match
keys the reasoning rules join on (employer key, address key, alias value,
dates, money, booleans, and the deterministic Commentary id).

Why this file exists at all: every Wave 1 rule is a *string equality
join*. Rule 1 joins two subjects on the same :Employer node, Rule 3 on
the same :Address node, Rule 5 on the same :Alias node. If ETL writes
"ACME CORP." for one subject and "Acme Corp" for another, the rule
silently does not fire — no error, no log line, just a fraud network
that never forms. Normalisation is therefore not cosmetic; it is the
precondition for the entire rule library working at all, so it lives in
one place with one owner rather than being inlined per-field inside
graph_sync.py.

Does NOT own: any AppWorks path, any Neo4j write, any rule logic.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

# Corporate suffixes stripped before building an employer name key, so
# "Acme Corp" and "Acme Corporation" collapse to the same key. Applied
# ONLY to the name-fallback key, never to the FEIN key — a FEIN match is
# exact by definition and needs no fuzzing.
_EMPLOYER_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co",
    "limited", "ltd", "llc", "llp", "lp", "plc", "pc",
)

# Street-type abbreviations. AppWorks' Address_Address is one free-text
# field with no enforced data-entry standard (see etl/GAP_ANALYSIS.md),
# so "12 Main Street" and "12 Main St." arrive as two different strings
# for one physical address. This table is the minimum viable
# normalisation; it is deliberately NOT a geocoder. If Rule 3's match
# quality turns out to matter more than this buys, the correct fix is a
# real address-normalisation service between AppWorks and Neo4j, not a
# longer table here.
_STREET_TOKENS = {
    "street": "st", "st.": "st",
    "avenue": "ave", "av": "ave", "ave.": "ave",
    "road": "rd", "rd.": "rd",
    "drive": "dr", "dr.": "dr",
    "boulevard": "blvd", "blvd.": "blvd",
    "lane": "ln", "ln.": "ln",
    "court": "ct", "ct.": "ct",
    "place": "pl", "pl.": "pl",
    "terrace": "ter", "highway": "hwy", "parkway": "pkwy",
    "apartment": "apt", "apt.": "apt", "unit": "unit", "suite": "ste", "ste.": "ste",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

_WS = re.compile(r"\s+")
_NON_ALNUM_SPACE = re.compile(r"[^a-z0-9 ]")
_DIGITS = re.compile(r"\D")


def clean_text(value: Any) -> Optional[str]:
    """Trim and collapse whitespace. Empty string becomes None so a blank
    AppWorks field never becomes a graph property with a meaningless ""
    value that a rule could still match on."""
    if value is None:
        return None
    text = _WS.sub(" ", str(value)).strip()
    return text or None


def to_bool(value: Any) -> bool:
    """AppWorks returns booleans as true/false, "Y"/"N", "1"/"0", or
    "True"/"False" depending on the field. All of them mean the same
    thing to a rule, so all of them are collapsed here rather than each
    call site re-guessing."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "y", "yes", "1"}


def to_float(value: Any) -> Optional[float]:
    """Money fields arrive as "$52,000.00" or 52000 or "" depending on
    the field and who entered it. Rule 13 compares fraud_amount against
    a numeric threshold, so it must be a number in the graph, not a
    string that silently fails a > comparison."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned not in ("", "-", ".") else None
    except ValueError:
        return None


def to_iso_date(value: Any) -> Optional[str]:
    """Return an ISO-8601 date string, or None. Stored as a string rather
    than a Neo4j date type because AppWorks' date formats are not
    uniform across entities and a partially-parseable date is more
    useful to an investigator than a dropped one — Rule 12's date
    comparison uses date(...) on this string and degrades explicitly
    (see that rule) when it is null."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:len(fmt) + 2].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    # ISO strings with a timezone suffix (the common AppWorks case).
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def now_iso() -> str:
    """Single source of the ETL's retrieved_at / ingested_at timestamps
    (Section 3.3's asserted-relationship provenance pair)."""
    return datetime.now(timezone.utc).isoformat()


def _slug(value: Optional[str]) -> str:
    if not value:
        return ""
    text = _NON_ALNUM_SPACE.sub(" ", str(value).lower())
    return _WS.sub(" ", text).strip()


def normalize_fein(value: Any) -> Optional[str]:
    """FEIN is Tier 2 PII (Section 3.5) — stored, because matching
    depends on it directly. Digits only: "04-1234567" and "041234567"
    are the same employer, and Rule 1's High-confidence branch depends
    on that being true in the graph."""
    if not value:
        return None
    digits = _DIGITS.sub("", str(value))
    return digits or None


def employer_key(fein: Any, employer_fid: Any, employer_name: Any) -> Optional[str]:
    """
    The canonical :Employer match key, in strict priority order:

        FEIN:<digits>   — Section 3.1's stated "primary match key"
        FID:<id>        — AppWorks' own employer foreign id (the Wage
                          table carries this but no FEIN — see
                          GAP_ANALYSIS.md). Two subjects whose wage rows
                          point at the same employer_fid ARE the same
                          employer, even with no FEIN to prove it, which
                          is exactly what Rule 9 needs.
        NAME:<slug>     — last resort, suffix-stripped. Produces Rule 1's
                          documented "Medium" confidence branch rather
                          than the High that a FEIN match earns.

    SCHEMA EXTENSION, FLAGGED: `employer_key` is not in Section 3.1's
    :Employer property list (which names only employer_name and fein).
    It is added under that section's own stated latitude ("Key Properties
    columns are not exhaustive... a new property can be added whenever a
    new rule or data source needs one"). The alternative — MERGE-ing
    :Employer on `fein` alone, as the previous ETL did — silently drops
    every employer AppWorks has no FEIN for, which is most of the Wage
    table, which is precisely the data Rules 9 and 12 need. Worth
    confirming with whoever owns the reference doc.
    """
    fein_digits = normalize_fein(fein)
    if fein_digits:
        return f"FEIN:{fein_digits}"
    fid = clean_text(employer_fid)
    if fid:
        return f"FID:{fid}"
    name_slug = _slug(employer_name)
    if not name_slug:
        return None
    tokens = [t for t in name_slug.split() if t not in _EMPLOYER_SUFFIXES]
    return f"NAME:{' '.join(tokens)}" if tokens else None


def normalize_street(street: Any) -> Optional[str]:
    """Lowercase, punctuation-stripped, street-type-abbreviated form of
    the free-text address line. Kept alongside the raw `street` property
    rather than replacing it — investigators need to see what was
    actually entered; only the rules need the normalised form."""
    slug = _slug(street)
    if not slug:
        return None
    return " ".join(_STREET_TOKENS.get(tok, tok) for tok in slug.split())


def normalize_zip(value: Any) -> Optional[str]:
    """First five digits. "02108-1234" and "02108" are the same place for
    matching purposes."""
    if not value:
        return None
    digits = _DIGITS.sub("", str(value))
    return digits[:5] or None


def address_key(street: Any, city: Any, state: Any, zip_code: Any) -> Optional[str]:
    """
    The canonical :Address match key — the thing Rule 3 actually joins
    on. Same schema-extension caveat as employer_key: Section 3.1 names
    street/city/state/zip and Section 3.4 indexes them as a composite,
    which only matches when all four were typed identically by two
    different investigators. That is not a safe assumption against real
    data entry (GAP_ANALYSIS.md flags it as an open ask). The raw four
    properties are still written exactly as the schema specifies; this
    key is written in addition, and is what the rule matches on.
    """
    parts = [
        normalize_street(street) or "",
        _slug(city) or "",
        (clean_text(state) or "").upper(),
        normalize_zip(zip_code) or "",
    ]
    if not any(parts):
        return None
    return "|".join(parts)


def alias_value(value: Any) -> Optional[str]:
    """Rule 5 is specified as exact string match only, so the alias is
    trimmed and whitespace-collapsed but NOT case-folded or fuzzed —
    doing so would quietly widen a rule the spec deliberately keeps
    narrow."""
    return clean_text(value)


def commentary_id(case_id: str, source_kind: str, source_id: Optional[str],
                  text: Optional[str], created_date: Optional[str]) -> str:
    """
    A deterministic, stable id for a :Commentary node.

    Section 3.1 gives :Commentary no id property, so the previous ETL
    used CREATE and duplicated every comment on every re-sync — which
    makes the ETL non-idempotent, which makes a lifecycle-event-driven
    re-sync (the whole point of this build) unusable in production.

    Preference order: AppWorks' own record id when the fetch could
    resolve one; otherwise a content hash. The hash is stable across
    re-runs for identical content, so re-syncing the same case MERGEs
    onto the same node instead of piling up copies.
    """
    if source_id:
        return f"{source_kind}:{source_id}"
    digest = hashlib.sha1(
        f"{case_id}|{source_kind}|{text or ''}|{created_date or ''}".encode("utf-8")
    ).hexdigest()[:20]
    return f"{source_kind}:h:{digest}"
