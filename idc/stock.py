# -*- coding: utf-8 -*-
"""stock.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *



def stock_available(c,item_id):
    r=c.execute('''SELECT COALESCE(SUM(CASE WHEN kind='入库' THEN qty ELSE 0 END),0)
                          -COALESCE(SUM(CASE WHEN kind='领用/安装' THEN qty ELSE 0 END),0)
                          -COALESCE(SUM(CASE WHEN kind='报废' THEN qty ELSE 0 END),0) n
                   FROM movements WHERE item_id=?''',(item_id,)).fetchone()
    return r['n'] if r else 0

def assert_stock(c,item_ids,action='该操作'):
    """事务内调用：任一受影响物料出现负库存就抛错，由外层回滚整笔操作。"""
    for iid in {int(x) for x in item_ids if x is not None}:
        n=stock_available(c,iid)
        if n<0:
            raise ValueError(f'可用库存不足：{action}会使库存变为 {n}，不允许负库存，本次操作已撤销')

def predict_negative(c,movement_id,new_item_id,new_kind,new_qty):
    """编辑明细前预演：把旧明细从汇总里剔除、加上新明细，是否会出现负库存。"""
    old=c.execute('SELECT item_id,kind,qty FROM movements WHERE id=?',(movement_id,)).fetchone()
    if not old:return False
    def delta(k,q):return q if k=='入库' else -q
    for iid in {old['item_id'],new_item_id}:
        total=stock_available(c,iid)
        if old['item_id']==iid:total-=delta(old['kind'],old['qty'])
        if iid==new_item_id:total+=delta(new_kind,new_qty)
        if total<0:return True
    return False
