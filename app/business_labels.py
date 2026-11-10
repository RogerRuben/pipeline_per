"""Display labels come from live business data; model inputs and numbers stay untouched."""
import copy

def overlay_labels(value,definitions):
    value=copy.deepcopy(value)
    def visit(v):
        if isinstance(v,list):
            for item in v:visit(item)
        elif isinstance(v,dict):
            key=next((v.get(k) for k in ('field_id','field_name','parameter_id','attribute_key','key','target') if isinstance(v.get(k),str) and v.get(k) in definitions),None)
            if key:
                label=definitions[key].get('label') or key
                for name in ('label','field_label','label_zh','parameter_label','attribute_label'):
                    if name in v:v[name]=label
                if 'field_name' in v:v['field_label']=label
            for prefix in ('source','target'):
                source=v.get(prefix) or v.get(prefix+'_id')
                if isinstance(source,str) and source in definitions and prefix+'_label' in v:v[prefix+'_label']=definitions[source].get('label') or source
            if isinstance(v.get('field_labels'),dict):
                v['field_labels']={k:(definitions.get(k) or {}).get('label',label) for k,label in v['field_labels'].items()}
            for item in v.values():visit(item)
    visit(value);return value
