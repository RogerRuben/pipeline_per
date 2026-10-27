# -*- coding: utf-8 -*-
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_frontend_has_required_sections_and_no_direct_model_fetch():
    html = (ROOT / "cost_effectiveness_analysis" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "cost_effectiveness_analysis" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "cost_effectiveness_analysis" / "static" / "styles.css").read_text(encoding="utf-8")
    for element_id in ("schemeList", "sourceFilter", "selectAllBtn", "invertBtn", "clearSelectionBtn", "paretoChart", "rankingChart", "detailRows", "selectedDetail"):
        assert 'id="%s"' % element_id in html
    assert "baseline-btn" in js
    assert "selectAllCurrent" in js and "invertCurrent" in js and "clearSelection" in js
    assert ".rank-bar{display:block" in css
    assert "127.0.0.1:18101" not in js
    assert "127.0.0.1:18102" not in js
    assert 'api("/api/analyze"' in js
