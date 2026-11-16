# -*- coding: utf-8 -*-
"""idem.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *

from .db import db


def idem_begin(key, user_id, method_path, req_hash, durable):
    """返回 None 表示可以执行；返回 (status, obj) 表示应直接回放/拒绝。
    所有关键写请求都持久化首次结果：显式 UUID 跨时间严格幂等；
    裸请求用 3 秒时间桶去重，同一桶内并发/重试统一回放首次响应。"""
    now=int(time.time()); pending=False
    try:
        with db() as c:
            if now % 16 == 0:
                c.execute('DELETE FROM idempotency WHERE created_at<?',(now-IDEMPOTENCY_TTL,))
            cur=c.execute('INSERT OR IGNORE INTO idempotency(key,user_id,endpoint,request_hash,created_at) VALUES(?,?,?,?,?)',
                          (key,user_id,method_path,req_hash,now))
            if cur.rowcount:return None
            row=c.execute('SELECT status,response,request_hash,endpoint FROM idempotency WHERE key=?',(key,)).fetchone()
            if not row:return (409, {'error':'重复请求，请稍后重试'})
            if row['endpoint'] and row['endpoint']!=method_path:
                return (409, {'error':'相同幂等键被用于不同的接口'})
            if row['request_hash']!=req_hash:
                return (409, {'error':'相同幂等键对应了不同的请求内容'})
            if row['status'] is not None:
                try:return (row['status'], json.loads(row['response']))
                except Exception:return (409, {'error':'重复请求，请稍后重试'})
            pending=True
    except sqlite3.Error:
        return (409, {'error':'重复请求，请稍后重试'})
    if pending:
        # 同一幂等键的并发请求：等待首次请求完成，然后回放它的结果。
        for _ in range(60):
            time.sleep(0.05)
            try:
                with db() as c2:
                    r=c2.execute('SELECT status,response FROM idempotency WHERE key=?',(key,)).fetchone()
                if r and r['status'] is not None:
                    return (r['status'], json.loads(r['response']))
            except Exception: pass
        return (409, {'error':'相同请求处理超时，请稍后刷新页面确认'})

def idem_finish(key, status, response_bytes, durable):
    try:
        with db() as c:
            if status>=500:
                c.execute('DELETE FROM idempotency WHERE key=?',(key,))
            else:
                c.execute('UPDATE idempotency SET status=?,response=? WHERE key=?',
                          (status,response_bytes.decode('utf-8','replace'),key))
    except Exception: pass
