# -*- coding: utf-8 -*-
"""Price score input, field overrides and independent effectiveness settings."""
import hashlib
import json
from pathlib import Path
import numpy as np
from app.product_table import clean_field_id,product_table_rows,TableFormatError
from app.xlsx_utils import read_workbook_bytes,read_workbook_details,column_name
from app.historical_onboarding import HistoricalProductOnboarding
from tools.product_price_automl import price_dataset,write_json


def ratings_from_workbook(source,known):
    if Path(source).suffix.lower() != ".xlsx":
        return {}, [], {}, {}
    from tools.price_attribute_input import field_index
    sheets = [sheet for sheet in read_workbook_details(Path(source).read_bytes())
              if clean_field_id(sheet["name"]) in ("价格属性评分", "价格属性重要性评分", "价格属性权重")]
    if not sheets:
        return {}, [], {}, {}
    if len(sheets) != 1:
        raise TableFormatError("存在多个价格属性评分工作表，请保留一张明确的评分表。",
                               {"stage": "price_score_sheet", "sheets": [sheet["name"] for sheet in sheets]})
    sheet = sheets[0]
    physical = [(row, number) for row, number in zip(sheet["rows"], sheet["row_numbers"])
                if any(clean_field_id(value) for value in row)]
    if not physical:
        return {}, [], {}, {}
    code_names = {"英文代号", "英文属性代号", "属性英文代号", "价格属性英文代号", "价格属性代号",
                  "属性代号", "英文代码", "field_id", "parameter_id", "attribute_id"}
    score_names = {"价格重要性评分", "价格评分", "价格属性评分", "评分", "分值", "专家评分", "重要性评分", "专家价格评分", "score", "importance_score"}
    token = lambda value: clean_field_id(value).casefold()
    import re
    header_token = lambda value: re.sub(r"\([^)]*\)", "", token(value)).strip()
    header_index = next((i for i,(row,_) in enumerate(physical[:10])
                         if any(header_token(value) in code_names for value in row)), 0)
    headers = [header_token(value) for value in physical[header_index][0]]
    def column(names, fallback=None):
        positions = [i for i,value in enumerate(headers) if value in names]
        if len(positions) > 1:
            raise TableFormatError("%s的表头存在重复的属性代号或评分列。" % sheet["name"],
                                   {"sheet_name": sheet["name"], "error_cell": column_name(positions[1])+str(physical[header_index][1])})
        return positions[0] if positions else fallback
    code_col = column(code_names)
    index = field_index(known)
    body = physical[header_index+1:]
    if code_col is None:
        width = max([len(row) for row,_ in body] or [0])
        hits = [sum(i<len(row) and token(row[i]) in index for row,_ in body) for i in range(width)]
        best = max(hits or [0])
        candidates = [i for i,count in enumerate(hits) if count == best and best]
        if len(candidates) != 1:
            raise TableFormatError("%s未找到明确的英文代号列。请使用“英文代号”或“价格属性英文代号”表头。" % sheet["name"],
                                   {"sheet_name":sheet["name"],"error_cell":"A"+str(physical[header_index][1]),"stage":"price_score_header"})
        code_col = candidates[0]
    score_col = column(score_names, 2 if not any(value in code_names for value in headers) else None)
    type_col = column({"属性类型", "value_type"})
    expert_col = column({"专家编号", "expert_id"})
    group_col = column({"价格属性组", "属性分组", "attribute_group"})
    records, types, groups, collected, seen = [], {}, {}, {}, set()
    def cell(row, col):
        return row[col] if col is not None and col < len(row) else None
    for row, number in body:
        raw_key = cell(row, code_col)
        coordinate = column_name(code_col)+str(number)
        raw_score = cell(row, score_col)
        raw_type, raw_group = cell(row,type_col), cell(row,group_col)
        # Accept a Chinese/English pair of header rows; an actual numeric score
        # on a field named field_id is still data, not a second header.
        if token(raw_key) in {"field_id","parameter_id","attribute_id"} and token(raw_score) in score_names:
            continue
        code_formula = sheet.get("formulas", {}).get(coordinate)
        if code_formula and not code_formula["cached"]:
            raise TableFormatError("%s!%s的英文代号公式没有保存计算结果，请计算保存或粘贴为值。" % (sheet["name"],coordinate),
                                   {"sheet_name":sheet["name"],"error_cell":coordinate,"stage":"price_score_formula"})
        if not token(raw_key):
            continue
        score_coordinate = column_name(score_col)+str(number) if score_col is not None else coordinate
        score_formula = sheet.get("formulas", {}).get(score_coordinate)
        if score_formula and not score_formula["cached"]:
            raise TableFormatError("%s!%s的价格评分公式没有保存计算结果，请计算保存或粘贴为值。" % (sheet["name"],score_coordinate),
                                   {"sheet_name":sheet["name"],"error_cell":score_coordinate,"stage":"price_score_formula"})
        # A code/label-only row contains no instruction and must not turn an
        # otherwise blank optional score table into a validation failure.
        if not any(token(value) for value in (raw_score,raw_type,raw_group)):
            continue
        key = index.get(token(raw_key))
        if key is None:
            import difflib
            suggestions = [index[value] for value in difflib.get_close_matches(token(raw_key),sorted(index),n=3,cutoff=.55)]
            message = "%s!%s引用的英文代号%r不在成品数据表第二行中。" % (sheet["name"],coordinate,raw_key)
            if suggestions:
                message += "相近代号：%s。" % "、".join(suggestions)
            raise TableFormatError(message,{"sheet_name":sheet["name"],"error_cell":coordinate,"field_id":str(raw_key),"suggestions":suggestions})
        expert = clean_field_id(cell(row,expert_col)) or "默认专家"
        if (key,expert) in seen:
            raise TableFormatError("%s!%s：同一专家对同一属性重复评分。" % (sheet["name"],coordinate),
                                   {"sheet_name":sheet["name"],"error_cell":coordinate,"field_id":key})
        seen.add((key,expert))
        if token(raw_score):
            try: value = float(clean_field_id(raw_score))
            except (TypeError,ValueError): value = float("nan")
            if isinstance(raw_score,bool) or not np.isfinite(value) or not 1 <= value <= 5:
                raise TableFormatError("%s!%s必须填写1—5分，不能填写百分比。" % (sheet["name"],score_coordinate),
                                       {"sheet_name":sheet["name"],"error_cell":score_coordinate,"field_id":key})
            records.append({"field_id":key,"raw_field_id":str(raw_key),"expert_id":expert,"score":value,"source_cell":score_coordinate})
            collected.setdefault(key,[]).append(value)
        if token(raw_type):
            value = token(raw_type)
            value = {"数值":"number","numeric":"number","配置类型":"enum","配置":"enum","configuration":"enum","categorical":"enum","布尔":"boolean","枚举":"enum","文本":"text"}.get(value,value)
            if value not in ("number","boolean","enum","text","ip_grade"):
                raise ValueError("价格属性评分中的属性类型无效："+value)
            if key in types and types[key] != value:
                raise ValueError("不同专家声明的属性类型不一致："+key)
            types[key] = value
        if token(raw_group):
            groups[key] = clean_field_id(raw_group)
    return {key:float(np.median(values)) for key,values in collected.items()},records,types,groups


def apply_field_types(data,types):
    definitions={p["parameter_id"]:p for p in data["parameters"]}
    for key,kind in types.items():
        if key not in definitions: raise ValueError("属性类型配置引用未知英文代号："+key)
        p=definitions[key]; values=[]
        for record in data["agreements"]:
            if key not in record["params"]: continue
            raw=record["params"][key]
            if raw is None:continue
            if kind in ("enum","text"):
                value=format(raw,".15g") if isinstance(raw,(float,int)) else str(raw).strip()
            elif kind=="boolean":
                token=str(raw).lower(); lookup={"1":1.,"1.0":1.,"有":1.,"是":1.,"true":1.,"0":0.,"0.0":0.,"无":0.,"否":0.,"false":0.}
                if token not in lookup: raise ValueError("属性%s的布尔取值无效：%s"%(key,raw))
                value=lookup[token]
            else:
                value=float(str(raw).upper().replace("IP",""))
                if not np.isfinite(value): raise ValueError("属性%s包含无效数值。"%key)
            record["params"][key]=value; values.append(value)
        p["value_type"]=kind
        p["allowed_values_json"]=json.dumps(sorted(set(values)),ensure_ascii=False) if kind=="enum" else "[]"
        if kind in ("enum","text"): p.update(min_value=None,max_value=None,search_type="unordered_enum" if kind=="enum" else "auto")
        else: p.update(min_value=min(values) if values else None,max_value=max(values) if values else None,search_type="boolean" if kind=="boolean" else "integer" if kind=="ip_grade" or (values and all(float(x).is_integer() for x in values)) else "continuous")
    return data


def prepare(source,root,settings):
    from tools.product_price_automl_r4 import load_config
    from tools.product_effectiveness_bootstrap import load_config as effect_config
    config_path=root/"config"/"price_automl_r4.json"; price=load_config(config_path)
    raw=Path(source).read_bytes(); table=product_table_rows(Path(source).name,raw,require_dual_header=True)
    write_json(root/"logs"/"input_diagnostics.json",table["diagnostics"])
    analyzed=HistoricalProductOnboarding().analyze(Path(source).name,raw,settings["product_code"],settings["product_name"],price["missing_tokens"],require_dual_header=True)
    known={p["parameter_id"] for p in analyzed["data"]["parameters"]}
    scores,records,types,groups=ratings_from_workbook(source,known)
    # An empty optional table must not erase explicitly configured ratings.
    if scores: price["expert"]["scores"]=scores; price["expert"]["records"]=records
    price["expert"]["field_types"].update(types); price["expert"]["attribute_groups"].update(groups)
    unknown=set(price["expert"]["scores"])-known
    if unknown and price["expert"]["enabled"]: raise ValueError("价格评分配置引用未知代号："+",".join(sorted(unknown)))
    apply_field_types(analyzed["data"],price["expert"]["field_types"])
    frame,y,specs,mappings,business_groups,ids=price_dataset(analyzed["data"],price["validation"]["minimum_groups"])
    effect=effect_config(source,allow_legacy=False); effect["auto_discover_couplings"]=False
    price["resources"]["cache_directory"]=str(root/"outputs"/"oof_cache")
    load_config(overrides=price)
    identity={"input_sha256":hashlib.sha256(raw).hexdigest(),"price_config":price,"effect_config":effect,
              "code":{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in ("tools/product_price_automl_r4.py","services/price_service/r4_estimators.py","tools/product_r4_input.py")}}
    notes=["专家1—5分用于价格属性先验，未评分属性使用中性倍率。","效能偏好独立读取；未提供耦合时不自动发现。"]
    if not price["expert"]["enabled"]: notes[0]="纯数据驱动：专家价格评分不参与训练，原评分保留作对照。"
    if not scores: notes.append("工作簿未提供价格评分，使用JSON评分或数据模型。")
    fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode("utf-8")).hexdigest()
    details={"records":len(frame),"attributes":len(specs),"independent_groups":len(set(business_groups)),"input_sha256":identity["input_sha256"],
             "source_filename":Path(source).name,"fingerprint":fingerprint,"notes":notes,"input_diagnostics":table["diagnostics"],
             "price_expert_scores_count":len(price["expert"]["scores"]),"virtual_data":identity["input_sha256"]==settings.get("demo_input_sha256")}
    return settings,price,effect,details
