import re
from typing import Optional

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# [download]  12.3%  veya [download] 12,3% gibi varyasyonları yakala
_PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:[.,]\d+)?)%")

def parse_progress(line: str) -> Optional[float]:
    if not line:
        return None

    # Olası ANSI renk kodlarını temizle
    line = _ANSI_RE.sub("", line)

    m = _PROGRESS_RE.search(line)
    if not m:
        return None

    try:
        pct_s = m.group(1).replace(",", ".")
        pct = float(pct_s)
        pct = max(0.0, min(100.0, pct))
        return pct / 100.0
    except Exception:
        return None
