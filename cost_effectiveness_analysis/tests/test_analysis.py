# -*- coding: utf-8 -*-
from cost_effectiveness_analysis.analysis import apply_analysis, baseline_differences, cost_effectiveness, pareto_ids


def test_cost_effectiveness_and_invalid_price():
    assert cost_effectiveness(10, 95) == 9.5
    assert cost_effectiveness(0, 95) is None


def test_pareto_cases():
    assert pareto_ids([{"scheme_id":"A","predicted_price_wan":10,"capability_score":90},
                       {"scheme_id":"B","predicted_price_wan":9,"capability_score":95}]) == ["B"]
    assert set(pareto_ids([{"scheme_id":"A","predicted_price_wan":10,"capability_score":90},
                           {"scheme_id":"B","predicted_price_wan":12,"capability_score":95}])) == {"A","B"}
    assert pareto_ids([{"scheme_id":"A","predicted_price_wan":10,"capability_score":90},
                       {"scheme_id":"B","predicted_price_wan":10,"capability_score":95}]) == ["B"]
    assert set(pareto_ids([{"scheme_id":"A","predicted_price_wan":10,"capability_score":95},
                           {"scheme_id":"B","predicted_price_wan":10,"capability_score":95}])) == {"A","B"}


def test_missing_model_result_is_not_pareto():
    items, frontier = apply_analysis([
        {"scheme_id":"A","predicted_price_wan":10,"capability_score":95},
        {"scheme_id":"B","predicted_price_wan":None,"capability_score":99},
    ])
    assert frontier == ["A"]
    assert items[1]["pareto"] is None
    assert items[1]["cost_effectiveness"] is None


def test_baseline_differences():
    items = [
        {"scheme_id":"A","predicted_price_wan":10,"capability_score":90,"cost_effectiveness":9},
        {"scheme_id":"B","predicted_price_wan":12,"capability_score":95,"cost_effectiveness":7.5},
    ]
    delta = baseline_differences(items, "A")["B"]
    assert delta == {"predicted_price_wan":2.0,"capability_score":5.0,"cost_effectiveness":-1.5}
