"""Display labels come from live business data; model inputs and numbers stay untouched."""
import copy
import re

def overlay_labels(value,definitions):
    value=copy.deepcopy(value)
    aliases={}
    def alias(old,new):
        if isinstance(old,str) and old and old!=new:
            aliases[old]=new if old not in aliases or aliases[old]==new else None
    def visit(v):
        if isinstance(v,list):
            for item in v:visit(item)
        elif isinstance(v,dict):
            key=next((v.get(k) for k in ('field_id','field_name','parameter_id','attribute_key','key','target') if isinstance(v.get(k),str) and v.get(k) in definitions),None)
            if key:
                label=definitions[key].get('label') or key
                for name in ('label','field_label','label_zh','parameter_label','attribute_label'):
                    if name in v:alias(v[name],label);v[name]=label
                if 'field_name' in v:v['field_label']=label
            for prefix in ('source','target'):
                source=v.get(prefix) or v.get(prefix+'_id')
                if isinstance(source,str) and source in definitions and prefix+'_label' in v:
                    label=definitions[source].get('label') or source;alias(v[prefix+'_label'],label);v[prefix+'_label']=label
            if isinstance(v.get('field_labels'),dict):
                v['field_labels']={k:(definitions.get(k) or {}).get('label',label) for k,label in v['field_labels'].items()}
            for item in v.values():visit(item)
    visit(value)
    # Older model formats embed display names in advisory strings. Derive the
    # replacements from structured field IDs in this same response, and only
    # rewrite diagnostic text; never touch parameter values or product names.
    aliases={k:v for k,v in aliases.items() if v is not None}
    pattern=re.compile('|'.join(re.escape(k) for k in sorted(aliases,key=len,reverse=True))) if aliases else None
    def messages(v,diagnostic=False):
        if isinstance(v,str):return pattern.sub(lambda m:aliases[m.group()],v) if diagnostic and pattern else v
        if isinstance(v,list):return [messages(x,diagnostic) for x in v]
        if isinstance(v,dict):
            return {k:messages(x,diagnostic or k in ('message','detail','warning','warnings','reason','reasons','hard_risk_reasons','unmet_conditions','extrapolation_warnings')) for k,x in v.items()}
        return v
    return messages(value)
