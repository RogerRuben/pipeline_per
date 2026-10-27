# -*- coding: utf-8 -*-
import json

import pytest

from cost_effectiveness_analysis.config import load_config


def test_independent_price_output_config_and_environment_override(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "cost_effectiveness_analysis.json").write_text(json.dumps({
        "database": {"path": "data/test.db"},
        "price_output": {"unit": "yuan", "scale": 0.5},
    }), encoding="utf-8")
    config = load_config(tmp_path)
    assert config["price_output"] == {"unit": "yuan", "scale": 0.5}

    monkeypatch.setenv("COST_EFFECTIVENESS_PRICE_OUTPUT_UNIT", "thousand_yuan")
    monkeypatch.setenv("COST_EFFECTIVENESS_PRICE_OUTPUT_SCALE", "2")
    assert load_config(tmp_path)["price_output"] == {"unit": "thousand_yuan", "scale": 2.0}


def test_invalid_price_output_config_fails_fast(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "cost_effectiveness_analysis.json").write_text(
        '{"price_output":{"unit":"yuan","scale":0}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="大于0"):
        load_config(tmp_path)
