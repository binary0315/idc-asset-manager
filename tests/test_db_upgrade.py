# -*- coding: utf-8 -*-
"""数据库回归：init() 幂等、IDC_DB 父目录自动创建、旧库结构升级。

用临时库，绝不碰项目根目录的真实数据库。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(str(REPO))


class TestDbInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='idc_dbupgrade_')
        self.db_path = os.path.join(self.tmp, 'missing', 'parent', 'isolated.sqlite3')
        self._old = os.environ.get('IDC_DB')
        os.environ['IDC_DB'] = self.db_path
        # 强制重新导入 db 模块，确保 get_db_path 读到新环境变量
        for name in ('app',) + tuple('idc.' + m for m in
                                     ('config', 'db', 'crypto', 'excel', 'idem', 'stock', 'audit')):
            sys.modules.pop(name, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop('IDC_DB', None)
        else:
            os.environ['IDC_DB'] = self._old

    def test_parent_dir_created_and_tables_ready(self):
        """IDC_DB 指向不存在的多级目录：自动建父目录并建表，不再裸 traceback。"""
        from idc.db import init
        init()
        self.assertTrue(os.path.isdir(os.path.dirname(self.db_path)), '父目录应被自动创建')
        self.assertTrue(os.path.exists(self.db_path), '数据库文件应生成')
        with sqlite3.connect(self.db_path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ('users', 'items', 'movements', 'servers', 'server_components',
                  'server_spares', 'operation_log', 'deletion_audit', 'idempotency'):
            self.assertIn(t, tables, '缺少表 %s' % t)

    def test_init_is_idempotent(self):
        """重复 init 不报错、不丢数据。"""
        from idc.db import db, init
        init()
        with db() as c:
            c.execute("INSERT INTO items(model,model_key,description) VALUES('M1','m1','D')")
        init()  # 第二次
        with db() as c:
            models = [r[0] for r in c.execute('SELECT model FROM items')]
        self.assertEqual(models, ['M1'])

    def test_legacy_schema_upgrade(self):
        """模拟旧版库：items 缺 category/model_key、deletion_audit 缺 restored_at，init 后自动补列。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.executescript('''
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt BLOB NOT NULL, pw_hash BLOB NOT NULL, is_admin INTEGER DEFAULT 0,
                    can_view INTEGER DEFAULT 1, can_edit INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE items(id INTEGER PRIMARY KEY, model TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL, supplier TEXT DEFAULT '', planned INTEGER DEFAULT 0,
                    location TEXT DEFAULT '', owner TEXT DEFAULT '', notes TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO items(model,description) VALUES('OLD-1','旧记录');
                CREATE TABLE deletion_audit(id INTEGER PRIMARY KEY, table_name TEXT NOT NULL,
                    payload TEXT NOT NULL);
            ''')
        from idc.db import db, init
        init()
        with db() as c:
            cols = {r['name'] for r in c.execute('PRAGMA table_info(items)')}
            self.assertIn('category', cols)
            self.assertIn('model_key', cols)
            acols = {r['name'] for r in c.execute('PRAGMA table_info(deletion_audit)')}
            self.assertIn('restored_at', acols)
            row = c.execute("SELECT model,model_key,category FROM items WHERE model='OLD-1'").fetchone()
            self.assertEqual(row['model_key'], 'old-1')
            self.assertEqual(row['category'], '光模块')

    def test_bad_path_gives_chinese_error(self):
        """父目录是文件时无法建目录，应抛中文 RuntimeError 而非裸 sqlite 异常。"""
        blocker = os.path.join(self.tmp, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as f:
            f.write('not a dir')
        os.environ['IDC_DB'] = os.path.join(blocker, 'sub', 'db.sqlite3')
        from idc.db import db
        import sqlite3 as _sq
        with self.assertRaises(RuntimeError) as ctx:
            db()
        self.assertIn('IDC_DB', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
