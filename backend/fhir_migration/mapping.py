"""Pure functions that flatten FHIR R4 resources into our internal model fields.

They take the raw resource dict and return a dict of model fields, so they can be
tested without a database or network. See the "Data mapping" tables in Plan.md.
"""

from datetime import datetime, time, timezone
from typing import Any

from django.utils.dateparse import parse_date, parse_datetime


def map_patient(resource: dict[str, Any]) -> dict[str, Any]:
    name = _official_name(resource.get("name", []))
    return {
        "fhir_id": resource["id"],
        "mrn": _mrn(resource.get("identifier", [])),
        "family_name": name.get("family", ""),
        "given_names": " ".join(name.get("given", [])),
        "gender": resource.get("gender", ""),
        "birth_date": _date(resource.get("birthDate")),
        "active": resource.get("active"),
        "managing_org_ref": resource.get("managingOrganization", {}).get("reference", ""),
    }


def map_observation(resource: dict[str, Any]) -> dict[str, Any]:
    coding = _first(resource.get("code", {}).get("coding"))
    reference_range = _first(resource.get("referenceRange"))
    return {
        "fhir_id": resource["id"],
        "subject_reference": resource.get("subject", {}).get("reference", ""),
        # Required by FHIR (1..1), but the sandbox has some without it. "unknown" is a valid
        # value of the status list, so the record is kept instead of skipped.
        "status": resource.get("status", "unknown"),
        "code_system": coding.get("system", ""),
        "code": coding.get("code", ""),
        "code_display": coding.get("display") or resource.get("code", {}).get("text", ""),
        "effective_at": _datetime(
            resource.get("effectiveDateTime")
            or resource.get("effectivePeriod", {}).get("start")
        ),
        "issued_at": _datetime(resource.get("issued")),
        "value_quantity": resource.get("valueQuantity"),
        "interpretation_code": _first(
            _first(resource.get("interpretation")).get("coding")
        ).get("code", ""),
        "ref_low": reference_range.get("low", {}).get("value"),
        "ref_high": reference_range.get("high", {}).get("value"),
        "performer_ref": _first(resource.get("performer")).get("reference", ""),
    }


def patient_id_from_reference(reference: str) -> str | None:
    """'Patient/123', 'Patient/123/_history/2' or an absolute URL -> '123'.
    None when the subject isn't a Patient (e.g. 'Group/7')."""
    parts = reference.split("/")
    if "Patient" not in parts[:-1]:
        return None
    return parts[parts.index("Patient") + 1]


def is_subsetted(resource: dict[str, Any]) -> bool:
    """Tagged SUBSETTED: some elements were left out (summary mode, `_elements`)."""
    return any(tag.get("code") == "SUBSETTED" for tag in resource.get("meta", {}).get("tag", []))


def _first(items):
    return items[0] if items else {}


def _official_name(names):
    return next((n for n in names if n.get("use") == "official"), _first(names))


def _mrn(identifiers):
    for identifier in identifiers:
        codes = [c.get("code") for c in identifier.get("type", {}).get("coding", [])]
        if "MR" in codes:
            return identifier.get("value")
    return None


def _date(value):
    # FHIR allows partial dates ("1970", "1970-04"); we keep only full dates.
    if not value or len(value) != 10:
        return None
    return parse_date(value)


def _datetime(value):
    # FHIR dateTime may be date-only or partial; a full date is read as midnight UTC.
    if not value:
        return None
    if date := _date(value):
        return datetime.combine(date, time.min, tzinfo=timezone.utc)
    parsed = parse_datetime(value)
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed

