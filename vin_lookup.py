import io
import logging
import re
import time
import requests
import pdfplumber
from config import NHTSA_URL

logger = logging.getLogger(__name__)

NHTSA_FIELDS = {
    "ModelYear": "year",
    "Make": "make",
    "Model": "model",
    "Series": "series",
    "Trim": "trim",
    "BodyClass": "body_class",
    "DriveType": "drive_type",
    "TransmissionStyle": "transmission",
    "DisplacementL": "displacement_l",
    "FuelTypePrimary": "fuel_type",
}

TOYOTA_WINDOW_STICKER_URL = "https://www.toyota.com/t3Portal/prodpage/getWindowSticker?vin={vin}"

# VIN WMI prefix (first 3-4 chars) → Toyota model name fallback
_VIN_MODEL_MAP = {
    "3TYK": "Tacoma",     "3TYL": "Tacoma",     "3TYM": "Tacoma",
    "5TFT": "Tundra",     "5TFE": "Tundra",     "3TMC": "Tundra",     "3TM": "Tundra",
    "5TDB": "Highlander", "5TDZ": "Highlander",
    "5TDK": "Sequoia",    "5TD":  "Sienna",
    "4T1":  "Camry",      "2T1":  "Corolla",
    "5TJJ": "RAV4",       "5TJG": "RAV4",       "JTM":  "RAV4",       "JTE": "RAV4",
    "JTJ":  "4Runner",    "4T3":  "Venza",       "JTJB": "GX",
    "JTMC": "C-HR",       "JTMW": "bZ4X",        "JTMG": "bZ4X",
}

# VIN 10th character → model year (ISO 3779 standard, cycles every 30 years)
_VIN_YEAR_MAP = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014,
    'F': 2015, 'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019,
    'L': 2020, 'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024,
    'S': 2025, 'T': 2026, 'V': 2027, 'W': 2028, 'X': 2029,
    'Y': 2030,
}


def _year_from_vin(vin: str) -> str:
    """Decode model year from VIN 10th character. Returns '' if unrecognized."""
    if len(vin) >= 10:
        return str(_VIN_YEAR_MAP.get(vin[9].upper(), ""))
    return ""


def lookup_vin(vin: str) -> dict:
    """
    1. NHTSA → year, make, model (with retry)
    2. Toyota Window Sticker PDF → real trim name + exterior color (with retry)
    3. Falls back to VIN-encoded year, hardcoded Toyota make, and WMI model table.
    """
    result = {v: "" for v in NHTSA_FIELDS.values()}
    result["vin"] = vin
    result["color"] = ""

    # --- Step 1: NHTSA base data (retry once on failure) ---
    url = NHTSA_URL.format(vin=vin)
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("Results", []):
                variable = item.get("Variable", "")
                value = item.get("Value") or ""
                if variable in NHTSA_FIELDS and value and value.lower() not in ("not applicable", "null", "0"):
                    result[NHTSA_FIELDS[variable]] = value.strip()
            break  # success
        except Exception as e:
            if attempt == 0:
                logger.warning(f"NHTSA attempt 1 failed for {vin}: {e} — retrying in 2s")
                time.sleep(2)
            else:
                logger.error(f"NHTSA lookup failed for {vin} after 2 attempts: {e}")

    # Fallback: year from VIN 10th character
    if not result.get("year"):
        result["year"] = _year_from_vin(vin)
        if result["year"]:
            logger.info(f"Year decoded from VIN for {vin}: {result['year']}")

    # Fallback: make is always Toyota for this dealership
    if not result.get("make"):
        result["make"] = "Toyota"
        logger.info(f"Make defaulted to Toyota for {vin}")

    # Fallback: model from VIN WMI prefix table
    if not result.get("model"):
        for prefix_len in (4, 3):
            model = _VIN_MODEL_MAP.get(vin[:prefix_len].upper(), "")
            if model:
                result["model"] = model
                logger.info(f"Model decoded from VIN prefix for {vin}: {model}")
                break

    # --- Step 2: Toyota Window Sticker (retry once on failure) ---
    try:
        sticker_trim, sticker_color = _fetch_window_sticker(vin)
        if sticker_trim:
            result["trim"] = sticker_trim
            logger.info(f"Window sticker trim for {vin}: {sticker_trim}")
        if sticker_color:
            result["color"] = sticker_color
            logger.info(f"Window sticker color for {vin}: {sticker_color}")
    except Exception as e:
        logger.warning(f"Window sticker lookup failed for {vin}: {e}")

    result["description"] = _build_description(result)
    return result


def _fetch_window_sticker(vin: str) -> tuple:
    """
    Fetches Toyota window sticker PDF and extracts trim and exterior color.
    Returns (trim, color) — either may be empty string if not found.
    Retries once on network failure. Logs non-200 and parse-miss cases for debugging.
    """
    url = TOYOTA_WINDOW_STICKER_URL.format(vin=vin)

    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=30)
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Window sticker fetch error for {vin} (attempt 1): {e} — retrying")
                time.sleep(2)
                continue
            logger.warning(f"Window sticker fetch error for {vin} (attempt 2): {e}")
            return "", ""

        if resp.status_code == 404:
            logger.info(f"Window sticker 404 for {vin} — VIN not yet in Toyota portal")
            return "", ""
        if resp.status_code != 200:
            logger.warning(f"Window sticker HTTP {resp.status_code} for {vin}")
            if attempt == 0:
                time.sleep(2)
                continue
            return "", ""
        if "application/pdf" not in resp.headers.get("Content-Type", ""):
            logger.warning(f"Window sticker non-PDF response for {vin}: {resp.headers.get('Content-Type', 'unknown')}")
            return "", ""

        # Got a PDF — extract text
        pdf_bytes = resp.content
        text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text += page_text + "\n"

        trim = _parse_sticker_trim(text)
        color = _parse_sticker_color(text)

        if not trim and not color:
            logger.debug(f"Window sticker parse found nothing for {vin}. Text preview: {text[:400]!r}")

        return trim, color

    return "", ""


def _parse_sticker_trim(text: str) -> str:
    """
    Extract trim grade from window sticker text.
    Toyota window stickers list the grade/trim on the first page near the model name.
    """
    trim_keywords = (
        r"(?:TRD Pro|TRD Off-Road|TRD Sport|TRD|XLE Premium|XLE|XSE|XSP|"
        r"SR5|SR|LE|SE|Limited|Platinum|Premium|Pro|Off-Road|"
        r"Adventure|Trail|Nightshade|Special Edition|Hybrid|PHEV|GR Sport|GR|"
        r"Woodland|Bronze Edition|1794 Edition|Capstone|Executive|Prestige|"
        r"High|Mid|Base)"
    )

    # Pattern 1: "Grade: TRIM" or "Grade TRIM"
    m = re.search(r"Grade[:\s]+([A-Z][A-Za-z0-9 ]+?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Pattern 2: "20XX Toyota ModelName TRIM" — year+make+model+trim on one header line
    m = re.search(
        r"20\d\d\s+Toyota\s+\w[\w\s]*?\s+" + trim_keywords,
        text, re.IGNORECASE
    )
    if m:
        # Extract just the trim portion at the end of the match
        tm = re.search(trim_keywords + r"(?:\s+\w+)?$", m.group(0), re.IGNORECASE)
        if tm:
            return tm.group(0).strip()

    # Pattern 3: Trim keyword on its own line or at line start
    m = re.search(r"(?:^|\n)\s*" + trim_keywords + r"\s*(?:\n|$)", text, re.IGNORECASE)
    if m:
        return m.group(0).strip()

    # Pattern 4: Any occurrence of a known trim keyword (last resort)
    m = re.search(trim_keywords, text, re.IGNORECASE)
    if m:
        return m.group(0).strip()

    return ""


def _parse_sticker_color(text: str) -> str:
    """
    Extract exterior color from window sticker text.
    Toyota window stickers list exterior color in various formats:
      "Exterior Color: Midnight Black Metallic"
      "Ext. Color  Midnight Black Metallic"
      "Outside Color Underground"
      "Color  Ice Cap"
    """
    for pattern in [
        r"Exterior\s+Color[:\s]+([A-Za-z][^\n]{2,40})",
        r"Ext\.?\s+Color[:\s]+([A-Za-z][^\n]{2,40})",
        r"Outside\s+Color[:\s]+([A-Za-z][^\n]{2,40})",
        r"(?:^|\n)\s*Color[:\s]+([A-Za-z][^\n]{2,40})",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            color = m.group(1).strip()
            # Remove price/code suffixes like "$495", "(040)", "[A]", numeric codes
            color = re.sub(r"\s*[\$\(\[].*$", "", color).strip()
            color = re.sub(r"\s*\b[0-9]{3,}\b.*$", "", color).strip()
            if color and len(color) >= 3:
                return color

    return ""


def _build_description(info: dict) -> str:
    year = info.get("year", "")
    make = info.get("make", "")
    model = info.get("model", "")
    trim = info.get("trim", "")

    base = f"{year} {model}".strip()

    # Reject anything that looks like a series number e.g. "40 Series", "62 Series"
    trim_is_series = bool(re.match(r"^\d+\s*series$", trim.strip(), re.IGNORECASE))
    if trim and trim.lower() not in ("unknown", "") and not trim_is_series:
        desc = f"{base} {trim}"
    else:
        desc = base

    return _title_case(desc)


def _title_case(text: str) -> str:
    """Title-case while preserving known uppercase abbreviations."""
    return " ".join(word.capitalize() for word in text.split())


def build_vehicle_line(vin: str, info: dict, color: str = "") -> str:
    """Format a single vehicle line: `VIN` | Year Model | Trim | Color"""
    year = info.get("year", "")
    model = info.get("model", "")
    trim = info.get("trim", "")
    effective_color = color or info.get("color", "")

    parts = [f"`{vin}`"]
    model_part = f"{year} {model}".strip()
    if model_part:
        parts.append(model_part)
    if trim:
        parts.append(trim)
    if effective_color:
        parts.append(effective_color)

    return " | ".join(parts)
