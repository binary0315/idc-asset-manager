# -*- coding: utf-8 -*-
"""state.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *


IMPORT_TOKENS = {}
MOVEMENT_IMPORT_TOKENS = {}
SERVER_IMPORT_TOKENS = {}
SERVER_COMPONENT_IMPORT_TOKENS = {}
LOGIN_FAILS = {}          # (ip, username) -> [失败次数, 首次失败时间, 解锁时间]
LOGIN_LOCK = threading.Lock()
IDEMPOTENCY_LOCK = threading.Lock()
