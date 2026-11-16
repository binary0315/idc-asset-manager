# -*- coding: utf-8 -*-
"""validation.py：由 app.py 按职责拆分而来，行为保持一致。"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta
from .config import *
from .state import *



def norm_header(value):
    return ''.join(ch.lower() for ch in str(value or '').strip() if ch.isalnum() or '\u3400' <= ch <= '\u9fff')

def norm_model(value):
    """型号唯一键：全角转半角 + 去首尾空格 + 大小写折叠。"""
    return unicodedata.normalize('NFKC', str(value or '')).strip().casefold()

def clean_text(value, field='notes'):
    text=str(value or '').strip()
    text=''.join(ch for ch in text if ch=='\t' or ord(ch)>=32)
    limit=FIELD_LIMITS.get(field,500)
    if len(text)>limit:raise ValueError(f'{FIELD_LABELS.get(field,field)}不能超过 {limit} 个字符')
    return text

def parse_planned(value, label='计划数量', allow_decimal_zero=False):
    """统一计划数量解析：0..MAX_QTY 的严格整数，拒绝布尔、浮点与非数字文本。
    allow_decimal_zero 仅供 Excel 单元格兼容 5.0 这类写法。"""
    if isinstance(value, bool):
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数，不接受布尔值')
    if isinstance(value, int):
        q = value
    elif isinstance(value, float):
        if allow_decimal_zero and math.isfinite(value) and value.is_integer():
            q = int(value)
        else:
            raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数，不接受小数')
    elif isinstance(value, str):
        s = value.strip().replace(',', '')
        pattern = r'\d+(?:\.0+)?' if allow_decimal_zero else r'\d+'
        if not re.fullmatch(pattern, s):
            raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
        q = int(float(s)) if '.' in s else int(s)
    else:
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
    if q < 0 or q > MAX_QTY:
        raise ValueError(f'{label}必须是 0 到 {MAX_QTY:,} 之间的整数')
    return q

def validate_server_fields(room, rack, sn):
    """服务器三个字段统一限长（与 Excel 导入口径一致），失败抛 ValueError。"""
    limits = SERVER_FIELD_LIMITS
    labels = {'room': '机房/房间', 'rack': '机柜位置', 'sn': 'SN'}
    for k, v in (('room', room), ('rack', rack), ('sn', sn)):
        if not v:
            raise ValueError('机房、机柜位置和 SN 均为必填项')
        if len(v) > limits[k]:
            raise ValueError(f'{labels[k]}不能超过 {limits[k]} 个字符')
    return room, rack, sn

def validate_item_fields(d, partial=False):
    """统一校验并规范化台账字段；partial=True 用于 PATCH。失败抛 ValueError。"""
    out={}
    for k in ITEM_FIELDS:
        if partial and k not in d:continue
        if k=='planned':
            out['planned']=parse_planned(d.get('planned',0))
        else:
            v=clean_text(d.get(k),k)
            if k in ('model','description') and not v:raise ValueError(f'{FIELD_LABELS[k]}不能为空')
            if k=='category' and v not in ('光模块','线缆'):raise ValueError('类别只能是光模块或线缆')
            out[k]=v
    if 'model' in out:out['model_key']=norm_model(out['model'])
    return out

def normalize_category(value):
    text=norm_header(value)
    if any(x in text for x in ('光模块','opticalmodule','transceiver')):return '光模块'
    if any(x in text for x in ('线缆','线材','cable','patchcord','网线')):return '线缆'
    if value in ('光模块','线缆'):return value
    return ''

def parse_qty(value, label='数量'):
    """严格正整数：拒绝小数、布尔、非数字字符串、零/负数与超大值。"""
    if isinstance(value, bool):raise ValueError(f'{label}必须是正整数，不接受布尔值')
    if isinstance(value, float):raise ValueError(f'{label}不允许小数')
    if isinstance(value, int):q=value
    elif isinstance(value, str):
        s=value.strip()
        if not re.fullmatch(r'\d+',s):raise ValueError(f'{label}必须是正整数')
        q=int(s)
    else:raise ValueError(f'{label}必须是正整数')
    if q<=0 or q>MAX_QTY:raise ValueError(f'{label}必须是 1 到 {MAX_QTY:,} 之间的整数')
    return q
