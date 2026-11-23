# -*- coding: utf-8 -*-
"""What a customer package starts, opens, and is allowed to serve.

full keeps the historical package. The other profiles omit unused model files
and do not start unused services. The frozen EXE is still the shared engine.
"""

FULL = 'full'
PRICE = 'price_only'
RECOMMENDATION = 'recommendation'

_PRICE_BLOCKED = (
    '/effectiveness', '/recommendation', '/price/deep', '/portal',
    '/cost-effectiveness', '/api/evaluate', '/api/effectiveness',
)
_RECOMMENDATION_BLOCKED = (
    '/price', '/effectiveness', '/portal', '/cost-effectiveness', '/api/price-workbench',
)

PROFILES = {
    FULL: {
        'id': FULL,
        'label': '完整系统',
        'services': ('price', 'effectiveness', 'web'),
        'required_release': ('price_model', 'effectiveness_model', 'business_package', 'evaluation_metadata'),
        'open_path': '/portal',
        'models': ('price', 'effectiveness', 'business'),
    },
    PRICE: {
        'id': PRICE,
        'label': '仅价格预测',
        'services': ('price', 'web'),
        'required_release': ('price_model', 'business_package', 'evaluation_metadata'),
        'open_path': '/price',
        'models': ('price', 'business'),
    },
    RECOMMENDATION: {
        'id': RECOMMENDATION,
        'label': '仅推荐系统',
        'services': ('price', 'effectiveness', 'web'),
        'required_release': ('price_model', 'effectiveness_model', 'business_package', 'evaluation_metadata'),
        'open_path': '/',
        'models': ('price', 'effectiveness', 'business'),
    },
}

SHARED_ENGINE_NOTE = (
    '本包复用完整冻结引擎。EXE 内仍包含公共界面资源和算法模块；'
    '未放入本包的外部模型不会启动对应服务，也不作为本包的交付文件。'
)


def normalize(value):
    if value in (None, ''):
        return FULL
    text = str(value).strip()
    aliases = {'1': FULL, '2': PRICE, '3': RECOMMENDATION, 'price': PRICE, 'recommendation': RECOMMENDATION}
    name = aliases.get(text, text)
    if name not in PROFILES:
        raise ValueError('未知交付类型：' + text)
    return name


def describe(manifest_or_name):
    if isinstance(manifest_or_name, dict):
        raw = manifest_or_name.get('delivery_profile') or {}
        name = raw.get('id') if isinstance(raw, dict) else raw
        if not name:
            name = FULL
    else:
        name = manifest_or_name
    try:
        name = normalize(name)
    except ValueError:
        name = FULL
    return dict(PROFILES[name])


def service_roles(manifest):
    return describe(manifest)['services']


def required_release_keys(manifest):
    if not isinstance(manifest, dict) or 'delivery_profile' not in manifest:
        return PROFILES[FULL]['required_release']
    return describe(manifest)['required_release']


def open_path(manifest):
    return describe(manifest)['open_path']


def _blocked(path, prefix):
    return path == prefix or path.startswith(prefix + '/')


def request_allowed(profile, path):
    name = normalize(profile) if profile else FULL
    if name == FULL:
        return True
    path = (path or '/').split('?', 1)[0] or '/'
    if name == PRICE:
        if path in ('/', '/index.html', '/recommendation'):
            return False
        return not any(_blocked(path, prefix) for prefix in _PRICE_BLOCKED)
    if path == '/price.html' or _blocked(path, '/price') or path.startswith('/price.'):
        return False
    return not any(_blocked(path, prefix) for prefix in _RECOMMENDATION_BLOCKED if prefix != '/price')


def data_token(product_code, profile):
    name = normalize(profile) if profile else FULL
    if name == FULL:
        return str(product_code)
    return str(product_code) + '|' + name


def adjust_page(profile, relative, data):
    name = normalize(profile) if profile else FULL
    if name == FULL or not relative:
        return data
    text = data.decode('utf-8')
    if name == PRICE and relative == 'price.html':
        text = text.replace(
            '<a class="button secondary" href="/portal" target="_top">系统导航</a>'
            '<a class="button secondary" href="/effectiveness">简易效能评价</a>'
            '<a class="button secondary" href="/recommendation">智能推荐</a>',
            '<a class="button secondary" href="/model-library">模型库</a>')
    elif name == RECOMMENDATION and relative == 'index.html':
        text = text.replace('<a class="button secondary" href="/portal" target="_top">系统导航</a>', '')
    return text.encode('utf-8')
