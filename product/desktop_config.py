"""Explicit loopback ports; no silent fallback to an unknown port."""
from pathlib import Path
from product.common import read_json,write_json

ROLES=('price','effectiveness','web')
PORT_ROLES=('control','web','price','effectiveness')
DEFAULT_PORTS={role:0 for role in PORT_ROLES}


def template_config():
    return validate_config(read_json(Path(__file__).resolve().parent/'defaults/desktop.json'))


def validate_config(raw):
    if not isinstance(raw,dict) or set(raw)-{'ports','open_browser'}:raise ValueError('Invalid desktop configuration keys')
    ports=raw.get('ports')
    if not isinstance(ports,dict) or set(ports)!=set(DEFAULT_PORTS):raise ValueError('Configure all four desktop ports')
    for role,value in ports.items():
        if isinstance(value,bool) or not isinstance(value,int) or value!=0 and not 1024<=value<=65535:
            raise ValueError('Invalid port for '+role)
    fixed=[p for p in ports.values() if p]
    if len(set(fixed))!=len(fixed):raise ValueError('Desktop ports must be distinct')
    if not isinstance(raw.get('open_browser',True),bool):raise ValueError('open_browser must be boolean')
    return {'ports':dict(ports),'open_browser':raw.get('open_browser',True)}


def load_config(package,data):
    package,data=Path(package),Path(data)
    settings=data/'config/desktop.json'
    if settings.exists():return validate_config(read_json(settings))
    default=package/'defaults/desktop.json'
    if default.exists():
        result=validate_config(read_json(default));write_json(settings,result);return result
    # Older packages and isolated legacy tests used explicit ephemeral ports.
    return {'ports':{role:0 for role in DEFAULT_PORTS},'open_browser':True}
