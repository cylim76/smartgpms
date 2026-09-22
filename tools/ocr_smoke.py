import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartgpms.recognition import RapidOCREngine, extract_container_candidates

source = Path(r"D:\RPA\photos\2023\10\CMAU4338290\4\F127558.jpg")
items = RapidOCREngine().recognize(source)
candidates = extract_container_candidates(items, "CMAU4338290")
print({"ocr_items": len(items), "best": candidates[0] if candidates else None})
