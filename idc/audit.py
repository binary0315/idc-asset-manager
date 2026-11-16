# -*- coding: utf-8 -*-
"""audit.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *

from .validation import norm_model


def to_local(ts):
    """SQLite 的 CURRENT_TIMESTAMP 是 UTC，展示时转成本地时间。"""
    try:
        utc=datetime.strptime(str(ts),'%Y-%m-%d %H:%M:%S')
        return (utc+timedelta(seconds=-time.timezone)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(ts or '')

def log_action(c,user_id,action,target='',detail=''):
    """在已有连接/事务里追加一条操作日志。"""
    c.execute('INSERT INTO operation_log(action,target,detail,operated_by) VALUES(?,?,?,?)',
              (action,str(target or '')[:200],str(detail or '')[:500],user_id))

def snapshot_plan(data):
    """按外键依赖顺序返回 (表名, 记录列表)，用于冲突检测与还原。"""
    item=data.get('item') or {}
    return [('items',[item] if item else []),
            ('movements',data.get('movements') or []),
            ('material_spare_cases',data.get('material_spare_cases') or []),
            ('material_spares',data.get('material_spares') or []),
            ('material_spare_history',data.get('material_spare_history') or []),
            ('material_spare_case_history',data.get('material_spare_case_history') or [])]

def restore_snapshot(c,row,user_id):
    """把删除前快照原样写回；任何冲突抛 ValueError，由调用方用 SAVEPOINT 回滚该条。"""
    if row['table_name']!='items':raise ValueError('该类型暂不支持还原')
    try:data=json.loads(row['payload'])
    except Exception:raise ValueError('快照内容无法解析')
    if not isinstance(data,dict):raise ValueError('快照格式无效')
    item=data.get('item') or {}
    if not item.get('id'):raise ValueError('快照缺少物料记录')
    plan=snapshot_plan(data)
    labels={'items':'物料台账','movements':'库存变动明细','material_spare_cases':'备件单',
            'material_spares':'坏件记录','material_spare_history':'坏件状态历史',
            'material_spare_case_history':'备件单历史'}
    for table,recs in plan:
        cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for rec in recs:
            if rec.get('id') is None or 'id' not in cols:continue
            if c.execute(f'SELECT 1 FROM {table} WHERE id=?',(rec['id'],)).fetchone():
                raise ValueError(f'{labels.get(table,table)}的原编号 {rec["id"]} 已被占用，无法原样还原')
    key=norm_model(item.get('model'))
    if c.execute('SELECT 1 FROM items WHERE model_key=? OR model=?',(key,item.get('model'))).fetchone():
        raise ValueError(f'型号「{item.get("model")}」已存在于台账，无法还原')
    for table,recs in plan:
        cols={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for rec in recs:
            keys=[k for k in rec if k in cols]
            if not keys:continue
            sql=f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})"
            c.execute(sql,[rec[k] for k in keys])
    c.execute('UPDATE deletion_audit SET restored_at=CURRENT_TIMESTAMP,restored_by=? WHERE id=?',(user_id,row['id']))
    return item.get('model','')
