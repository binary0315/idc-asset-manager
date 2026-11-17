# -*- coding: utf-8 -*-
"""IDC 资产管理系统：HTTP 服务入口与请求路由。

业务实现按职责拆分在 idc/ 包中；此处集中导出常用符号以保持兼容
（测试脚本与旧调用方会从 app 顶层访问这些名字）。
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import sqlite3, json, secrets, hashlib, hmac, os, time, urllib.parse, mimetypes, zipfile, io, posixpath, re, uuid, unicodedata, threading, math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import date, datetime, timedelta

from idc.config import (
    ARCHIVED_SERVER_ID,
    CALC_FIELDS,
    FIELD_LIMITS,
    FRAME_OPTIONS,
    GB_SPECS,
    HEADER_SCAN_ROWS,
    HOST,
    IDEMPOTENCY_AUTO_BUCKET,
    IDEMPOTENCY_TTL,
    IDEMPOTENT_AUTO_POST,
    IMPORT_LIMIT,
    IMPORT_ROW_LIMIT,
    ITEM_FIELDS,
    LOGIN_BASE_LOCK,
    LOGIN_MAX_FAILS,
    MATERIAL_SPARE_STATUSES,
    MAX_QTY,
    MOVEMENT_FIELDS,
    PORT,
    ROOT,
    SERVER_FIELDS,
    SERVER_FIELD_LIMITS,
    SERVER_SPARE_STATUSES,
    SESSION_SECONDS,
    SPARE_NOTES_LIMIT,
    get_db_path,
)
from idc.state import (
    IMPORT_TOKENS,
    MOVEMENT_IMPORT_TOKENS,
    SERVER_COMPONENT_IMPORT_TOKENS,
    SERVER_IMPORT_TOKENS,
)
from idc.db import db, db_ctx, init
from idc.crypto import (pw_hash, json_bytes, validate_username, verify_credentials,
                        login_block_seconds, login_record_fail, login_clear,
                        DUMMY_SALT, DUMMY_HASH)
from idc.validation import (norm_header, norm_model, clean_text, parse_planned,
                            validate_server_fields, validate_item_fields, parse_qty,
                            normalize_category)
from idc.excel import (match_header, xlsx_matrix, item_matrix, active_column_indexes,
                       header_candidates, auto_detect_header, build_item_preview,
                       parse_item_import, remap_item_import, movement_header,
                       movement_date, inbound_kind, movement_header_candidates,
                       auto_detect_movement_header, select_edited_rows,
                       build_movement_preview, parse_movement_import,
                       remap_movement_import, gb_header_match, auto_detect_gb_header,
                       gb_context, parse_server_row, parse_component_row,
                       build_gb_preview, parse_gb_import, remap_gb_import,
                       make_xlsx, prune_import_tokens)
from idc.idem import idem_begin, idem_finish
from idc.stock import stock_available, assert_stock, predict_negative
from idc.audit import to_local, log_action, snapshot_plan, restore_snapshot


class Handler(BaseHTTPRequestHandler):
    server_version='IDCInventory/1.0'
    def log_message(self,fmt,*args): print('%s - %s' % (self.address_string(),fmt%args))
    def handle_one_request(self):
        self._idem_key=None; self._idem_durable=False; self._idem_result=None; self._idem_replay=False
        try:super().handle_one_request()
        except (BrokenPipeError,ConnectionResetError):self.close_connection=True
        except Exception as e:
            print('[unhandled] %s: %s'%(type(e).__name__,e))
            try:self.send(500,{'error':'服务器内部错误，请稍后重试'})
            except Exception:self.close_connection=True
        finally:
            if getattr(self,'_idem_key',None):
                st,body=getattr(self,'_idem_result',None) or (500,b'{}')
                try:idem_finish(self._idem_key,st,body,self._idem_durable)
                except Exception:pass
    def do_PUT(self): self.send(405,{'error':'不支持的请求方法；新增用 POST，修改用 PATCH，删除用 DELETE'})
    def send_error(self,code,message=None,explain=None):
        self.close_connection=True
        try:self.send(code,{'error':message or '请求无法处理'})
        except Exception:pass
    def send(self,status,obj=None,headers=None,content_type='application/json; charset=utf-8',cache='no-store'):
        body=obj if isinstance(obj,bytes) else json_bytes(obj or {})
        # RFC 7230 §3.3.2：1xx/204 不得带 Content-Length；304 不得带与 200 不一致的长度（代理会误判资源为空）。
        self.send_response(status); self.send_header('Content-Type',content_type)
        if status not in (204,304): self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control',cache)
        # 单文件前端含内联 script/style 与 onclick，CSP 必须允许 inline；frame-ancestors 用 'self' 以兼容内置预览面板。
        ancestors = " frame-ancestors 'self';" if FRAME_OPTIONS else ''
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; form-action 'self';" + ancestors + " base-uri 'self'; object-src 'none'")
        if FRAME_OPTIONS: self.send_header('X-Frame-Options',FRAME_OPTIONS)
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Permissions-Policy','geolocation=(), microphone=(), camera=(), payment=()')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        if getattr(self,'_idem_replay',False):
            self.send_header('X-Idempotency-Replay','1')
        if getattr(self,'_idem_key',None) and getattr(self,'_idem_result',None) is None:
            self._idem_result=(status,body)
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(body)
    def do_HEAD(self):
        """HEAD 与 GET 同路径同响应头，但不写 body（供健康检查/代理探测）。"""
        return self.do_GET()
    def body(self):
        if getattr(self,'_body_done',False):
            if self._body_error:raise ValueError(self._body_error)
            return self._body_cache
        self._body_done=True; self._body_error=None; self._body_cache=None
        n=int(self.headers.get('Content-Length','0') or 0)
        if n>1_000_000:
            self._body_error='请求内容过大'; raise ValueError(self._body_error)
        try: d=json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            self._body_error='请求内容不是有效的 JSON'; raise ValueError(self._body_error)
        if not isinstance(d,dict):
            self._body_error='请求内容必须是 JSON 对象'; raise ValueError(self._body_error)
        self._body_cache=d
        return d

    def idem_guard(self,p,method):
        """写接口统一幂等入口。返回 True 表示响应已发出、调用方应立即 return。
        带显式 X-Idempotency-Key 时覆盖全部写接口（防双击/网络重试/多标签）；
        无键时仅对 /api/movements 做短时并发兜底，并在回放响应加标记头。"""
        if method not in ('POST','PATCH','DELETE'):return False
        raw=self.headers.get('X-Idempotency-Key','').strip()
        if raw and not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}',raw):
            self.send(400,{'error':'X-Idempotency-Key 格式无效：需为 8 到 128 位字母、数字、点、下划线、冒号或连字符'})
            return True
        valid_key=bool(raw)
        # 认证端点、预览/重映射（二进制或只读）不参与幂等
        if valid_key:
            if p in ('/api/login','/api/setup','/api/logout') or 'preview' in p or 'remap' in p:return False
        else:
            if method!='POST' or p not in IDEMPOTENT_AUTO_POST:return False
        # 必须走完整 CSRF 校验：否则缺 CSRF 的请求会命中已完成记录并回放 200，绕过鉴权。
        a=self.auth(csrf=True)
        if a is False or a is None:return False   # 交给业务 handler 返回 401/403
        u=a
        try:d=self.body()
        except Exception:return False   # 非法 JSON 交给业务 handler 返回 400
        try:
            req_hash=hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        except Exception:req_hash=hashlib.sha256(repr(d).encode('utf-8','replace')).hexdigest()
        method_path=method+' '+p
        if valid_key:
            key='idem:%s:%s:%s'%(u['id'],method_path,raw); durable=True
        else:
            bucket=int(time.time())//IDEMPOTENCY_AUTO_BUCKET
            key='auto:%s:%s:%s:%d'%(u['id'],method_path,req_hash,bucket); durable=True
        verdict=idem_begin(key,u['id'],method_path,req_hash,durable)
        if verdict is not None:
            self._idem_replay=True       # 回放/冲突响应带标记，客户端可识别
            self.send(verdict[0],verdict[1])
            return True
        self._idem_key=key; self._idem_durable=durable
        return False
    def auth(self,csrf=False):
        token=''
        for part in self.headers.get('Cookie','').split(';'):
            if part.strip().startswith('sid='): token=part.strip()[4:]
        if not token: return None
        th=hashlib.sha256(token.encode()).hexdigest()
        with db_ctx() as c:
            row=c.execute('SELECT s.csrf,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.active=1',(th,)).fetchone()
            if not row or row['expires']<int(time.time()):
                c.execute('DELETE FROM sessions WHERE token_hash=?',(th,)); return None
            if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),row['csrf']): return False
            return dict(row)
    def require(self,perm=None,csrf=True):
        u=self.auth(csrf)
        if u is None: self.send(401,{'error':'请先登录'}); return None
        if u is False: self.send(403,{'error':'请求校验失败，请刷新页面后重试'}); return None
        if perm and not u['is_admin'] and not u[perm]: self.send(403,{'error':'当前账号没有此操作权限'}); return None
        return u
    def do_GET(self):
        p=urllib.parse.urlparse(self.path).path
        if p=='/':
            try:
                body=(ROOT/'index.html').read_bytes()
                # 静态资源版本号跟随文件修改时间：改动后浏览器自动失效缓存，
                # 磁盘上的 index.html 保持无版本号引用（便于离线读取/测试）。
                try:
                    ver=max(int((ROOT/'static'/'app.css').stat().st_mtime),
                            int((ROOT/'static'/'app.js').stat().st_mtime))
                    if b'/static/app.css?v=' not in body:
                        body=body.replace(b'/static/app.css',
                                          ('/static/app.css?v=%d' % ver).encode())
                        body=body.replace(b'/static/app.js',
                                          ('/static/app.js?v=%d' % ver).encode())
                except OSError: pass
            except OSError: return self.send(500,{'error':'页面文件缺失'})
            return self.send(200,body,content_type='text/html; charset=utf-8')
        if p.startswith('/static/'):
            # 静态资源：只允许 app.css / app.js，防路径穿越
            name=p[len('/static/'):]
            if name not in ('app.css','app.js'):return self.send(404,{'error':'资源不存在'})
            fp=ROOT/'static'/name
            try:
                body=fp.read_bytes(); etag='"'+str(int(fp.stat().st_mtime))+'"'
            except OSError: return self.send(404,{'error':'资源不存在'})
            ctype='text/css; charset=utf-8' if name.endswith('.css') else 'application/javascript; charset=utf-8'
            inm=self.headers.get('If-None-Match','')
            if inm=='*' or inm==etag:
                return self.send(304,b'',headers={'ETag':etag},content_type=ctype,cache='public, max-age=3600')
            return self.send(200,body,headers={'ETag':etag},content_type=ctype,cache='public, max-age=3600')
        if p=='/api/export/items.xlsx':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c:
                rows=c.execute('''SELECT i.category,i.model,i.description,i.supplier,i.planned,
                COALESCE(SUM(CASE WHEN m.kind='入库' THEN m.qty ELSE 0 END),0) received,
                COALESCE(SUM(CASE WHEN m.kind='领用/安装' THEN m.qty ELSE 0 END),0) used,
                COALESCE(SUM(CASE WHEN m.kind='报废' THEN m.qty ELSE 0 END),0) damaged,
                i.location,i.owner,i.notes FROM items i LEFT JOIN movements m ON m.item_id=i.id GROUP BY i.id ORDER BY i.id''').fetchall()
            data=[]
            for r in rows:
                d=dict(r);d['available']=d['received']-d['used']-d['damaged'];d['not_received']=max(d['planned']-d['received'],0)
                data.append([d['category'],d['model'],d['description'],d['supplier'],d['planned'],d['received'],d['used'],d['damaged'],d['available'],d['not_received'],d['location'],d['owner'],d['notes']])
            headers=['类别','型号','名称/规格','供应商/品牌','计划数量','累计入库','已领用/安装','损坏/报废','可用库存','未到数量','存放位置','负责人','备注']
            payload=make_xlsx('物料台账',headers,data,[14,20,30,22,12,12,14,14,12,12,20,16,28])
            return self.send(200,payload,headers={'Content-Disposition':'attachment; filename=IDC_material_ledger.xlsx'},content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        if p=='/api/export/movements.xlsx':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c: rows=c.execute('SELECT m.moved_at,m.kind,i.category,i.model,m.qty,m.location,m.owner,m.notes FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.moved_at,m.id').fetchall()
            headers=['日期','业务类型','类别','型号','数量','位置/去向','负责人','备注']
            payload=make_xlsx('库存变动明细',headers,[list(r) for r in rows],[14,16,14,20,12,24,18,32])
            return self.send(200,payload,headers={'Content-Disposition':'attachment; filename=IDC_inventory_movements.xlsx'},content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        if p=='/api/status':
            with db_ctx() as c: setup=c.execute('SELECT count(*) FROM users').fetchone()[0]==0
            return self.send(200,{'setup_required':setup})
        if p=='/api/config':
            # 开放化第一步：把目前写死在后端/前端的业务口径集中到一个只读接口。
            # 后续改成可配置字典时，只要本接口返回值不变，前端与导入逻辑都无需修改。
            return self.send(200,{
                'item_categories':['光模块','线缆'],
                'item_fields':list(ITEM_FIELDS),'field_limits':dict(FIELD_LIMITS),
                'material_spare_statuses':list(MATERIAL_SPARE_STATUSES),
                'server_spare_statuses':list(SERVER_SPARE_STATUSES),
                'rack_u_slots':46,'qty_max':MAX_QTY,
                'planned_min':0,'planned_max':MAX_QTY,
                'server_field_limits':dict(SERVER_FIELD_LIMITS),
                'idempotency':{'header':'X-Idempotency-Key','replay_header':'X-Idempotency-Replay',
                               'scope':'method+path+key','ttl_seconds':IDEMPOTENCY_TTL,
                               'login_max_fails':LOGIN_MAX_FAILS,'login_base_lock_seconds':LOGIN_BASE_LOCK},
                'import':{'header_scan_rows':HEADER_SCAN_ROWS,
                          'row_limit':IMPORT_ROW_LIMIT,'page_size':50},
                'configurable':False})
        if p=='/api/me':
            u=self.auth()
            if not u: return self.send(401,{'error':'请先登录'})
            return self.send(200,{'id':u['id'],'username':u['username'],'is_admin':bool(u['is_admin']),'can_view':bool(u['can_view']),'can_edit':bool(u['can_edit']),'csrf':u['csrf']})
        if p=='/api/items':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c:
                rows=c.execute('''SELECT i.*,
                COALESCE(SUM(CASE WHEN m.kind='入库' THEN m.qty ELSE 0 END),0) received,
                COALESCE(SUM(CASE WHEN m.kind='领用/安装' THEN m.qty ELSE 0 END),0) used,
                COALESCE(SUM(CASE WHEN m.kind='报废' THEN m.qty ELSE 0 END),0) damaged
                FROM items i LEFT JOIN movements m ON m.item_id=i.id GROUP BY i.id ORDER BY i.id DESC''').fetchall()
            out=[]
            for x in rows:
                d=dict(x); d['available']=d['received']-d['used']-d['damaged']; d['not_received']=max(d['planned']-d['received'],0); out.append(d)
            return self.send(200,{'items':out})
        if p=='/api/spares':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c:
                rows=c.execute('''SELECT i.id,i.category,i.model,i.description,
                    COALESCE(m.received,0) received,COALESCE(m.used,0) used,COALESCE(m.damaged,0) damaged,
                    COALESCE(r.waiting,0) rma_waiting,COALESCE(r.processing,0) rma_processing
                    FROM items i
                    LEFT JOIN (SELECT item_id,SUM(CASE WHEN kind='入库' THEN qty ELSE 0 END) received,
                        SUM(CASE WHEN kind='领用/安装' THEN qty ELSE 0 END) used,
                        SUM(CASE WHEN kind='报废' THEN qty ELSE 0 END) damaged FROM movements GROUP BY item_id) m ON m.item_id=i.id
                    LEFT JOIN (SELECT item_id,
                        SUM(CASE WHEN status='待RMA' THEN 1 ELSE 0 END) waiting,
                        SUM(CASE WHEN status='RMA处理中' THEN 1 ELSE 0 END) processing
                        FROM material_spares GROUP BY item_id) r ON r.item_id=i.id
                    WHERE i.category IN ('光模块','线缆') ORDER BY i.category,i.model''').fetchall()
                materials=[]
                for row in rows:
                    d=dict(row);d['ledger_available']=d['received']-d['used']-d['damaged'];d['rma_hold']=0;d['available']=d['ledger_available']
                    if d['available']>0 or d['damaged']>0:materials.append(d)
                material_spares=[dict(r) for r in c.execute('''SELECT s.*,i.category,i.model,i.description,m.moved_at,m.location AS source_location,m.owner AS source_owner,
                    u.username AS operator_username FROM material_spares s JOIN items i ON i.id=s.item_id
                    LEFT JOIN movements m ON m.id=s.movement_id LEFT JOIN users u ON u.id=s.created_by
                    ORDER BY CASE WHEN s.status='待处理' THEN 0 ELSE 1 END,s.created_at DESC,s.id DESC''').fetchall()]
                material_history=c.execute('''SELECT h.*,u.username AS operator_username FROM material_spare_history h
                    LEFT JOIN users u ON u.id=h.operated_by ORDER BY h.id DESC''').fetchall()
                server_spares=[dict(r) for r in c.execute('''SELECT s.*,u.username AS operator_username,
                    sv.sn AS current_sn,
                    CASE WHEN s.server_id IS NULL OR s.server_id=? OR sv.id IS NULL THEN 1 ELSE 0 END AS orphan
                    FROM server_spares s LEFT JOIN users u ON u.id=s.created_by
                    LEFT JOIN servers sv ON sv.id=s.server_id
                    ORDER BY s.created_at DESC,s.id DESC''',(ARCHIVED_SERVER_ID,)).fetchall()]
                history=c.execute('''SELECT h.*,u.username AS operator_username FROM server_spare_status_history h
                    LEFT JOIN users u ON u.id=h.operated_by ORDER BY h.id DESC''').fetchall()
            material_history_by_spare={}
            for h in material_history:material_history_by_spare.setdefault(h['spare_id'],[]).append(dict(h))
            for spare in material_spares:spare['history']=material_history_by_spare.get(spare['id'],[])
            history_by_spare={}
            for h in history:history_by_spare.setdefault(h['spare_id'],[]).append(dict(h))
            for spare in server_spares:spare['history']=history_by_spare.get(spare['id'],[])
            return self.send(200,{'materials':materials,'material_spares':material_spares,'material_statuses':MATERIAL_SPARE_STATUSES,'server_spares':server_spares,'statuses':SERVER_SPARE_STATUSES})
        if p=='/api/movements':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c: rows=c.execute('SELECT m.id,m.moved_at,m.kind,i.category,i.model,m.qty,m.location,m.owner,m.notes FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.moved_at DESC,m.id DESC LIMIT 1000').fetchall()
            return self.send(200,{'movements':[dict(x) for x in rows]})
        if p=='/api/servers':
            u=self.require('can_view',False)
            if not u:return
            with db_ctx() as c: rows=c.execute('SELECT id,room,rack,sn FROM servers ORDER BY room,rack,sn').fetchall()
            return self.send(200,{'servers':[dict(x) for x in rows]})
        if p.startswith('/api/servers/') and p.endswith('/components'):
            u=self.require('can_view',False)
            if not u:return
            try:server_id=int(p.split('/')[3])
            except:return self.send(400,{'error':'服务器编号无效'})
            with db_ctx() as c:
                server=c.execute('SELECT id,room,rack,sn FROM servers WHERE id=?',(server_id,)).fetchone()
                if not server:return self.send(404,{'error':'服务器不存在'})
                components=c.execute('SELECT id,node_sn,node_u,kp_name,bmc_mac FROM server_components WHERE server_sn=? ORDER BY node_u DESC',(server['sn'],)).fetchall()
                operations=c.execute('''SELECT o.id,o.node_u,o.action,o.responsible,o.old_data,o.new_data,o.operated_at,u.username AS operator_username
                    FROM server_component_operations o LEFT JOIN users u ON u.id=o.operated_by
                    WHERE o.server_id=? ORDER BY o.id DESC''',(server['id'],)).fetchall()
            return self.send(200,{'server':dict(server),'components':[dict(x) for x in components],'operations':[dict(x) for x in operations]})
        if p=='/api/recycle' or p.startswith('/api/recycle/'):
            u=self.require(csrf=False)
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可查看回收站'})
            detail_id=None
            if p!='/api/recycle':
                try:detail_id=int(p.rsplit('/',1)[1])
                except:return self.send(400,{'error':'记录编号无效'})
            with db_ctx() as c:
                if detail_id is not None:
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(detail_id,)).fetchone()
                    if not row:return self.send(404,{'error':'回收站记录不存在'})
                    try:snap=json.loads(row['payload'])
                    except Exception:snap={}
                    return self.send(200,{'id':row['id'],'deleted_at':to_local(row['deleted_at']),
                                          'restored_at':to_local(row['restored_at']) if row['restored_at'] else None,
                                          'snapshot':snap})
                rows=[dict(r) for r in c.execute('''SELECT a.*,u.username AS operator FROM deletion_audit a
                    LEFT JOIN users u ON u.id=a.deleted_by ORDER BY a.id DESC LIMIT 500''').fetchall()]
                occupied={r['id'] for r in c.execute('SELECT id FROM items')}
                occupied_keys={r['model_key'] for r in c.execute('SELECT model_key FROM items')}
                records=[]
                for r in rows:
                    try:data=json.loads(r['payload'])
                    except Exception:data={}
                    data=data if isinstance(data,dict) else {}
                    it=data.get('item') or {}
                    key=norm_model(it.get('model'))
                    reason=''
                    if r['restored_at']:reason='已还原'
                    elif r['table_name']!='items':reason='该类型暂不支持还原'
                    elif it.get('id') in occupied:reason='原物料编号已被占用'
                    elif key in occupied_keys:reason='型号已被占用'
                    records.append({'id':r['id'],'table_name':r['table_name'],
                        'record_id':r['record_id'],'deleted_at':to_local(r['deleted_at']),
                        'restored_at':to_local(r['restored_at']) if r['restored_at'] else None,
                        'operator':r['operator'],
                        'category':it.get('category',''),'model':it.get('model',''),
                        'description':it.get('description',''),
                        'movement_count':len(data.get('movements') or []),
                        'spare_count':len(data.get('material_spares') or []),
                        'can_restore':not reason,'reason':reason,
                        'snapshot':data})
                logs=[]
                for r in c.execute('''SELECT l.*,u.username AS operator FROM operation_log l
                    LEFT JOIN users u ON u.id=l.operated_by ORDER BY l.id DESC LIMIT 300''').fetchall():
                    x=dict(r);x['operated_at']=to_local(x.get('operated_at'));logs.append(x)
            return self.send(200,{'records':records,'logs':logs})
        if p=='/api/users':
            u=self.require(csrf=False)
            if not u:return
            if not u['is_admin']: return self.send(403,{'error':'仅管理员可管理账号'})
            with db_ctx() as c: rows=c.execute('SELECT id,username,is_admin,can_view,can_edit,active,created_at FROM users ORDER BY id').fetchall()
            return self.send(200,{'users':[dict(x) for x in rows]})
        return self.send(404,{'error':'没有这个页面'})
    def do_POST(self):
        p=urllib.parse.urlparse(self.path).path
        if self.idem_guard(p,'POST'):return
        if p=='/api/material-spare-cases':return self.send(410,{'error':'备件由库存变动明细中的报废记录自动生成'})
        if p=='/api/import-server-components-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            try:result=parse_gb_import(self.rfile.read(n),u['id'],'components')
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-server-components':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=SERVER_COMPONENT_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'部件清单预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is not None:
                try:
                    if not isinstance(edited,list) or not edited or len(edited)>len(batch['rows']):
                        raise ValueError('预览中没有可导入的数据，或行数超过预览行数；请重新上传')
                    ctx=gb_context('components');clean=[]
                    for i,raw in enumerate(edited,1):
                        vals={'server_sn':str(raw.get('server_sn','')).strip(),
                              'node_sn':str(raw.get('node_sn','')).strip(),
                              'node':str(raw.get('node_u','')).strip(),
                              'kp_name':str(raw.get('kp_name','')).strip(),
                              'bmc_mac':str(raw.get('bmc_mac','')).strip()}
                        rec,issues=parse_component_row(lambda f:vals.get(f,''),i,ctx)
                        if issues:raise ValueError(f'第 {i} 行：'+'；'.join(issues))
                        clean.append(rec)
                    batch['rows']=clean;batch['serials']=sorted(ctx['serials'])
                except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db_ctx() as c:
                    current={r['sn'] for r in c.execute('SELECT sn FROM servers')}
                    if not set(batch['serials']).issubset(current):raise ValueError('服务器台账中的 SN 已变更，请重新预览文件')
                    for server_sn in batch['serials']:c.execute('DELETE FROM server_components WHERE server_sn=?',(server_sn,))
                    c.executemany('INSERT INTO server_components(server_sn,node_sn,node_u,kp_name,bmc_mac) VALUES(?,?,?,?,?)',[(x['server_sn'],x['node_sn'],x['node_u'],x['kp_name'],x['bmc_mac']) for x in batch['rows']])
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'部件数据存在重复 NODE_SN 或 NODE，整批未导入；请重新预览'})
            with db_ctx() as c:
                log_action(c,u['id'],'导入部件清单',f'{len(batch["rows"])} 条 / {len(batch["serials"])} 台',
                           f'按 SN 替换 46U 部件清单；来源文件预览令牌已消费')
            # 校验与事务都成功后才消费令牌；失败时用同一令牌修正数据可直接重试。
            SERVER_COMPONENT_IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows']),'servers':len(batch['serials'])})
        if p=='/api/import-servers-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            try:result=parse_gb_import(self.rfile.read(n),u['id'],'servers')
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-servers':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=SERVER_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is not None:
                try:
                    if not isinstance(edited,list) or not edited or len(edited)>len(batch['rows']):
                        raise ValueError('预览中没有可导入的数据，或行数超过预览行数；请重新上传')
                    ctx=gb_context('servers');clean=[]
                    for i,raw in enumerate(edited,1):
                        vals={f:str(raw.get(f,'')).strip() for f in SERVER_FIELDS}
                        rec,issues=parse_server_row(lambda f:vals.get(f,''),i,ctx)
                        if issues:raise ValueError(f'第 {i} 行：'+'；'.join(issues))
                        clean.append(rec)
                    batch['rows']=clean
                except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db_ctx() as c:
                    for server in batch['rows']:
                        if c.execute('SELECT 1 FROM servers WHERE sn=?',(server['sn'],)).fetchone():raise ValueError('确认导入前发现重复 SN，请重新上传并处理重复记录')
                        c.execute('INSERT INTO servers(room,rack,sn) VALUES(?,?,?)',(server['room'],server['rack'],server['sn']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入期间发现重复 SN，整批未写入；请重新预览文件'})
            with db_ctx() as c:
                log_action(c,u['id'],'导入服务器',f'{len(batch["rows"])} 台',
                           'Excel 导入服务器位置清单')
            SERVER_IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows'])})

        if p in ('/api/import-servers-remap','/api/import-server-components-remap'):
            u=self.require('can_edit')
            if not u:return
            kind='servers' if p=='/api/import-servers-remap' else 'components'
            store=SERVER_IMPORT_TOKENS if kind=='servers' else SERVER_COMPONENT_IMPORT_TOKENS
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''));batch=store.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                spec=GB_SPECS[kind];mapping={};used=set()
                for field,col in raw.items():
                    if field not in spec['fields']:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix);mapping[field]=ix
                result=remap_gb_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)

        if p=='/api/import-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            raw=self.rfile.read(n)
            default_category={'optical':'光模块','cable':'线缆'}.get(self.headers.get('X-Category-Default',''),self.headers.get('X-Category-Default',''))
            mode=self.headers.get('X-Import-Mode','create')
            if mode not in ('create','update'):mode='create'
            try: result=parse_item_import(raw,default_category,u['id'],mode)
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-remap':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''))
            batch=IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                mapping={}; used=set()
                for field,col in raw.items():
                    if field not in ITEM_FIELDS and field not in CALC_FIELDS:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix); mapping[field]=ix
                result=remap_item_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)
        if p=='/api/recycle/restore':
            u=self.require('can_edit')
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可还原记录'})
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取还原请求'})
            with db_ctx() as c:
                ids=d.get('ids')
                if d.get('all'):
                    ids=[r['id'] for r in c.execute("SELECT id FROM deletion_audit WHERE table_name='items' AND restored_at IS NULL ORDER BY id")]
                if not isinstance(ids,list) or not ids:return self.send(400,{'error':'请选择要还原的记录'})
                restored=[];skipped=[]
                for raw_id in ids[:200]:
                    try:aid=int(raw_id)
                    except:skipped.append({'id':raw_id,'reason':'编号无效'});continue
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(aid,)).fetchone()
                    if not row:skipped.append({'id':aid,'reason':'记录不存在'});continue
                    if row['restored_at']:skipped.append({'id':aid,'reason':'已还原'});continue
                    c.execute('SAVEPOINT restore_one')
                    try:
                        model=restore_snapshot(c,row,u['id'])
                        c.execute('RELEASE restore_one')
                        restored.append({'id':aid,'model':model})
                        log_action(c,u['id'],'还原物料',model,f'从回收站记录 #{aid} 还原')
                    except (ValueError,sqlite3.IntegrityError,TypeError) as e:
                        c.execute('ROLLBACK TO restore_one');c.execute('RELEASE restore_one')
                        skipped.append({'id':aid,'reason':str(e) or '还原失败'})
            return self.send(200,{'ok':True,'restored':len(restored),'items':restored,'skipped':skipped})
        if p=='/api/recycle/purge':
            u=self.require('can_edit')
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可彻底删除回收站记录'})
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取彻底删除请求'})
            with db_ctx() as c:
                ids=d.get('ids')
                if d.get('all'):
                    ids=[r['id'] for r in c.execute('SELECT id FROM deletion_audit ORDER BY id')]
                if not isinstance(ids,list) or not ids:return self.send(400,{'error':'请选择要彻底删除的记录'})
                purged=[];skipped=[]
                for raw_id in ids[:500]:
                    try:aid=int(raw_id)
                    except:skipped.append({'id':raw_id,'reason':'编号无效'});continue
                    row=c.execute('SELECT * FROM deletion_audit WHERE id=?',(aid,)).fetchone()
                    if not row:skipped.append({'id':aid,'reason':'记录不存在'});continue
                    try:
                        data=json.loads(row['payload'])
                        model=(data.get('item') or {}).get('model','') if isinstance(data,dict) else ''
                    except Exception:model=''
                    c.execute('DELETE FROM deletion_audit WHERE id=?',(aid,))
                    purged.append({'id':aid,'model':model})
                    log_action(c,u['id'],'彻底删除回收站记录',model or f'#{aid}',
                               f'永久移除删除前快照 #{aid}，不可再还原')
            return self.send(200,{'ok':True,'purged':len(purged),'items':purged,'skipped':skipped})
        if p=='/api/import-movements-preview':
            u=self.require('can_edit')
            if not u:return
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>IMPORT_LIMIT:return self.send(400,{'error':'请选择有效的 .xlsx 文件（最大 20 MB）'})
            raw=self.rfile.read(n)
            default_category={'optical':'光模块','cable':'线缆'}.get(self.headers.get('X-Category-Default',''),self.headers.get('X-Category-Default',''))
            try:result=parse_movement_import(raw,u['id'],default_category)
            except ValueError as e:return self.send(400,{'error':str(e)})
            except Exception:return self.send(400,{'error':'Excel 文件结构无法读取，请另存为标准 .xlsx 文件后重试'})
            return self.send(200,result)
        if p=='/api/import-movements-remap':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取字段映射请求'})
            token=str(d.get('edit_token',''))
            batch=MOVEMENT_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):
                return self.send(400,{'error':'预览已失效，请重新上传 Excel 文件'})
            try:
                header_row=int(d.get('header_row'))
                raw=d.get('mapping_index') or {}
                if not isinstance(raw,dict):raise ValueError('字段映射格式不正确')
                mapping={}; used=set()
                for field,col in raw.items():
                    if field not in MOVEMENT_FIELDS:continue
                    ix=int(col)
                    if ix<0 or ix>=len(batch['columns']):raise ValueError(f'列序号 {ix} 超出表格范围')
                    if ix in used:raise ValueError('同一列不能同时对应多个字段')
                    used.add(ix); mapping[field]=ix
                result=remap_movement_import(batch,header_row,mapping,token)
            except (ValueError,TypeError,KeyError) as e:
                return self.send(400,{'error':f'字段映射无效：{e}'})
            return self.send(200,result)
        if p=='/api/import-movements':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token',''));batch=MOVEMENT_IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'入库预览已失效，请重新上传文件'})
            edited=d.get('rows')
            if edited is None:
                source=batch['rows']
            else:
                try:select_edited_rows(edited,batch['rows'])  # 校验删行合法性（身份/数量）
                except ValueError as e:return self.send(400,{'error':str(e)})
                source=edited  # 用户手填的数量以提交内容为准
            # 无论是否提交编辑后的行，确认时都做一次完整复校（防止 0 数量落库）
            with db_ctx() as c:
                items={norm_model(r['model']):dict(r) for r in c.execute('SELECT id,category,model,description FROM items')}
            clean=[]
            try:
                for i,raw in enumerate(source,1):
                    model=str(raw.get('model','')).strip();key=norm_model(model)
                    category=str(raw.get('category','')).strip()
                    moved_at,_=movement_date(str(raw.get('moved_at','')).strip())
                    qv=raw.get('qty',0)
                    if qv is None or qv=='' or qv==0:
                        raise ValueError(f'第 {i} 行入库数量必须大于 0，请在预览中补填或删除该行')
                    try:qty=parse_qty(qv)
                    except ValueError as e:raise ValueError(f'第 {i} 行{e}')
                    if not model or key not in items:raise ValueError(f'第 {i} 行型号必须匹配现有物料台账')
                    if qty<=0:raise ValueError(f'第 {i} 行入库数量必须大于 0，请在预览中补填或删除该行')
                    if category!=items[key]['category']:raise ValueError(f'第 {i} 行类别与型号台账不一致')
                    clean.append({'item_id':items[key]['id'],'category':category,'model':model,
                                  'description':items[key]['description'],'kind':'入库','qty':qty,
                                  'moved_at':moved_at,'location':str(raw.get('location','')).strip(),
                                  'owner':str(raw.get('owner','')).strip(),'notes':str(raw.get('notes','')).strip()})
            except (ValueError,TypeError) as e:
                return self.send(400,{'error':str(e) or '编辑内容格式错误'})
            batch['rows']=clean
            try:
                with db_ctx() as c:
                    for item in batch['rows']:
                        exists=c.execute('SELECT id FROM items WHERE id=?',(item['item_id'],)).fetchone()
                        if not exists:raise ValueError('物料在确认导入前已被删除，请重新预览文件')
                        c.execute('INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,?,?,?,?,?,?,?)',(item['item_id'],'入库',item['qty'],item['moved_at'],item['location'],item['owner'],item['notes'],u['id']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入未完成，请重新预览文件后重试'})
            with db_ctx() as c:
                log_action(c,u['id'],'导入入库明细',f'{len(batch["rows"])} 条',
                           f'Excel 导入入库，合计 {sum(x["qty"] for x in batch["rows"])} 件')
            MOVEMENT_IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows'])})
        if p=='/api/import-items':
            u=self.require('can_edit')
            if not u:return
            try:d=self.body()
            except:return self.send(400,{'error':'无法读取导入确认请求'})
            token=str(d.get('token','')); batch=IMPORT_TOKENS.get(token)
            if not batch or batch['user_id']!=u['id'] or batch['expires']<int(time.time()):return self.send(400,{'error':'导入预览已失效，请重新上传文件'})
            if not batch.get('rows'):return self.send(400,{'error':'预览中没有可导入的数据，请先修正字段映射'})
            mode=batch.get('mode','create')
            edited=d.get('rows')
            if edited is not None:
                try:selected=select_edited_rows(edited,batch['rows'])
                except ValueError as e:return self.send(400,{'error':str(e)})
                seen=set();clean=[]
                try:
                    for i,raw in enumerate(edited,1):
                        base=selected[i-1]
                        # 更新模式下型号是匹配键，不允许在预览里被改掉
                        model=(str(base.get('model','')) if mode=='update'
                               else str(raw.get('model','')).strip())
                        desc=str(raw.get('description','')).strip()
                        category=str(raw.get('category','')).strip()
                        try:
                            planned=parse_planned(raw.get('planned',-1), allow_decimal_zero=True)
                        except ValueError:
                            raise ValueError(f'第 {i} 行计划数量必须是 0 到 {MAX_QTY:,} 之间的整数')
                        if not model or not desc:raise ValueError(f'第 {i} 行型号和名称/规格不能为空')
                        if category not in ('光模块','线缆'):raise ValueError(f'第 {i} 行类别只能是光模块或线缆')
                        if planned<0:raise ValueError(f'第 {i} 行计划数量必须是大于等于 0 的整数')
                        key=norm_model(model)
                        if key in seen:raise ValueError(f'第 {i} 行型号在本批次重复（忽略大小写后）')
                        seen.add(key)
                        fields=validate_item_fields({'category':category,'model':model,'description':desc,
                            'planned':planned,'supplier':raw.get('supplier'),'location':raw.get('location'),
                            'owner':raw.get('owner'),'notes':raw.get('notes')})
                        fields['row']=base.get('row')
                        if mode=='update':fields['matched_id']=base.get('matched_id')
                        clean.append(fields)
                    with db_ctx() as c:
                        existing={r['model_key'] for r in c.execute('SELECT model_key FROM items')}
                    if mode=='create':
                        duplicate=seen & existing
                        if duplicate:raise ValueError('型号已存在于台账：'+next(iter(duplicate)))
                    else:
                        absent=seen-existing
                        if absent:raise ValueError('台账中找不到型号：'+next(iter(absent))+'，请重新预览')
                except (ValueError,TypeError) as e:
                    return self.send(400,{'error':str(e) or '编辑内容格式错误'})
                except Exception:
                    return self.send(400,{'error':'编辑内容格式错误，请检查预览表格中的数值和文本'})
                batch['rows']=clean
            try:
                with db_ctx() as c:
                    if mode=='update':
                        for item in batch['rows']:
                            rid=item.get('matched_id')
                            if not rid:raise ValueError('预览数据已失效，请重新上传文件')
                            cur=c.execute('''UPDATE items SET category=?,description=?,planned=?,supplier=?,location=?,owner=?,notes=? WHERE id=?''',
                                (item['category'],item['description'],item['planned'],item['supplier'],
                                 item['location'],item['owner'],item['notes'],rid))
                            if cur.rowcount==0:raise ValueError('物料在确认前已被删除，请重新预览')
                    else:
                        for item in batch['rows']:
                            c.execute('INSERT INTO items(category,model,model_key,description,supplier,planned,location,owner,notes) VALUES(?,?,?,?,?,?,?,?,?)',
                                (item['category'],item['model'],item['model_key'],item['description'],
                                 item['supplier'],item['planned'],item['location'],item['owner'],item['notes']))
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(409,{'error':'导入期间发现重复型号，整批未导入；请刷新预览后处理重复项'})
            with db_ctx() as c:
                log_action(c,u['id'],'批量更新物料' if mode=='update' else '导入物料',
                           f'{len(batch["rows"])} 条',f'模式：{mode}')
            IMPORT_TOKENS.pop(token,None)
            return self.send(200,{'ok':True,'imported':len(batch['rows']),'mode':mode})
        try: d=self.body()
        except Exception as e:return self.send(400,{'error':'无法读取提交内容'})
        if p=='/api/setup':
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            try:validate_username(username)
            except ValueError as e:return self.send(400,{'error':str(e)})
            if len(password)<12:return self.send(400,{'error':'管理员密码至少 12 位'})
            salt=secrets.token_bytes(16); hashed=pw_hash(password,salt)
            # 独占写事务 + 条件插入 + 检查受影响行数：并发初始化只允许一次成功。
            c=db()
            try:
                c.execute('BEGIN IMMEDIATE')
                cur=c.execute('''INSERT INTO users(username,salt,pw_hash,is_admin,can_view,can_edit)
                                 SELECT ?,?,?,1,1,1 WHERE NOT EXISTS(SELECT 1 FROM users)''',
                              (username,salt,hashed))
                if cur.rowcount!=1:
                    c.rollback(); return self.send(409,{'error':'管理员已初始化'})
                uid=cur.lastrowid; c.commit()
            except sqlite3.IntegrityError:
                c.rollback(); return self.send(409,{'error':'管理员已初始化'})
            except Exception:
                c.rollback(); raise
            finally:c.close()
            return self.make_session(uid)
        if p=='/api/login':
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            ip=self.client_address[0] if self.client_address else 'unknown'
            wait=login_block_seconds(ip,username)
            if wait:return self.send(429,{'error':f'登录失败次数过多，请 {wait} 秒后重试'},headers={'Retry-After':str(wait)})
            u=verify_credentials(username,password)
            if not u:
                login_record_fail(ip,username)
                return self.send(401,{'error':'用户名或密码不正确'})
            login_clear(ip,username)
            return self.make_session(u['id'])
        if p=='/api/logout':
            u=self.require(csrf=True)
            if not u:return
            token=''
            for part in self.headers.get('Cookie','').split(';'):
                if part.strip().startswith('sid='):token=part.strip()[4:]
            with db_ctx() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            return self.send(200,{'ok':True},headers={'Set-Cookie':'sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if p=='/api/servers':
            u=self.require('can_edit')
            if not u:return
            room=str(d.get('room','')).strip();rack=str(d.get('rack','')).strip();sn=str(d.get('sn','')).strip()
            try:room,rack,sn=validate_server_fields(room,rack,sn)
            except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db_ctx() as c:c.execute('INSERT INTO servers(room,rack,sn) VALUES(?,?,?)',(room,rack,sn))
            except sqlite3.IntegrityError:return self.send(409,{'error':'该 SN 已存在'})
            return self.send(201,{'ok':True})
        if p=='/api/items':
            u=self.require('can_edit')
            if not u:return
            try: fields=validate_item_fields(d)
            except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db_ctx() as c:
                    if c.execute('SELECT 1 FROM items WHERE model_key=?',(fields['model_key'],)).fetchone():
                        return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
                    c.execute('INSERT INTO items(category,model,model_key,description,supplier,planned,location,owner,notes) VALUES(?,?,?,?,?,?,?,?,?)',
                        (fields['category'],fields['model'],fields['model_key'],fields['description'],
                         fields['supplier'],fields['planned'],fields['location'],fields['owner'],fields['notes']))
                    log_action(c,u['id'],'新增物料',fields['model'],f"计划数量 {fields['planned']}")
            except sqlite3.IntegrityError:return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
            return self.send(201,{'ok':True})
        if p=='/api/movements':
            u=self.require('can_edit')
            if not u:return
            kind=str(d.get('kind','')); moved=str(d.get('moved_at','')); model=str(d.get('model','')).strip(); category=str(d.get('category',''))
            try: qty=parse_qty(d.get('qty',0))
            except ValueError as e:return self.send(400,{'error':str(e)})
            if kind not in ('入库','领用/安装','报废') or not moved:return self.send(400,{'error':'业务类型、日期必填'})
            if category not in ('光模块','线缆'):return self.send(400,{'error':'类别必填且只能是光模块或线缆'})
            try:
                datetime.strptime(str(moved),'%Y-%m-%d')
            except (ValueError,TypeError):
                return self.send(400,{'error':'日期格式必须是 YYYY-MM-DD，例如 2026-11-12'})
            try:
                with db_ctx() as c:
                    matches=c.execute('SELECT id,model,category FROM items WHERE model_key=?',(norm_model(model),)).fetchall()
                    if not matches:return self.send(404,{'error':'型号不存在'})
                    if len(matches)>1:return self.send(409,{'error':'该型号不唯一，无法关联'})
                    it=matches[0]; item_id=it['id']
                    if category and category!=it['category']:return self.send(400,{'error':'类别与该型号台账不一致'})
                    cur=c.execute('INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,?,?,?,?,?,?,?)',(item_id,kind,qty,moved,str(d.get('location','')),str(d.get('owner',d.get('handler',''))),str(d.get('notes','')),u['id']))
                    if kind=='报废':
                        c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(cur.lastrowid,i,item_id,u['id']) for i in range(1,qty+1)])
                    if kind!='入库':
                        assert_stock(c,[item_id],f'登记{kind} {qty} 件')
                    log_action(c,u['id'],'登记'+kind,model,f'数量 {qty}，日期 {moved}')
            except ValueError as e:return self.send(409,{'error':str(e)})
            except sqlite3.IntegrityError:return self.send(400,{'error':'明细数据无效'})
            return self.send(201,{'ok':True})
        if p=='/api/users':
            u=self.require(csrf=True)
            if not u:return
            if not u['is_admin']:return self.send(403,{'error':'仅管理员可创建账号'})
            username=str(d.get('username','')).strip(); password=str(d.get('password',''))
            try:validate_username(username)
            except ValueError as e:return self.send(400,{'error':str(e)})
            if len(password)<12:return self.send(400,{'error':'新账号密码至少 12 位'})
            salt=secrets.token_bytes(16)
            try:
                with db_ctx() as c:c.execute('INSERT INTO users(username,salt,pw_hash,is_admin,can_view,can_edit) VALUES(?,?,?,0,?,?)',(username,salt,pw_hash(password,salt),int(bool(d.get('can_view')) or bool(d.get('can_edit'))),int(bool(d.get('can_edit')))))
            except sqlite3.IntegrityError:return self.send(409,{'error':'用户名已存在'})
            return self.send(201,{'ok':True})
        return self.send(404,{'error':'没有这个接口'})
    def do_PATCH(self):
        p=urllib.parse.urlparse(self.path).path
        if self.idem_guard(p,'PATCH'):return
        if p.startswith('/api/material-spares/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();spare_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'坏件记录格式无效'})
            serial=str(d.get('serial_number','')).strip();status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip() or u['username'];notes=str(d.get('notes','')).strip()
            if not serial or len(serial)>150:return self.send(400,{'error':'SN必填，最多 150 字'})
            if status not in MATERIAL_SPARE_STATUSES or status=='待处理':return self.send(400,{'error':'请填写SN并选择处理状态'})
            if len(responsible)>100:return self.send(400,{'error':'负责人最多 100 字'})
            if len(notes)>SPARE_NOTES_LIMIT:return self.send(400,{'error':f'备注最多 {SPARE_NOTES_LIMIT} 字'})
            try:
                with db_ctx() as c:
                    old=c.execute('SELECT * FROM material_spares WHERE id=?',(spare_id,)).fetchone()
                    if not old:return self.send(404,{'error':'坏件记录不存在'})
                    duplicate=c.execute('SELECT id FROM material_spares WHERE item_id=? AND serial_number=? AND id<>?',(old['item_id'],serial,spare_id)).fetchone()
                    if duplicate:return self.send(409,{'error':'同一型号下该SN已登记'})
                    c.execute('UPDATE material_spares SET serial_number=?,status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(serial,status,responsible,notes,spare_id))
                    if old['serial_number']!=serial or old['status']!=status or old['responsible']!=responsible or old['notes']!=notes:
                        c.execute('''INSERT INTO material_spare_history(spare_id,from_status,to_status,old_serial_number,serial_number,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?,?,?)''',(spare_id,old['status'],status,old['serial_number'],serial,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'坏件记录更新冲突，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/material-spare-cases/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();case_id=int(p.rsplit('/',1)[1]);returned_qty=int(d.get('returned_qty',0));scrapped_qty=int(d.get('scrapped_qty',0))
            except:return self.send(400,{'error':'RMA处理数量格式无效'})
            status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip();notes=str(d.get('notes','')).strip()
            if status not in MATERIAL_SPARE_STATUSES:return self.send(400,{'error':'备件状态无效'})
            if not responsible or len(responsible)>100:return self.send(400,{'error':'实际操作人必填，最多 100 字'})
            try:
                with db_ctx() as c:
                    old=c.execute('SELECT * FROM material_spare_cases WHERE id=?',(case_id,)).fetchone()
                    if not old:return self.send(404,{'error':'RMA备件记录不存在'})
                    active=('待RMA','RMA处理中')
                    if old['status'] not in active and status!=old['status']:return self.send(409,{'error':'该RMA记录已结束，不能再次改变处置结果'})
                    if old['status']=='待RMA' and status=='RMA已完成':return self.send(400,{'error':'请先将状态更新为RMA处理中，再登记RMA结果'})
                    if returned_qty<old['returned_qty'] or scrapped_qty<old['scrapped_qty'] or returned_qty+scrapped_qty>old['qty']:
                        return self.send(400,{'error':'返还和报废数量必须递增，且合计不能超过登记数量'})
                    if status=='报废':scrapped_qty=old['qty']-returned_qty
                    if status=='RMA已完成' and returned_qty+scrapped_qty!=old['qty']:
                        return self.send(400,{'error':'RMA已完成时，必须登记全部数量的返还或报废结果'})
                    if status in active and returned_qty+scrapped_qty>=old['qty']:
                        return self.send(400,{'error':'该数量已全部返还或报废，请完成RMA记录'})
                    scrap_delta=scrapped_qty-old['scrapped_qty']
                    if scrap_delta>0:
                        c.execute("INSERT INTO movements(item_id,kind,qty,moved_at,location,owner,notes,created_by) VALUES(?,'报废',?,?,?,?,?,?)",(old['item_id'],scrap_delta,date.today().isoformat(),'RMA处置',responsible,notes or f'RMA备件记录 #{case_id} 报废',u['id']))
                    c.execute('UPDATE material_spare_cases SET returned_qty=?,scrapped_qty=?,status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(returned_qty,scrapped_qty,status,responsible,notes,case_id))
                    if status!=old['status'] or returned_qty!=old['returned_qty'] or scrapped_qty!=old['scrapped_qty'] or responsible!=old['responsible'] or notes!=old['notes']:
                        c.execute('''INSERT INTO material_spare_case_history(case_id,from_status,to_status,qty_returned,qty_scrapped,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?,?,?)''',(case_id,old['status'],status,returned_qty,scrapped_qty,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'RMA状态更新冲突，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/server-spares/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); spare_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'备件记录格式无效'})
            status=str(d.get('status','')).strip();responsible=str(d.get('responsible','')).strip();notes=str(d.get('notes','')).strip()
            if status not in SERVER_SPARE_STATUSES:return self.send(400,{'error':'备件状态无效'})
            if not responsible or len(responsible)>100:return self.send(400,{'error':'实际操作人必填，最多 100 字'})
            if len(notes)>SPARE_NOTES_LIMIT:return self.send(400,{'error':f'备注最多 {SPARE_NOTES_LIMIT} 字'})
            try:
                with db_ctx() as c:
                    old=c.execute('SELECT status FROM server_spares WHERE id=?',(spare_id,)).fetchone()
                    if not old:return self.send(404,{'error':'服务器备件记录不存在'})
                    c.execute('UPDATE server_spares SET status=?,responsible=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(status,responsible,notes,spare_id))
                    if old['status']!=status:
                        c.execute('INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by) VALUES(?,?,?,?,?,?)',(spare_id,old['status'],status,responsible,notes,u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'状态更新失败，请刷新后重试'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/server-components/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); component_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'部件数据格式无效'})
            node_sn=str(d.get('node_sn','')).strip(); responsible=str(d.get('responsible','')).strip(); action=str(d.get('action','信息修正'))
            if not node_sn or not responsible or len(responsible)>100:return self.send(400,{'error':'新 NODE_SN 和责任人必填（责任人最多 100 字）'})
            if action not in ('信息修正','部件更换'):return self.send(400,{'error':'操作类型无效'})
            if action=='部件更换' and node_sn=='' :return self.send(400,{'error':'更换后的 NODE_SN 不能为空'})
            try:
                with db_ctx() as c:
                    old=c.execute('SELECT server_sn,node_sn,node_u,kp_name,bmc_mac FROM server_components WHERE id=?',(component_id,)).fetchone()
                    if not old:return self.send(404,{'error':'该 U 位部件不存在'})
                    srv=c.execute('SELECT id FROM servers WHERE sn=?',(old['server_sn'],)).fetchone()
                    server_id=srv['id'] if srv else ARCHIVED_SERVER_ID
                    if action=='部件更换' and node_sn==old['node_sn']:return self.send(400,{'error':'更换后的 NODE_SN 与当前相同，请确认更换编号'})
                    old_data=dict(old)
                    new_data={'node_sn':node_sn,'node_u':old['node_u'],'kp_name':old['kp_name'],'bmc_mac':old['bmc_mac']}
                    c.execute('UPDATE server_components SET node_sn=? WHERE id=?',(node_sn,component_id))
                    op=c.execute('INSERT INTO server_component_operations(server_sn,server_id,node_u,action,responsible,old_data,new_data,operated_by) VALUES(?,?,?,?,?,?,?,?)',(old['server_sn'],server_id,old['node_u'],action,responsible,json.dumps(old_data,ensure_ascii=False),json.dumps(new_data,ensure_ascii=False),u['id']))
                    if action=='部件更换' and old['node_sn']!=node_sn:
                        place=c.execute('SELECT room,rack FROM servers WHERE sn=?',(old['server_sn'],)).fetchone()
                        room,rack=(place['room'],place['rack']) if place else ('','')
                        spare=c.execute('''INSERT INTO server_spares(operation_id,server_sn,server_id,node_sn,node_u,kp_name,bmc_mac,room,rack,status,responsible,created_by)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(op.lastrowid,old['server_sn'],server_id,old['node_sn'],old['node_u'],old['kp_name'],old['bmc_mac'],room,rack,'待检测',responsible,u['id']))
                        c.execute('''INSERT INTO server_spare_status_history(spare_id,from_status,to_status,responsible,notes,operated_by)
                            VALUES(?,?,?,?,?,?)''',(spare.lastrowid,'更换下架','待检测',responsible,'部件更换时自动转入服务器备件台账',u['id']))
            except sqlite3.IntegrityError:return self.send(409,{'error':'目标 U 位已占用，或 NODE_SN 已在本服务器使用'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/servers/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body();server_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'服务器记录格式无效'})
            room=str(d.get('room','')).strip();rack=str(d.get('rack','')).strip();sn=str(d.get('sn','')).strip()
            try:room,rack,sn=validate_server_fields(room,rack,sn)
            except ValueError as e:return self.send(400,{'error':str(e)})
            try:
                with db_ctx() as c:
                    cur=c.execute('UPDATE servers SET room=?,rack=?,sn=? WHERE id=?',(room,rack,sn,server_id))
                    if cur.rowcount==0:return self.send(404,{'error':'服务器记录不存在'})
            except sqlite3.IntegrityError:return self.send(409,{'error':'该 SN 已被其他服务器使用'})
            return self.send(200,{'ok':True})
        if p.startswith('/api/movements/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); movement_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'明细数据格式无效'})
            try:qty=parse_qty(d.get('qty',0))
            except ValueError as e:return self.send(400,{'error':str(e)})
            model=str(d.get('model','')).strip(); kind=str(d.get('kind','')); moved=str(d.get('moved_at',''))
            if not model or not moved or kind not in ('入库','领用/安装','报废'):return self.send(400,{'error':'型号、日期和有效业务类型必填'})
            with db_ctx() as c:
                items=c.execute('SELECT id FROM items WHERE model=?',(model,)).fetchall()
                if len(items)!=1:return self.send(400,{'error':'型号不存在或不唯一'})
                item=c.execute('SELECT category FROM items WHERE id=?',(items[0]['id'],)).fetchone()
                if d.get('category') and d.get('category')!=item['category']:return self.send(400,{'error':'类别需与型号对应的台账类别一致'})
                old=c.execute('SELECT * FROM movements WHERE id=?',(movement_id,)).fetchone()
                if not old:return self.send(404,{'error':'库存变动明细不存在'})
                linked=c.execute('SELECT COUNT(*) n FROM material_spares WHERE movement_id=?',(movement_id,)).fetchone()['n']
                progressed=c.execute("SELECT COUNT(*) n FROM material_spares WHERE movement_id=? AND (serial_number<>'' OR status<>'待处理')",(movement_id,)).fetchone()['n']
                if linked and progressed and (kind!='报废' or old['item_id']!=items[0]['id'] or qty<old['qty']):
                    return self.send(409,{'error':'该报废明细已有已处理坏件，不能更改型号、业务类型或减少数量'})
                if predict_negative(c,movement_id,items[0]['id'],kind,qty):
                    return self.send(409,{'error':'可用库存不足：修改后会出现负库存，不允许负库存，本次修改已撤销'})
                cur=c.execute('UPDATE movements SET item_id=?,kind=?,qty=?,moved_at=?,location=?,owner=?,notes=? WHERE id=?',(items[0]['id'],kind,qty,moved,str(d.get('location','')),str(d.get('owner','')),str(d.get('notes','')),movement_id))
                if linked:
                    if kind!='报废':c.execute('DELETE FROM material_spares WHERE movement_id=?',(movement_id,))
                    elif not progressed and old['item_id']!=items[0]['id']:c.execute('DELETE FROM material_spares WHERE movement_id=?',(movement_id,))
                    if kind=='报废':
                        c.execute('DELETE FROM material_spares WHERE movement_id=? AND unit_index>?',(movement_id,qty))
                        existing=c.execute('SELECT COALESCE(MAX(unit_index),0) n FROM material_spares WHERE movement_id=?',(movement_id,)).fetchone()['n']
                        c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(movement_id,i,items[0]['id'],u['id']) for i in range(existing+1,qty+1)])
                elif kind=='报废':
                    c.executemany('INSERT INTO material_spares(movement_id,unit_index,item_id,created_by) VALUES(?,?,?,?)',[(movement_id,i,items[0]['id'],u['id']) for i in range(1,qty+1)])
            return self.send(200,{'ok':True})
        if p.startswith('/api/items/'):
            u=self.require('can_edit')
            if not u:return
            try:d=self.body(); item_id=int(p.rsplit('/',1)[1])
            except:return self.send(400,{'error':'请求格式无效'})
            unknown=[k for k in d if k not in ITEM_FIELDS]
            if unknown:return self.send(400,{'error':'不支持修改的字段：'+ '、'.join(sorted(unknown))})
            try:fields=validate_item_fields(d,partial=True)
            except ValueError as e:return self.send(400,{'error':str(e)})
            if not fields:return self.send(400,{'error':'没有需要修改的字段'})
            with db_ctx() as c:
                cur=c.execute('SELECT id,model_key FROM items WHERE id=?',(item_id,)).fetchone()
                if not cur:return self.send(404,{'error':'物料不存在'})
                if 'model_key' in fields and fields['model_key']!=cur['model_key']:
                    if c.execute('SELECT 1 FROM items WHERE model_key=? AND id<>?',(fields['model_key'],item_id)).fetchone():
                        return self.send(409,{'error':'该型号已存在；型号必须唯一（不区分大小写）'})
                sets=', '.join(f'{k}=?' for k in fields)
                c.execute(f'UPDATE items SET {sets} WHERE id=?',list(fields.values())+[item_id])
                log_action(c,u['id'],'编辑物料',fields.get('model') or f'#{item_id}',
                           '修改字段：'+ '、'.join(sorted(set(fields)&set(ITEM_FIELDS))))
            return self.send(200,{'ok':True,'updated':sorted(set(fields)&set(ITEM_FIELDS))})
        if not p.startswith('/api/users/'):return self.send(404,{'error':'没有这个接口'})
        u=self.require(csrf=True)
        if not u:return
        if not u['is_admin']:return self.send(403,{'error':'仅管理员可管理账号'})
        try:d=self.body(); uid=int(p.rsplit('/',1)[1])
        except:return self.send(400,{'error':'请求格式无效'})
        if uid==u['id']:return self.send(400,{'error':'不能停用当前管理员账号'})
        with db_ctx() as c:
            target=c.execute('SELECT id,is_admin FROM users WHERE id=?',(uid,)).fetchone()
            if not target or target['is_admin']:return self.send(404,{'error':'协管员不存在'})
            active=int(bool(d.get('active',True))); edit=int(bool(d.get('can_edit'))); view=int(bool(d.get('can_view')) or bool(edit))
            c.execute('UPDATE users SET active=?,can_view=?,can_edit=? WHERE id=?',(active,view,edit,uid))
            if not active:c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        return self.send(200,{'ok':True})
    def make_session(self,uid):
        token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); th=hashlib.sha256(token.encode()).hexdigest()
        with db_ctx() as c:
            c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires) VALUES(?,?,?,?)',(th,uid,csrf,int(time.time())+SESSION_SECONDS))
            u=c.execute('SELECT id,username,is_admin,can_view,can_edit FROM users WHERE id=?',(uid,)).fetchone()
        # 内网 HTTP 直接部署不能加 Secure（否则 Cookie 不会被发送）；经 HTTPS 反代时自动补齐。
        fwd=(self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        secure='; Secure' if (fwd=='https' or getattr(self.connection,'_sslobj',None)) else ''
        return self.send(200,dict(u)|{'csrf':csrf},headers={'Set-Cookie':f'sid={token}; HttpOnly; SameSite=Strict{secure}; Path=/; Max-Age={SESSION_SECONDS}'})
    def do_DELETE(self):
        p=urllib.parse.urlparse(self.path).path
        if self.idem_guard(p,'DELETE'):return
        u=self.require(csrf=True)
        if not u:return
        if not u['is_admin']:return self.send(403,{'error':'仅管理员可删除记录'})
        try: record_id=int(p.rsplit('/',1)[1])
        except:return self.send(400,{'error':'记录编号无效'})
        try: d=self.body() if int(self.headers.get('Content-Length','0') or 0)>0 else {}
        except Exception: d={}
        with db_ctx() as c:
            if p.startswith('/api/server-spares/'):
                spare=c.execute('''SELECT s.id,s.server_id,sv.sn AS current_sn
                    FROM server_spares s LEFT JOIN servers sv ON sv.id=s.server_id
                    WHERE s.id=?''',(record_id,)).fetchone()
                if not spare:return self.send(404,{'error':'服务器备件记录不存在'})
                if spare['server_id'] and spare['server_id']>0 and spare['current_sn']:
                    return self.send(409,{'error':'该备件仍关联现有服务器，不能单独删除；如需清理请先删除对应服务器，历史归档后再处理。'})
                c.execute('DELETE FROM server_spare_status_history WHERE spare_id=?',(record_id,))
                c.execute('DELETE FROM server_spares WHERE id=?',(record_id,))
                log_action(c,u['id'],'彻底删除服务器备件',f'#{record_id}','清理已归档孤儿备件及状态历史；更换操作记录保留')
                return self.send(200,{'ok':True})
            if p.startswith('/api/servers/'):
                sv=c.execute('SELECT sn FROM servers WHERE id=?',(record_id,)).fetchone()
                if not sv:return self.send(404,{'error':'服务器记录不存在'})
                counts={'components':c.execute('SELECT COUNT(*) n FROM server_components WHERE server_sn=?',(sv['sn'],)).fetchone()['n'],
                        'operations':c.execute('SELECT COUNT(*) n FROM server_component_operations WHERE server_id=?',(record_id,)).fetchone()['n'],
                        'spares':c.execute('SELECT COUNT(*) n FROM server_spares WHERE server_id=?',(record_id,)).fetchone()['n'],
                        'history':c.execute('''SELECT COUNT(*) n FROM server_spare_status_history h
                            JOIN server_spares s ON s.id=h.spare_id WHERE s.server_id=?''',(record_id,)).fetchone()['n']}
                if sum(counts.values()) and str(d.get('confirm_sn','')).strip()!=str(sv['sn']).strip():
                    return self.send(409,{
                        'error':f'该服务器有 {counts["components"]} 条 46U 部件、{counts["operations"]} 条更换/修正历史、'
                                f'{counts["spares"]} 条备件及 {counts["history"]} 条状态历史。删除会移除部件清单，'
                                f'更换历史与备件将归档为「原服务器已删除」（不再关联任何服务器，也不会错挂到复用的 SN）。'
                                f'如确需删除，请输入完整 SN「{sv["sn"]}」再次确认。',
                        'counts':counts,'sn':sv['sn'],'requires_confirm':True})
                # 归档：断开 id 关联，历史与备件本身保留供追溯，绝不按 SN 重新挂回新机器
                c.execute('UPDATE server_component_operations SET server_id=? WHERE server_id=?',(ARCHIVED_SERVER_ID,record_id))
                c.execute('UPDATE server_spares SET server_id=? WHERE server_id=?',(ARCHIVED_SERVER_ID,record_id))
                c.execute('DELETE FROM servers WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除服务器',f'#{record_id}','部件清单已删除；更换历史与备件已归档')
                return self.send(200,{'ok':True,'archived':counts})
            if p.startswith('/api/items/'):
                it=c.execute('SELECT * FROM items WHERE id=?',(record_id,)).fetchone()
                if not it:return self.send(404,{'error':'物料不存在'})
                counts={'movements':c.execute('SELECT COUNT(*) n FROM movements WHERE item_id=?',(record_id,)).fetchone()['n'],
                        'spares':c.execute('SELECT COUNT(*) n FROM material_spares WHERE item_id=?',(record_id,)).fetchone()['n'],
                        'cases':c.execute('SELECT COUNT(*) n FROM material_spare_cases WHERE item_id=?',(record_id,)).fetchone()['n']}
                if sum(counts.values()) and str(d.get('confirm_model','')).strip()!=str(it['model']).strip():
                    return self.send(409,{
                        'error':f'该物料已有 {counts["movements"]} 条库存变动明细、{counts["spares"]} 条坏件 SN 记录、{counts["cases"]} 条备件单。删除会一并抹掉这些历史且无法恢复。如确需删除，请输入完整型号「{it["model"]}」再次确认。',
                        'counts':counts,'model':it['model'],'requires_confirm':True})
                # 删除前留完整快照，便于事后追溯/恢复
                def fetch(sql,args=()):return [dict(r) for r in c.execute(sql,args).fetchall()]
                spare_ids=[r['id'] for r in c.execute('SELECT id FROM material_spares WHERE item_id=?',(record_id,)).fetchall()]
                case_ids=[r['id'] for r in c.execute('SELECT id FROM material_spare_cases WHERE item_id=?',(record_id,)).fetchall()]
                snapshot={'item':dict(it),
                          'movements':fetch('SELECT * FROM movements WHERE item_id=?',(record_id,)),
                          'material_spares':fetch('SELECT * FROM material_spares WHERE item_id=?',(record_id,)),
                          'material_spare_cases':fetch('SELECT * FROM material_spare_cases WHERE item_id=?',(record_id,))}
                for key,table,col,ids in (('material_spare_history','material_spare_history','spare_id',spare_ids),
                                          ('material_spare_case_history','material_spare_case_history','case_id',case_ids)):
                    if ids:
                        ph=','.join('?'*len(ids))
                        snapshot[key]=fetch(f'SELECT * FROM {table} WHERE {col} IN ({ph})',ids)
                c.execute('INSERT INTO deletion_audit(table_name,record_id,payload,deleted_by) VALUES(?,?,?,?)',
                          ('items',record_id,json.dumps(snapshot,ensure_ascii=False,default=str),u['id']))
                removed=c.execute('DELETE FROM movements WHERE item_id=?',(record_id,)).rowcount
                c.execute('DELETE FROM items WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除物料',it['model'],
                           f"删除 {removed} 条明细、{counts['spares']} 条坏件记录；快照已进入回收站")
                return self.send(200,{'ok':True,'deleted_movements':removed,'audit_saved':True})
            if p.startswith('/api/movements/'):
                linked=c.execute('SELECT COUNT(*) n FROM material_spares WHERE movement_id=? AND (serial_number<>\'\' OR status<>\'待处理\')',(record_id,)).fetchone()['n']
                if linked:return self.send(409,{'error':'该报废明细已有已处理坏件，不能删除；请保留明细及SN追溯记录'})
                old=c.execute('SELECT item_id,kind,qty FROM movements WHERE id=?',(record_id,)).fetchone()
                if not old:return self.send(404,{'error':'明细不存在'})
                after=stock_available(c,old['item_id'])+(-old['qty'] if old['kind']=='入库' else old['qty'])
                if after<0:
                    return self.send(409,{'error':f'删除后可用库存将变为 {after}，不允许负库存，本次删除已撤销'})
                cur=c.execute('DELETE FROM movements WHERE id=?',(record_id,))
                log_action(c,u['id'],'删除库存变动明细',f'#{record_id}','该操作不进入回收站')
                return self.send(200,{'ok':True})
        return self.send(404,{'error':'没有这个接口'})
    def do_OPTIONS(self): self.send(204,b'')


if __name__=='__main__':
    init(); print(f'IDC资产管理已启动: http://{HOST}:{PORT}'); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
