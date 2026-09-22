from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

STEP_INFO = {"U1": 1, "U2": 2, "U3": 3, "S1": 4}


def _photo_urls(block: str) -> list[str]:
    urls = [
        match.group(1)
        for match in re.finditer(
            r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if re.search(r"\.(?:jpe?g|png|bmp|webp)(?:\?|$)", match.group(1), re.IGNORECASE)
        or "photo" in match.group(1).lower()
        or "image" in match.group(1).lower()
    ]
    if not urls:
        urls = [
            value
            for value in re.findall(
                r"<(?:img|image)\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
                block,
                re.IGNORECASE | re.DOTALL,
            )
            if re.search(r"\.(?:jpe?g|png|bmp|webp)(?:\?|$)", value, re.IGNORECASE)
        ]
    return list(dict.fromkeys(urls))


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


@dataclass
class CpmDetail:
    cpm_id: str
    container_no: str
    seal_no: str = ""
    begin_date: str = ""
    end_date: str = ""
    status_text: str = ""
    business_stage: int = 0
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
            for url in _photo_urls(row):
                absolute = urljoin(page_url, html.unescape(url))
                if absolute not in seen_urls:
                    photos.append(DasPhoto(step_code, step_no, absolute))
                    seen_urls.add(absolute)
        if not any(photo.step_code == step_code for photo in photos):
            block = re.search(
                rf"id=[\"'][^\"']*TR_STEP_{step_code}[^\"']*[\"'].*?(?=<tr\b|</table>)",
                source,
                re.IGNORECASE | re.DOTALL,
            )
            if block:
                for url in _photo_urls(block.group(0)):
                    absolute = urljoin(page_url, html.unescape(url))
                    if absolute not in seen_urls:
                        photos.append(DasPhoto(step_code, step_no, absolute))
                        seen_urls.add(absolute)
    status_text = element_text(source, "lbStatus")
    status_match = re.search(r"(?:^|\D)([1-4])(?:\D|$)", status_text)
    status_stage = int(status_match.group(1)) if status_match else 0
    highest = max(status_stage, max((photo.step_no for photo in photos), default=0))
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
        status_text=status_text,
        business_stage=highest,
        latest_stage_code=latest_code,
        photos=photos,
    )


def parse_gate_detail(source: str) -> dict[str, str]:
    return {
        "container_no": element_text(source, "lbl_container_no"),
        "seal_no": element_text(source, "lbl_seal_no"),
    }


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
