# -*- coding: utf-8 -*-
"""db.py：按职责拆分，显式依赖，无自导入/循环。"""

import sqlite3
import json
from .config import get_db_path
from .validation import norm_model
def db():
    p=get_db_path()
    try:
        # IDC_DB 可指向自定义目录：自动补建父目录，避免运维配错时只看到原始 traceback。
        p.parent.mkdir(parents=True, exist_ok=True)
        c=sqlite3.connect(p, timeout=15)
    except (OSError, sqlite3.OperationalError) as e:
        raise RuntimeError(f'无法打开数据库文件 {p}：请检查 IDC_DB 指向的目录是否存在且可写（{e}）') from e
    c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def init():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, pw_hash BLOB NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, can_view INTEGER NOT NULL DEFAULT 1, can_edit INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, user_id INTEGER, endpoint TEXT, request_hash TEXT, status INTEGER, response TEXT, created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency(created_at);
        CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, category TEXT NOT NULL DEFAULT '光模块', model TEXT NOT NULL, model_key TEXT, description TEXT NOT NULL, supplier TEXT DEFAULT '', planned INTEGER NOT NULL DEFAULT 0, location TEXT DEFAULT '', owner TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(model));
        CREATE TABLE IF NOT EXISTS deletion_audit(id INTEGER PRIMARY KEY, table_name TEXT NOT NULL, record_id INTEGER, payload TEXT NOT NULL, deleted_by INTEGER, deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, restored_at TEXT, restored_by INTEGER);
        CREATE TABLE IF NOT EXISTS operation_log(id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT '', operated_by INTEGER, operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS movements(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), kind TEXT NOT NULL CHECK(kind IN ('入库','领用/安装','报废')), qty INTEGER NOT NULL CHECK(qty>0), moved_at TEXT NOT NULL, location TEXT DEFAULT '', owner TEXT DEFAULT '', handler TEXT DEFAULT '', reference TEXT DEFAULT '', notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY, room TEXT NOT NULL, rack TEXT NOT NULL, sn TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_components(id INTEGER PRIMARY KEY, server_sn TEXT NOT NULL REFERENCES servers(sn) ON UPDATE CASCADE ON DELETE CASCADE, node_sn TEXT NOT NULL, node_u INTEGER NOT NULL CHECK(node_u BETWEEN 1 AND 46), kp_name TEXT NOT NULL, bmc_mac TEXT DEFAULT '', UNIQUE(server_sn,node_u), UNIQUE(server_sn,node_sn));
        CREATE TABLE IF NOT EXISTS server_component_operations(id INTEGER PRIMARY KEY, server_sn TEXT NOT NULL, node_u INTEGER NOT NULL, action TEXT NOT NULL, responsible TEXT NOT NULL, old_data TEXT NOT NULL, new_data TEXT NOT NULL, operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_spares(id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL UNIQUE REFERENCES server_component_operations(id), server_sn TEXT NOT NULL, node_sn TEXT NOT NULL, node_u INTEGER NOT NULL, kp_name TEXT NOT NULL, bmc_mac TEXT DEFAULT '', room TEXT DEFAULT '', rack TEXT DEFAULT '', status TEXT NOT NULL DEFAULT '待检测', responsible TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS server_spare_status_history(id INTEGER PRIMARY KEY, spare_id INTEGER NOT NULL REFERENCES server_spares(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, responsible TEXT NOT NULL, notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_server_spares_status ON server_spares(status,created_at);
        CREATE TABLE IF NOT EXISTS material_spare_cases(id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, qty INTEGER NOT NULL CHECK(qty>0), returned_qty INTEGER NOT NULL DEFAULT 0 CHECK(returned_qty>=0), scrapped_qty INTEGER NOT NULL DEFAULT 0 CHECK(scrapped_qty>=0), status TEXT NOT NULL DEFAULT '待RMA', responsible TEXT NOT NULL, notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS material_spare_case_history(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES material_spare_cases(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, qty_returned INTEGER NOT NULL DEFAULT 0, qty_scrapped INTEGER NOT NULL DEFAULT 0, responsible TEXT NOT NULL, notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_material_spare_cases_item_status ON material_spare_cases(item_id,status);
        CREATE TABLE IF NOT EXISTS material_spares(id INTEGER PRIMARY KEY, movement_id INTEGER REFERENCES movements(id) ON DELETE CASCADE, unit_index INTEGER NOT NULL DEFAULT 1, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, serial_number TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '待处理', responsible TEXT DEFAULT '', notes TEXT DEFAULT '', created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(movement_id,unit_index));
        CREATE TABLE IF NOT EXISTS material_spare_history(id INTEGER PRIMARY KEY, spare_id INTEGER NOT NULL REFERENCES material_spares(id) ON DELETE CASCADE, from_status TEXT NOT NULL, to_status TEXT NOT NULL, old_serial_number TEXT NOT NULL DEFAULT '', serial_number TEXT NOT NULL DEFAULT '', responsible TEXT DEFAULT '', notes TEXT DEFAULT '', operated_by INTEGER REFERENCES users(id), operated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_material_spares_status ON material_spares(status,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_material_spares_item_sn ON material_spares(item_id,serial_number) WHERE serial_number<>'';
        CREATE INDEX IF NOT EXISTS idx_movements_item ON movements(item_id,kind);
        CREATE INDEX IF NOT EXISTS idx_servers_room_rack ON servers(room,rack);
        CREATE INDEX IF NOT EXISTS idx_server_components_sn ON server_components(server_sn,node_u);
        ''')
        # 历史表改用 server_id：只回填一次，匹配不到服务器的记录直接标记为已归档(-1)，
        # 之后即使用同一个 SN 新建服务器，也不会被错误关联回新机器。
        for _t in ('server_component_operations','server_spares'):
            _cols={r['name'] for r in c.execute(f'PRAGMA table_info({_t})')}
            if 'server_id' not in _cols:
                c.execute(f'ALTER TABLE {_t} ADD COLUMN server_id INTEGER')
            c.execute(f'''UPDATE {_t} SET server_id=COALESCE(
                (SELECT id FROM servers WHERE servers.sn={_t}.server_sn), -1)
                WHERE server_id IS NULL''')
        audit_cols={r['name'] for r in c.execute('PRAGMA table_info(deletion_audit)')}
        for col,decl in (('restored_at','TEXT'),('restored_by','INTEGER')):
            if col not in audit_cols:c.execute(f'ALTER TABLE deletion_audit ADD COLUMN {col} {decl}')
        cols={r['name'] for r in c.execute('PRAGMA table_info(items)')}
        if 'category' not in cols: c.execute("ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT '光模块'")
        # 型号归一化唯一键：全角/半角、大小写、首尾空格视为同一型号。
        if 'model_key' not in cols: c.execute('ALTER TABLE items ADD COLUMN model_key TEXT')
        for row in c.execute("SELECT id,model FROM items WHERE model_key IS NULL OR model_key=''").fetchall():
            c.execute('UPDATE items SET model_key=? WHERE id=?',(norm_model(row['model']),row['id']))
        dup=c.execute('SELECT model_key,COUNT(*) n FROM items GROUP BY model_key HAVING n>1').fetchall()
        if not dup:c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_items_model_key ON items(model_key)')
        else:print('警告：存在归一化后重复的型号，未建立 model_key 唯一索引：',[r['model_key'] for r in dup])
        movement_cols={r['name'] for r in c.execute('PRAGMA table_info(movements)')}
        if 'owner' not in movement_cols:
            c.execute("ALTER TABLE movements ADD COLUMN owner TEXT DEFAULT ''")
            c.execute("UPDATE movements SET owner=COALESCE(handler,'')")
        # Backfill legacy scrap transactions once. Each scrapped unit becomes an
        # individual pending spare record whose serial number can be entered later.
        for movement in c.execute("SELECT id,item_id,qty,created_by FROM movements WHERE kind='报废'").fetchall():
            for unit in range(1,movement['qty']+1):
                c.execute("INSERT OR IGNORE INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)",(movement['id'],unit,movement['item_id'],movement['created_by']))
        # Recover older recorded part replacements that predate automatic spare
        # registration. The operation log already contains the removed NODE_SN.
        missing_replacements=c.execute('''SELECT o.* FROM server_component_operations o
            LEFT JOIN server_spares s ON s.operation_id=o.id
            WHERE o.action='部件更换' AND s.id IS NULL AND o.server_id>0 ORDER BY o.id''').fetchall()
        for operation in missing_replacements:
            try:
                old_data=json.loads(operation['old_data']);new_data=json.loads(operation['new_data'])
                old_sn=str(old_data.get('node_sn','')).strip();new_sn=str(new_data.get('node_sn','')).strip()
                node_u=int(old_data.get('node_u',operation['node_u']))
                if not old_sn or old_sn==new_sn:continue
                place=c.execute('SELECT room,rack FROM servers WHERE sn=?',(operation['server_sn'],)).fetchone()
                room,rack=(place['room'],place['rack']) if place else ('','')
                spare=c.execute('''INSERT OR IGNORE INTO server_spares(operation_id,server_sn,server_id,node_sn,node_u,kp_name,bmc_mac,room,rack,status,responsible,created_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(operation['id'],operation['server_sn'],operation['server_id'],old_sn,node_u,str(old_data.get('kp_name','')),str(old_data.get('bmc_mac','')),room,rack,'待检测',operation['responsible'],operation['operated_by']))
                if spare.lastrowid:
                    c.execute('''INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by)
                        VALUES(?,?,?,?,?,?)''',(spare.lastrowid,'历史更换记录','待检测',operation['responsible'],'根据既有部件更换操作记录补入服务器备件',operation['operated_by']))
            except (TypeError,ValueError,json.JSONDecodeError):continue
        duplicates=c.execute('SELECT model FROM items GROUP BY model HAVING COUNT(*)>1').fetchall()
        if not duplicates:c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_items_model_unique ON items(model)')
