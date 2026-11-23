# -*- coding: utf-8 -*-
"""What a customer package starts, opens, and is allowed to serve.

full keeps the historical package. The other profiles omit unused model files
and do not start unused services. The frozen EXE is still the shared engine.
"""

FULL = 'full'
PRICE = 'price_only'
RECOMMENDATION = 'recommendation'

_PRICE_ALLOWED = (
    '/price', '/model-library', '/admin', '/login', '/login.html',
    '/styles.css', '/ui-theme.css', '/price.js', '/price_ensemble.js', '/price_ensemble.css',
    '/business-metadata.js', '/model-library.js', '/admin.js', '/login.js',
    '/favicon.png', '/launcher.svg', '/launcher.ico',
    '/api/price-workbench', '/api/model-library', '/api/admin', '/api/health',
    '/api/desktop-status', '/api/business-labels', '/api/auth',
)
_RECOMMEND_ALLOWED = (
    '/', '/index.html', '/recommendation', '/model-library', '/admin', '/login', '/login.html',
    '/styles.css', '/ui-theme.css', '/app.js', '/business-metadata.js', '/model-library.js',
    '/admin.js', '/login.js', '/favicon.png', '/launcher.svg', '/launcher.ico',
    '/api/recommend', '/api/generation', '/api/generation-tasks', '/api/evaluate', '/api/improve',
    '/api/bootstrap', '/api/agreements', '/api/saved', '/api/requirements', '/api/final-decisions',
    '/api/save-scheme', '/api/clear-live-generated', '/api/model-library', '/api/admin',
    '/api/health', '/api/desktop-status', '/api/business-labels', '/api/auth',
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


def model_roles(manifest_or_name):
    return tuple(role for role in describe(manifest_or_name)['models'] if role in ('price', 'effectiveness'))


def required_release_keys(manifest):
    if not isinstance(manifest, dict) or 'delivery_profile' not in manifest:
        return PROFILES[FULL]['required_release']
    return describe(manifest)['required_release']


def open_path(manifest):
    return describe(manifest)['open_path']


def _allowed(path, prefix):
    if prefix == '/':
        return path == '/'
    if prefix == '/price':
        return path in ('/price', '/price/', '/price.html')
    return path == prefix or path.startswith(prefix + '/') or path.startswith(prefix + '.')


def request_allowed(profile, path):
    name = normalize(profile) if profile else FULL
    if name == FULL:
        return True
    path = (path or '/').split('?', 1)[0] or '/'
    allowed = _PRICE_ALLOWED if name == PRICE else _RECOMMEND_ALLOWED
    return any(_allowed(path, prefix) for prefix in allowed)


def data_token(product_code, profile):
    name = normalize(profile) if profile else FULL
    if name == FULL:
        return str(product_code)
    return str(product_code) + '|' + name


def adjust_page(profile, relative, data):
    name = normalize(profile) if profile else FULL
    if name == FULL or relative not in ('price.html', 'index.html', 'model-library.html', 'admin.html'):
        return data
    text = data.decode('utf-8')
    if name == PRICE:
        if relative == 'price.html':
            text = text.replace(
                '<a class="button secondary" href="/portal" target="_top">系统导航</a>'
                '<a class="button secondary" href="/effectiveness">简易效能评价</a>'
                '<a class="button secondary" href="/recommendation">智能推荐</a>',
                '<a class="button secondary" href="/model-library">模型库</a>')
        elif relative == 'model-library.html':
            text = text.replace(
                '<a class="button" href="/portal" target="_top">返回系统导航</a>',
                '<a class="button" href="/price" target="_top">返回价格预测</a>')
            text = text.replace(
                '<option value="effectiveness">效能模型（原导出包 / 快照）</option>',
                '')
        elif relative == 'admin.html':
            text = text.replace(
                '<a class="button secondary" href="/portal">系统导航</a>'
                '<a class="button secondary" href="/">返回推荐系统</a>',
                '<a class="button secondary" href="/price">返回价格预测</a>'
                '<a class="button secondary" href="/model-library">模型库</a>')
    elif name == RECOMMENDATION:
        if relative == 'index.html':
            text = text.replace('<a class="button secondary" href="/portal" target="_top">系统导航</a>', '')
        elif relative == 'model-library.html':
            text = text.replace(
                '<a class="button" href="/portal" target="_top">返回系统导航</a>',
                '<a class="button" href="/" target="_top">返回推荐系统</a>')
        elif relative == 'admin.html':
            text = text.replace('<a class="button secondary" href="/portal">系统导航</a>', '')
    return text.encode('utf-8')
