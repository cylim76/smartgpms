from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    data_root: Path | None = None
    sso_url: str = "http://newep.lge.com/portal/main/portalMain.do"
    das_login_url: str = "http://das.china.lge.com:7005/LoginSSO.aspx"
    gate_url: str = "http://das.china.lge.com:7005/eGate/tm/R_EGT_TMGERPVIEW.aspx?t3_menuid=EGT340103"
    cpm_detail_url: str = (
        "http://das.china.lge.com:7005/Manage/cpm/V_CPM_DETAIL.aspx?cpm_id={cpm_id}"
    )
    cpm_index_url: str = (
        "http://das.china.lge.com:7005/Manage/cpm/R_CPM_SEARCH.aspx?t3_menuid=EGT340112"
    )
    session_check_seconds: int = 600
    sync_interval_seconds: int = 600
    sync_batch_size: int = 100
    targeted_scan_limit: int = 300
    startup_snapshot_size: int = 500
    daily_scan_safety_limit: int = 5000
    photo_retention_days: int = 30
    crop_retention_days: int = 60
    gatepass_pdf_retention_days: int = 60
    print_evidence_retention_days: int = 365
    cache_cleanup_hour: int = 12
    cache_cleanup_minute: int = 0
    cache_max_bytes: int = 20 * 1024 * 1024 * 1024
    cache_min_free_bytes: int = 10 * 1024 * 1024 * 1024

    @property
    def data_dir(self) -> Path:
        return self.data_root or self.base_dir / "data"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def print_spool_dir(self) -> Path:
        return self.data_dir / "print-spool"

    @property
    def gatepass_dir(self) -> Path:
        return self.data_dir / "gatepass"
