from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    base_dir: Path
    sso_url: str = "http://newep.lge.com/portal/main/portalMain.do"
    das_login_url: str = "http://das.china.lge.com:7005/LoginSSO.aspx"
    gate_url: str = "http://das.china.lge.com:7005/eGate/tm/R_EGT_TMGERPVIEW.aspx?t3_menuid=EGT340103"
    cpm_detail_url: str = (
        "http://das.china.lge.com:7005/Manage/cpm/V_CPM_DETAIL.aspx?cpm_id={cpm_id}"
    )
    session_check_seconds: int = 600
    sync_interval_seconds: int = 600
    sync_batch_size: int = 100

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"
