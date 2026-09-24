from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

STEP_INFO = {"U1": 1, "U2": 2, "U3": 3, "S1": 4}


def parse_process_status(value: str) -> int:
    """Parse DAS workflow status 1-5; seal confirmation is status 5."""
    text = strip_tags(value).strip()
    match = re.match(r"^\s*([1-5])(?:\s*[.．、]|\s|$)", text)
    if match:
        return int(match.group(1))
    if "铅封确认" in text:
        return 5
    embedded = re.search(r"(?:^|\D)([1-5])(?:\D|$)", text)
    if embedded:
        return int(embedded.group(1))
    return 0


def _photo_links(block: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for match in re.finditer(
        r"<a\b[^>]*\bhref=[\"'](?P<url>[^\"']+)[\"'][^>]*>(?P<body>.*?)</a>",
        block,
        re.IGNORECASE | re.DOTALL,
    ):
        url = match.group("url")
        if not (
            re.search(r"\.(?:jpe?g|png|bmp|webp)(?:\?|$)", url, re.IGNORECASE)
            or "photo" in url.lower()
            or "image" in url.lower()
        ):
            continue
        label_match = re.search(r"\bF\d+\b", strip_tags(match.group("body")), re.IGNORECASE)
        links.append((url, label_match.group(0).upper() if label_match else ""))
    if not links:
        links = [
            (value, "")
            for value in re.findall(
                r"<(?:img|image)\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE | re.DOTALL,
            )
            if re.search(r"\.(?:jpe?g|png|bmp|webp)(?:\?|$)", value, re.IGNORECASE)
        ]
    return list(dict.fromkeys(links))


def strip_tags(value: str) -> str:
    value = re.sub(
        r"<script\b[^>]*>.*?</script>", "", value, flags=re.IGNORECASE | re.DOTALL
    )
    value = re.sub(
        r"<style\b[^>]*>.*?</style>", "", value, flags=re.IGNORECASE | re.DOTALL
    )
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def element_text(source: str, element_id: str) -> str:
    pattern = rf"<(?P<tag>[a-z0-9]+)\b[^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*>(?P<body>.*?)</(?P=tag)>"
    match = re.search(pattern, source, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return strip_tags(match.group("body"))
    input_match = re.search(
        rf"<input\b[^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*\bvalue=[\"']([^\"']*)[\"']",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(input_match.group(1)).strip() if input_match else ""


@dataclass
class DasPhoto:
    step_code: str
    step_no: int
    source_url: str
    label: str = ""


@dataclass
class CpmDetail:
    cpm_id: str
    container_no: str
    seal_no: str = ""
    begin_date: str = ""
    end_date: str = ""
    product_type: str = ""
    packing_type: str = ""
    status_text: str = ""
    business_stage: int = 0
    das_process_status: int = 0
    latest_stage_code: str = ""
    photos: list[DasPhoto] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def parse_cpm_detail(source: str, page_url: str = "", cpm_id: str = "") -> CpmDetail:
    cpm_id = cpm_id or element_text(source, "lbCpmId")
    photos: list[DasPhoto] = []
    seen_urls: set[str] = set()
    for step_code, step_no in STEP_INFO.items():
        for row in re.findall(
            rf"<tr\b[^>]*\bid=[\"'][^\"']*TR_STEP_{step_code}[^\"']*[\"'][^>]*>(.*?)</tr>",
            source,
            re.IGNORECASE | re.DOTALL,
        ):
            for url, label in _photo_links(row):
                absolute = urljoin(page_url, html.unescape(url))
                if absolute not in seen_urls:
                    photos.append(DasPhoto(step_code, step_no, absolute, label))
                    seen_urls.add(absolute)
        if not any(photo.step_code == step_code for photo in photos):
            block = re.search(
                rf"id=[\"'][^\"']*TR_STEP_{step_code}[^\"']*[\"'].*?(?=<tr\b|</table>)",
                source,
                re.IGNORECASE | re.DOTALL,
            )
            if block:
                for url, label in _photo_links(block.group(0)):
                    absolute = urljoin(page_url, html.unescape(url))
                    if absolute not in seen_urls:
                        photos.append(DasPhoto(step_code, step_no, absolute, label))
                        seen_urls.add(absolute)
    status_text = element_text(source, "lbStatus")
    process_status = parse_process_status(status_text)
    highest = max(
        min(process_status, 4), max((photo.step_no for photo in photos), default=0)
    )
    latest_code = next(
        (code for code, stage in STEP_INFO.items() if stage == highest), ""
    )
    return CpmDetail(
        cpm_id=cpm_id,
        container_no=element_text(source, "lbCntrNo")
        or element_text(source, "lbl_container_no"),
        seal_no=element_text(source, "lbSealNo") or element_text(source, "lbl_seal_no"),
        begin_date=element_text(source, "lbBeginDate"),
        end_date=element_text(source, "lbEndDate"),
        product_type=element_text(source, "lbProductType")
        or element_text(source, "lbl_product_type"),
        packing_type=element_text(source, "lbPackingType")
        or element_text(source, "lbl_packing_type"),
        status_text=status_text,
        business_stage=highest,
        das_process_status=process_status,
        latest_stage_code=latest_code,
        photos=photos,
    )


def parse_gate_detail(source: str) -> dict[str, str]:
    return {
        "container_no": element_text(source, "lbl_container_no"),
        "seal_no": element_text(source, "lbl_seal_no"),
    }


def parse_gate_search_rows(source: str, page_url: str = "") -> list[dict[str, str]]:
    """Parse gate-pass list rows from the DAS WebForms result grid."""
    records: list[dict[str, str]] = []
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", source, re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL)
        if len(cells) < 15:
            continue
        values = [strip_tags(cell).replace("\xa0", "").strip() for cell in cells]
        container_no = re.sub(r"[^A-Z0-9]", "", values[9].upper())
        if not re.fullmatch(r"[A-Z]{4}\d{7}", container_no):
            continue
        postback = re.search(
            r"__doPostBack\(\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']*)[\"']\s*\)",
            row,
            re.IGNORECASE,
        )
        status_link = re.search(
            r"<a\b[^>]*\bhref=[\"']([^\"']*R_EGT_GERPTMRESERVEDETAIL\.aspx[^\"']*)[\"']",
            row,
            re.IGNORECASE | re.DOTALL,
        )
        status_url = (
            urljoin(page_url, html.unescape(status_link.group(1))) if status_link else ""
        )
        status_query = parse_qs(urlparse(status_url).query)
        records.append(
            {
                "application_date": values[2],
                "gate_type": values[3],
                "gate_pass_no": values[4],
                "vendor_name": values[5],
                "vehicle_no": values[6],
                "returner": values[7],
                "remark": values[8],
                "container_no": container_no,
                "seal_no": re.sub(r"\s+", "", values[10].upper()),
                "return_quantity": values[11],
                "process_status": values[12],
                "planned_departure_at": values[13],
                "actual_departure_at": values[14],
                "sequence_no": next(iter(status_query.get("Seqno", [])), ""),
                "status_url": status_url,
                "event_target": postback.group(1) if postback else "",
            }
        )
    return records


def find_cpm_id(source: str, page_url: str = "") -> str:
    """Find a CPMID exposed by a gate result/detail page, if DAS provides one."""
    query = parse_qs(urlparse(page_url).query)
    for key, values in query.items():
        if re.sub(r"[^a-z]", "", key.lower()) == "cpmid":
            candidate = next((value for value in values if value.isdigit()), "")
            if candidate:
                return candidate
    known = element_text(source, "lbCpmId")
    if known.isdigit():
        return known
    linked = re.search(r"[?&]cpm[_-]?id=(\d+)", source, re.IGNORECASE)
    if linked:
        return linked.group(1)
    for tag in re.findall(
        r"<(?:input|span|label|td|a)\b[^>]*(?:>.*?</(?:span|label|td|a)>|/?>)",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        if not re.search(
            r"\b(?:id|name)=[\"'][^\"']*cpm[_-]?id[^\"']*[\"']",
            tag,
            re.IGNORECASE,
        ):
            continue
        value = re.search(r"\bvalue=[\"'](\d+)[\"']", tag, re.IGNORECASE)
        if value:
            return value.group(1)
        text = re.search(r"\b(\d{1,12})\b", strip_tags(tag))
        if text:
            return text.group(1)
    scripted = re.search(
        r"\bcpm[_-]?id\b\s*[:=]\s*[\"']?(\d+)", source, re.IGNORECASE
    )
    return scripted.group(1) if scripted else ""


def find_latest_cpm_id(source: str) -> str:
    """Return the first CPMID row from a newest-first DAS CPM result table."""
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", source, re.IGNORECASE | re.DOTALL):
        candidate = find_cpm_id(row)
        if candidate:
            return candidate
    candidates = re.findall(r"[?&]cpm[_-]?id=(\d+)", source, re.IGNORECASE)
    return candidates[0] if candidates else ""


def parse_cpm_search_rows(source: str, limit: int = 500) -> list[dict]:
    """Parse newest-first metadata rows from the DAS CPM search result table."""
    records: list[dict] = []
    stage_codes = {1: "U1", 2: "U2", 3: "U3", 4: "S1"}
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", source, re.IGNORECASE | re.DOTALL):
        cpm_id = find_cpm_id(row)
        if not cpm_id:
            continue
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.IGNORECASE | re.DOTALL)
        if len(cells) < 4:
            continue
        values = [strip_tags(cell).replace("\xa0", "").strip() for cell in cells]
        container_no = re.sub(r"[^A-Z0-9]", "", values[2].upper())
        if not re.fullmatch(r"[A-Z]{4}\d{7}", container_no):
            continue
        status_text = values[3]
        process_status = parse_process_status(status_text)
        stage = min(process_status, 4)
        records.append(
            {
                "cpm_id": cpm_id,
                "container_no": container_no,
                "begin_date": values[4] if len(values) > 4 else "",
                "end_date": values[5] if len(values) > 5 else "",
                "product_type": values[6] if len(values) > 6 else "",
                "packing_type": values[7] if len(values) > 7 else "",
                "das_status_text": status_text,
                "business_stage": stage,
                "das_process_status": process_status,
                "latest_stage_code": stage_codes.get(stage, ""),
                "photo_count": 0,
                "is_valid": True,
            }
        )
        if len(records) >= limit:
            break
    return records


def find_gate_postback(source: str, container_no: str) -> str:
    wanted = re.sub(r"[^A-Z0-9]", "", container_no.upper())
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", source, re.IGNORECASE | re.DOTALL):
        hidden = re.search(
            r"\bid=[\"'][^\"']*hddCntrNo[^\"']*[\"'][^>]*\bvalue=[\"']([^\"']+)[\"']",
            row,
            re.IGNORECASE | re.DOTALL,
        )
        row_container = (
            re.sub(r"[^A-Z0-9]", "", html.unescape(hidden.group(1)).upper())
            if hidden
            else ""
        )
        compact = re.sub(r"[^A-Z0-9]", "", strip_tags(row).upper())
        if wanted and (
            (row_container and row_container != wanted)
            or (not row_container and wanted not in compact)
        ):
            continue
        match = re.search(
            r"__doPostBack\(\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']*)[\"']\s*\)",
            row,
            re.IGNORECASE,
        )
        if match:
            return match.group(1)
    return ""
