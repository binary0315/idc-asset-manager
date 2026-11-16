# -*- coding: utf-8 -*-
"""crypto.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *

from .db import db


def pw_hash(password,salt): return hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,310000)

def json_bytes(obj): return json.dumps(obj,ensure_ascii=False).encode('utf-8')
DUMMY_SALT = b'IDC_TIMING_EQUALIZER_'[:16]
DUMMY_HASH = pw_hash('__timing_equalizer__', DUMMY_SALT)

def validate_username(name):
    """用户名统一 3–40 字符，拒绝控制字符（调用方先 strip）。"""
    if len(name)<3 or len(name)>40:
        raise ValueError('用户名需为 3 到 40 个字符')
    if any(ord(ch)<32 or ord(ch)==127 for ch in name):
        raise ValueError('用户名不能包含控制字符')
    return name

def verify_credentials(username, password):
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
    salt=bytes(u['salt']) if u else DUMMY_SALT
    expected=bytes(u['pw_hash']) if u else DUMMY_HASH
    ok=hmac.compare_digest(pw_hash(password,salt),expected)
    return dict(u) if (u and ok) else None

def login_block_seconds(ip, username):
    """返回还需等待的秒数；0 表示放行。"""
    now=int(time.time()); key=(ip, username)
    with LOGIN_LOCK:
        rec=LOGIN_FAILS.get(key)
        if not rec:return 0
        if now-rec[1]>LOGIN_WINDOW:
            LOGIN_FAILS.pop(key,None);return 0
        return max(0, rec[2]-now)

def login_record_fail(ip, username):
    now=int(time.time()); key=(ip, username)
    with LOGIN_LOCK:
        rec=LOGIN_FAILS.get(key)
        if not rec or now-rec[1]>LOGIN_WINDOW:rec=[0,now,0]
        rec[0]+=1
        if rec[0]>=LOGIN_MAX_FAILS:
            lock=min(LOGIN_BASE_LOCK*(2**(rec[0]-LOGIN_MAX_FAILS)),LOGIN_MAX_LOCK)
            rec[2]=now+lock
        LOGIN_FAILS[key]=rec
        if len(LOGIN_FAILS)>2000:
            for k,v in list(LOGIN_FAILS.items()):
                if now-v[1]>LOGIN_WINDOW:LOGIN_FAILS.pop(k,None)

def login_clear(ip, username):
    with LOGIN_LOCK: LOGIN_FAILS.pop((ip,username),None)
