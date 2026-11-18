# -*- coding: utf-8 -*-
"""Recommendation UI only exposes the center effectiveness score by default."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")


class RecommendationEffectivenessPresentationTests(unittest.TestCase):
    def test_candidate_cards_keep_price_and_center_score_but_not_ce_metric(self):
        start = SCRIPT.index("function renderResults(")
        stop = SCRIPT.index("function renderPages(", start)
        cards = SCRIPT[start:stop]
        self.assertIn("item.predicted_price_wan", cards)
        self.assertIn("<span>中心效能分数</span>", cards)
        self.assertIn("n(item.capability_score,3)", cards)
        self.assertNotIn('<div class="metric"><span>效费比</span>', cards)

    def test_detail_only_shows_center_effectiveness(self):
        self.assertIn('<span>中心效能分数</span><b id="detailCapability">', HTML)
        self.assertIn('<span>预测价格</span><b id="detailPrice">', HTML)
        self.assertIn('<section class="metric-board compact-score-view">', HTML)
        self.assertIn('.metric-board.compact-score-view{grid-template-columns:repeat(2,minmax(0,1fr))}', CSS)
        self.assertIn('<div class="hidden"><span>效费比</span><b id="detailCE">', HTML)
        self.assertIn('q("detailCapability").textContent=n(e.capability_score,3)', SCRIPT)

    def test_optional_improvement_stays_visible(self):
        self.assertIn(
            '<section class="insight-card"><div class="insight-title"><span>↗</span><h3>优化建议</h3>',
            HTML,
        )
        self.assertIn('id="improveBtn"', HTML)
        self.assertIn('id="improvementPanel"', HTML)

    def test_additional_effectiveness_panels_are_hidden_not_deleted(self):
        for title, panel in (
            ("调整建议", "guidancePanel"),
            ("范围提示", "rangeDiagnosticsPanel"),
            ("需求与风险", "riskPanel"),
            ("经验参考", "couplingPanel"),
        ):
            self.assertIn('<h3>%s</h3>' % title, HTML)
            self.assertIn('id="%s"' % panel, HTML)
        self.assertIn('class="insight-card guidance hidden"', HTML)
        for mark, title in (("ⓘ", "范围提示"), ("!", "需求与风险"), ("⌁", "经验参考")):
            self.assertIn(
                '<section class="insight-card hidden"><div class="insight-title"><span>%s</span><h3>%s</h3>' %
                (mark, title),
                HTML,
            )


if __name__ == "__main__":
    unittest.main()
