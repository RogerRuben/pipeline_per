"""A request-local SQLite snapshot; existing Store codecs, no Store initialization."""
import hashlib
import json
import sqlite3
from pathlib import Path

from ..store import Store
from .errors import DeepPriceError


class _BorrowedConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, *args):
        return self.conn.execute(*args)

    def close(self):
        pass


class _SnapshotCodec(Store):
    def __init__(self, conn):
        self.conn = conn

    def connect(self, path=None):
        return _BorrowedConnection(self.conn)


class BusinessSnapshot:
    def __init__(self, store, timeout=5):
        self.conn = sqlite3.connect(Path(store.db_path).resolve().as_uri() + '?mode=ro', uri=True,
                                    timeout=max(0.01, min(timeout, 5)))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA query_only=ON')
        try:
            self.version = self.conn.execute('PRAGMA data_version').fetchone()[0]
            self.conn.execute('BEGIN')
            releases = self.conn.execute("SELECT * FROM product_releases WHERE status='active'").fetchall()
            if len(releases) != 1:
                raise DeepPriceError('unavailable', '需要且仅允许一个已激活业务发布版本', 503)
            self.release = dict(releases[0])
            self.product_code = self.release['product_code']
            products = self.conn.execute('SELECT * FROM products WHERE enabled=1').fetchall()
            if len(products) != 1 or products[0]['product_code'] != self.product_code:
                raise DeepPriceError('unavailable', '激活版本与当前业务产品不一致', 503)
            self.definitions = [dict(row) for row in self.conn.execute(
                'SELECT * FROM parameter_definitions WHERE enabled=1 ORDER BY display_order,parameter_id')]
            self.products = []
            for row in self.conn.execute("SELECT * FROM agreements WHERE enabled=1 AND product_code=? "
                                         "AND agreement_source IN ('historical','imported') ORDER BY agreement_id", (self.product_code,)):
                self.products.append({'product_id': row['agreement_id'], 'name': row['agreement_name'],
                                      'parameters': json.loads(row['params_json'])})
            self.codec = _SnapshotCodec(self.conn)
            # Keep the read transaction through parameter encoding, then release
            # it before HTTP. Retain this connection for data_version detection.
        except Exception:
            self.conn.close()
            raise

    def release_read_lock(self):
        self.conn.commit()

    def assert_unchanged(self):
        self.release_read_lock()
        if self.conn.execute('PRAGMA data_version').fetchone()[0] != self.version:
            raise DeepPriceError('version_conflict', '业务数据在分析期间发生变化，请刷新后重试', 409)

    def close(self):
        self.conn.close()

    @property
    def fingerprint(self):
        data = {'release': self.release, 'fields': self.definitions, 'products': self.products}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8')).hexdigest()
