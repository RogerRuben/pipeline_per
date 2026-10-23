(function () {
  "use strict";
  function escape(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  function percent(value) { return value == null ? "未指定" : (Number(value) * 100).toFixed(2) + "%"; }
  function price(value) { return Number(value).toFixed(4); }
  function table(headers, rows) {
    return '<div class="ensemble-table-scroll"><table class="ensemble-table"><thead><tr>' + headers.map(function (x) { return "<th>" + escape(x) + "</th>"; }).join("") + "</tr></thead><tbody>" + rows.map(function (row) { return "<tr>" + row.map(function (x) { return "<td>" + escape(x) + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table></div>";
  }
  window.PriceEnsemble = { render: function (explanation) {
    var panel = document.getElementById("priceEnsemblePanel");
    if (!panel) return;
    if (!explanation || !(explanation.model_contributions || []).length) { panel.classList.add("hidden"); return; }
    var html = "<h3>价格融合解释</h3><p>" + escape(explanation.sample_warning) + "</p>";
    if (explanation.quality_status) {
      html += '<p><strong>' + (explanation.quality_status === 'qualified_development' ? '开发目标达标' : '未达标试用草稿') + '</strong></p>';
      var scores = [['部署模型训练集', explanation.training_metrics], ['开发集（参与选择）', explanation.development_metrics], ['固定外层留出集', explanation.holdout_metrics]];
      if (explanation.reference_metrics) scores.splice(1, 0, ['保留的V3基线开发集', explanation.reference_metrics]);
      if (explanation.reference_training_metrics) scores.splice(1, 0, ['保留的V3基线训练集', explanation.reference_training_metrics]);
      html += table(['评估范围', '平均百分比误差', 'R方'], scores.map(function (item) {
        var score = item[1];
        return [item[0], !score || score.mape_percent == null ? '未提供' : Number(score.mape_percent).toFixed(3) + '%', !score ? '未提供' : score.r2 == null ? '不可定义' : Number(score.r2).toFixed(5)];
      }));
    }
    if (explanation.price_risk) {
      var risk = explanation.price_risk;
      html += "<p>历史可信域：" + (risk.is_extrapolation ? "存在外推" : "范围内") + "；模型分歧 " + price(risk.model_disagreement_wan) + " 万元；保守排序价格 " + price(risk.recommendation_price_wan) + " 万元。此风险量不是已校准置信区间。</p>";
    }
    html += table(["模型", "融合权重", "价格贡献（万元）"], explanation.model_contributions.map(function (x) { return [x.label, percent(x.weight), price(x.weighted_contribution_wan)]; }));
    if ((explanation.price_attribute_scores || []).length) {
      html += "<h4>价格属性评分（1—5分）</h4>";
      if (explanation.expert_scores_enabled === false) html += "<p>当前为纯数据驱动模型，以下评分仅供对照，未参与价格训练。</p>";
      html += table(["属性", "价格重要性", "价格模型内部倍率", "来源", "训练使用情况"], explanation.price_attribute_scores.map(function (x) {
        var used = x.used_in_training !== false;
        var source = x.score_source === "example" ? "示例预设" : x.specified ? "已提供评分" : x.score_source === "supplied_default" ? "附带价格先验" : x.score_source === "default" ? "未评分，按" + x.score + "分" : "未评分，使用中性";
        return [x.label, String(x.score) + "分", x.enabled === false ? "未启用" : !used ? "未用于训练" : x.multiplier_display || Number(x.multiplier_full_strength).toFixed(3), source,
          !used ? (x.exclusion_reason || "未参与价格训练") : x.enabled === false ? "未启用" : "参与价格训练"];
      }));
    }
    if ((explanation.expert_groups || []).length) {
      html += "<h4>专家模型份额与学习结果</h4>" + table(["模型族", "专家先验", "学习权重", "偏离"], explanation.expert_groups.map(function (x) { return [x.label || x.name, percent(x.weight), percent(x.learned_weight), x.deviation == null ? "未指定" : (x.deviation * 100).toFixed(2) + "个百分点"]; }));
    }
    (explanation.configuration_details || []).forEach(function (detail) {
      html += "<details><summary>" + escape(detail.label) + "：" + detail.seed_instances + "个训练成员，校准 " + Number(detail.calibration).toFixed(4) + "倍</summary>";
      var weightedMembers = (detail.members || []).filter(function (member) { return member.weight != null; });
      if (weightedMembers.length) html += table(['子模型', '组合内份额', '总融合份额', '价格贡献（万元）'], weightedMembers.map(function (member) {
        var totalWeight = member.weight * detail.ensemble_weight;
        return [member.label, percent(member.weight), percent(totalWeight), price(totalWeight * member.predicted_price_wan)];
      }));
      (detail.members || []).forEach(function (member, index) {
        if ((member.tree_path || []).length) html += table(["分裂属性", "条件", "阈值或类别", "价格评分"], member.tree_path.map(function (step) { return [step.label, step.encoding === "one_hot" ? (step.branch === "大于" ? "属于" : "不属于") : step.branch, step.category || price(step.threshold), step.expert_score]; }));
        if ((member.attribute_terms || []).length) {
          var terms = member.attribute_terms.slice().sort(function (a, b) { return Math.abs(b.term) - Math.abs(a.term); }).slice(0, 12);
          html += "<p>成员" + (index + 1) + "的主要字段项（" + escape(terms[0].space === "log_price" ? "对数价格" : "万元") + "）</p>";
          html += table(["属性", "本次字段项"], terms.map(function (term) { return [term.label, price(term.term)]; }));
        }
      });
      html += "</details>";
    });
    (explanation.engineering_components || []).forEach(function (x) {
      html += "<details><summary>" + escape(x.label) + "：工程基准 " + price(x.base_price_wan) + " 万元</summary>";
      html += table(["工程属性", "成本贡献（万元）"], [["基础成本", price(x.intercept_wan)]].concat(x.attribute_contributions.map(function (piece) { return [piece.label, price(piece.contribution_wan)]; }), [["残差修正", price(x.residual_correction_wan)]]));
      html += "</details>";
    });
    html += "<p>" + escape(explanation.interval_note) + "</p>";
    panel.innerHTML = html; panel.classList.remove("hidden");
  }};
  document.addEventListener("DOMContentLoaded", function () {
    var panel = document.createElement("section");
    panel.id = "priceEnsemblePanel"; panel.className = "effect-detail-card hidden";
    document.getElementById("priceResults").appendChild(panel);
  });
})();
