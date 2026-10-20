# -*- coding: utf-8 -*-
"""Check local imports against the ZIP overlay plus the actual base package."""
import ast
from pathlib import Path


def validate_local_imports(payload,base,optional_modules=()):
    base=Path(base);missing=[];optional_missing=[];checked=0
    def exists(name):
        rel=name.replace('.','/')
        return (rel+'.py' in payload or (base/(rel+'.py')).is_file() or
                any(key.startswith(rel+'/') for key in payload) or (base/rel).is_dir())
    for path,data in payload.items():
        if not path.endswith('.py'):continue
        package=path.split('/')[:-1]
        for node in ast.walk(ast.parse(data.decode('utf-8-sig'),filename=path)):
            names=[]
            if isinstance(node,ast.Import):names=[item.name for item in node.names]
            elif isinstance(node,ast.ImportFrom):
                if node.level:
                    parents=package[:len(package)-node.level+1];name='.'.join(parents+([node.module] if node.module else []))
                else:name=node.module or ''
                if name:names=[name]
            for name in names:
                if name.split('.')[0] not in ('app','tools','services','automl_pipeline'):continue
                checked+=1
                if not exists(name):
                    row={'source':path,'line':node.lineno,'module':name}
                    (optional_missing if name in optional_modules else missing).append(row)
    if missing:raise ValueError('PATCH contains unresolved local imports: '+repr(missing))
    return {'checked_imports':checked,'missing':missing,'optional_unavailable':optional_missing,'scope':'direct local imports of every patched Python file against payload + base package'}
