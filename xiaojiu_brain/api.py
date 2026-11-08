from __future__ import annotations
import hmac,json,mimetypes,socket,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie,CookieError
from pathlib import Path
from urllib.parse import parse_qs,unquote,urlsplit

class BrainServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,app):
        self.app=app
        if ':' in address[0]:self.address_family=socket.AF_INET6
        super().__init__(address,Handler)
        from .browser_auth import BrowserSessions
        self.browser_sessions=BrowserSessions(app.data_dir/'browser-sessions.json',app.config.api_token)
        self.browser_cookie_name='memory_brain_session_'+str(self.server_port)

class Handler(BaseHTTPRequestHandler):
    server_version='MemoryBrain'
    def log_message(self,format,*args):pass  # Never log payload, token or query string.
    @property
    def app(self):return self.server.app
    def _headers(self,status,content_type,length,attachment=None,cookie=None):
        self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
    def _bytes(self,status,body,content_type='application/json; charset=utf-8',attachment=None,cookie=None):
        if status>=400:self._discard_rejected_body()
        self._headers(status,content_type,len(body),attachment,cookie)
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def _json(self,status,data=None,error=None,cookie=None):
        obj={'ok':error is None}
        if error is None:obj['data']=data
        else:obj['error']=error
        self._bytes(status,json.dumps(obj,ensure_ascii=False,allow_nan=False).encode('utf8'),cookie=cookie)
    def _guard_origin(self):
        hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}',f'[::1]:{self.server.server_port}'}
        if self.headers.get('Host') not in hosts:return False
        origin=self.headers.get('Origin')
        try:
            parsed=urlsplit(origin) if origin else None
            if parsed and (parsed.netloc not in hosts or parsed.scheme!='http' or parsed.path or parsed.query or parsed.fragment):return False
        except ValueError:return False
        return True
    def _browser_token(self):
        raw=self.headers.get('Cookie','')
        if len(raw)>8192:return None
        try:
            cookies=SimpleCookie();cookies.load(raw)
            entry=cookies.get(self.server.browser_cookie_name)
            return entry.value if entry else None
        except CookieError:return None
    def _new_browser_cookie(self):
        from .browser_auth import browser_cookie
        # Reopening the launcher must not invalidate another active tab.
        # A still-valid browser keeps its original absolute expiration.
        if self.server.browser_sessions.valid(self._browser_token()):return None
        token,expires=self.server.browser_sessions.issue()
        return browser_cookie(self.server.browser_cookie_name,token,expires_at=expires,max_age=self.server.browser_sessions.ttl)
    def _authorized(self):
        self._auth_mode=None
        value=self.headers.get('Authorization','')
        if value:
            if value.startswith('Bearer ') and hmac.compare_digest(value[7:].encode(),self.app.config.api_token.encode()):
                self._auth_mode='bearer';return True
            return False
        if self.server.browser_sessions.valid(self._browser_token()):
            self._auth_mode='browser';return True
        return False
    def _discard_rejected_body(self):
        # Closing HTTP/1.0 with a normal POST body still in flight can turn
        # a 401/403 into a TCP reset. Discard small declared bodies only;
        # never parse/store them or wait indefinitely on a slow sender.
        if getattr(self,'_request_body_consumed',False):return
        if self.command not in ('POST','PATCH','PUT','DELETE') or self.headers.get('Transfer-Encoding'):return
        try:length=int(self.headers.get('Content-Length','0'))
        except (ValueError,TypeError):return
        if not 0<length<=65536:return
        deadline=time.monotonic()+.1;old_timeout=self.connection.gettimeout()
        try:
            while length:
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                self.connection.settimeout(remaining)
                chunk=self.rfile.read1(min(length,8192))
                if not chunk:break
                length-=len(chunk)
        except (OSError,ValueError):pass
        finally:
            self._request_body_consumed=True
            self.close_connection=True
            try:self.connection.settimeout(old_timeout)
            except OSError:pass
    def _body(self):
        length=int(self.headers.get('Content-Length','0'))
        if length<0 or length>2*1024*1024:raise ValueError('请求内容过大')
        raw=self.rfile.read(length) if length else b''
        self._request_body_consumed=True
        value=json.loads(raw.decode('utf8')) if length else {}
        if not isinstance(value,dict):raise ValueError('请求必须为JSON对象')
        return value
    def _query(self):return parse_qs(urlsplit(self.path).query)
    def _dispatch(self,method):
        try:
            parsed=urlsplit(self.path)
            if parsed.scheme or parsed.netloc or '\x00' in unquote(parsed.path):return self._json(400,error='请求路径无效')
            path=unquote(parsed.path)
        except ValueError:return self._json(400,error='请求路径无效')
        app=self.app
        if not self._guard_origin():return self._json(403,error='不允许的访问来源')
        if method=='GET' and path=='/health':return self._json(200,{'version':__import__('xiaojiu_brain').__version__,'build_id':app.build_id,'auth_required':True})
        if method=='GET' and (path in ('/','/brain','/brain/','/panel','/panel/') or path.startswith('/static/')):
            static=Path(__file__).resolve().parent/'static'
            target=static/('brain.html' if path in ('/','/brain','/brain/') else 'panel.html' if path in ('/panel','/panel/') else path[len('/static/'):])
            target=target.resolve()
            if not target.is_relative_to(static.resolve()) or not target.is_file():return self._json(404,error='文件不存在')
            mime=mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            return self._bytes(200,target.read_bytes(),mime+('; charset=utf-8' if mime.startswith(('text/','application/javascript')) else ''))
        if method=='POST' and path=='/v1/session/bootstrap':
            try:result=app.redeem_ticket(self._body().get('ticket',''))
            except (ValueError,TypeError):return self._json(401,error='登录链接已过期或已使用')
            try:return self._json(200,result,cookie=self._new_browser_cookie())
            except Exception:return self._json(500,error='无法保存本机浏览器登录状态，请从桌面入口重试')
        if not self._authorized():return self._json(401,error='管理口令无效或未登录')
        try:
            body=self._body() if method in ('POST','PATCH','PUT','DELETE') else {};q=self._query();owner=app.owner
            limit=lambda default=50:min(200,max(1,int(q.get('limit',[default])[0])))
            conversation=q.get('conversation_id',[None])[0]
            if method=='GET' and path=='/v1/session/current':return self._json(200,{'authenticated':True,'mode':self._auth_mode})
            if method=='POST' and path=='/v1/session/browser':
                return self._json(200,{'authenticated':True,'mode':'browser'},cookie=self._new_browser_cookie())
            if method=='POST' and path=='/v1/session/logout':
                from .browser_auth import browser_cookie
                self.server.browser_sessions.revoke(self._browser_token())
                return self._json(200,{'logged_out':True},cookie=browser_cookie(self.server.browser_cookie_name))
            if method=='GET' and path=='/v1/status':return self._json(200,app.status())
            if method=='GET' and path=='/v1/observatory':
                from .observatory import snapshot
                return self._json(200,snapshot(app))
            if method=='GET' and path=='/v1/activity':
                from .observatory import activity_snapshot
                return self._json(200,activity_snapshot(app))
            if method=='GET' and path=='/v1/memory-map':
                from .memory_map import snapshot
                return self._json(200,snapshot(app,days=q.get('days',['30'])[0],kind=q.get('kind',['memories'])[0],
                    limit=int(q.get('limit',['200'])[0]),offset=int(q.get('offset',['0'])[0]),until=q.get('until',[None])[0]))
            if method=='GET' and path.startswith('/v1/memory-map/'):
                from .memory_map import detail
                return self._json(200,detail(app,path[len('/v1/memory-map/'):]))
            if method=='POST' and path=='/v1/admin/shutdown':
                self._json(200,{'stopping':True});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            if method=='POST' and path=='/v1/session/ticket':return self._json(200,{'ticket':app.issue_ticket()})
            if method=='POST' and path=='/v1/ingest':return self._json(200,app.ingest(body))
            if method=='POST' and path=='/v1/remember':return self._json(200,app.remember(body))
            if method=='POST' and path=='/v1/remember-important':return self._json(200,app.remember_important(body))
            if method=='POST' and path=='/v1/recall':return self._json(200,app.recall(body))
            if method=='POST' and path=='/v1/context/life':return self._json(200,app.life_context(body))
            if method=='POST' and path=='/v1/context/profile':return self._json(200,app.profile_context(body))
            if method=='GET' and path=='/v1/standards':return self._json(200,app.extensions.standards())
            if method=='PATCH' and path=='/v1/standards':return self._json(200,app.extensions.update_standards(body))
            if method=='DELETE' and path.startswith('/v1/standards/'):
                return self._json(200,app.extensions.delete_standard(path[len('/v1/standards/'):]))
            if method=='GET' and path=='/v1/messages':
                items=app.store.list_messages(owner,conversation,limit=limit(),offset=max(0,int(q.get('offset',['0'])[0])))
                return self._json(200,{'items':items,'total':app.store.stats(owner).get('messages',len(items))})
            if method=='GET' and path=='/v1/memories':
                items=app.store.list_memories(owner,kind=q.get('kind',[None])[0],status=q.get('status',[None])[0],limit=limit())
                return self._json(200,{'items':items,'total':len(items)})
            if method=='GET' and path=='/v1/conversations':return self._json(200,{'items':app.store.conversations(owner)})
            if method=='GET' and path.startswith('/v1/evidence/'):
                row=app.store.get_message(owner,int(path.rsplit('/',1)[1]))
                if row is None:raise KeyError('原话不存在或已删除')
                return self._json(200,row)
            if method=='GET' and path.startswith('/v1/trace/'):return self._json(200,app.trace(path.rsplit('/',1)[1]))
            if method=='PATCH' and path.startswith('/v1/memories/'):return self._json(200,app.store.update_memory(owner,path[len('/v1/memories/'):],body))
            if method=='DELETE' and path.startswith('/v1/messages/'):return self._json(200,app.store.delete_message(owner,int(path.rsplit('/',1)[1])))
            if method=='DELETE' and path.startswith('/v1/memories/'):return self._json(200,app.store.delete_memory(owner,path[len('/v1/memories/'):]))
            if method=='GET' and path=='/v1/settings':return self._json(200,app.extensions.settings())
            if method=='PATCH' and path=='/v1/settings':
                result=app.extensions.update_settings(body);app._apply_settings();return self._json(200,result)
            if method=='GET' and path=='/v1/extensions':return self._json(200,app.extensions.catalog())
            if method=='GET' and path=='/v1/profile':
                from .profile_view import snapshot
                return self._json(200,snapshot(app))
            if method=='GET' and path=='/v1/life':return self._json(200,app.life.snapshot())
            if method=='GET' and path=='/v1/life/rules':return self._json(200,app.life.rules())
            if method=='POST' and path=='/v1/life/tick':return self._json(200,app.life.tick())
            if method=='GET' and path=='/v1/life/events':return self._json(200,app.life.events(limit=limit(),conversation_id=conversation))
            if method=='POST' and path=='/v1/life/events':return self._json(200,app.life.add_event(body))
            if method=='DELETE' and path.startswith('/v1/life/events/'):return self._json(200,app.life.delete_event(path.rsplit('/',1)[1]))
            if method=='GET' and path=='/v1/life/tasks':return self._json(200,app.life.tasks(limit=limit()))
            if method=='POST' and path=='/v1/life/tasks':return self._json(200,app.life.add_task(body))
            if method=='PATCH' and path.startswith('/v1/life/tasks/'):return self._json(200,app.life.update_task(path.rsplit('/',1)[1],body))
            if method=='DELETE' and path.startswith('/v1/life/tasks/'):return self._json(200,app.life.delete_task(path.rsplit('/',1)[1]))
            if method=='GET' and path=='/v1/life/books':return self._json(200,app.life.books())
            if method=='POST' and path=='/v1/life/books':return self._json(200,app.life.add_book(body))
            if method=='DELETE' and path.startswith('/v1/life/books/'):return self._json(200,app.life.delete_book(path.rsplit('/',1)[1]))
            if method=='GET' and path=='/v1/life/journal':return self._json(200,app.life.journal(q.get('date',[None])[0]))
            if method=='GET' and path=='/v1/emotion':return self._json(200,app.extensions.emotion())
            if method=='GET' and path=='/v1/emotion/rules':return self._json(200,app.extensions.emotion_rules())
            if method=='GET' and path=='/v1/emotion/events':return self._json(200,app.extensions.emotion_events(limit=limit()))
            if method=='POST' and path=='/v1/emotion/events':return self._json(200,app.extensions.apply_emotion_event(body))
            if method in ('POST','PATCH') and path=='/v1/emotion':return self._json(200,app.extensions.update_emotion(body))
            if method=='GET' and path=='/v1/self':return self._json(200,app.extensions.self_notes())
            if method in ('POST','PATCH','PUT') and path=='/v1/self':return self._json(200,app.extensions.update_self(body))
            if method=='DELETE' and path.startswith('/v1/self/'):return self._json(200,app.extensions.delete_self(path.rsplit('/',1)[1]))
            if method=='POST' and path=='/v1/backup':return self._json(200,app.backup())
            if method=='POST' and path=='/v1/maintenance':return self._json(200,app.maintain())
            if method=='GET' and path.startswith('/v1/backups/'):
                key=path.rsplit('/',1)[1]
                if not key or any(c not in '0123456789abcdefT-' for c in key):raise ValueError('备份标识格式错误')
                target=app.data_dir/'backups'/(key+'.zip')
                if not target.is_file():raise KeyError('备份不存在')
                return self._bytes(200,target.read_bytes(),'application/zip',target.name)
            if method=='GET' and path=='/v1/export':
                value=app.store.export_json(owner);value['preferences']=app.extensions.data
                value['life']=app.life.export()
                return self._bytes(200,json.dumps(value,ensure_ascii=False,indent=2).encode(),'application/json; charset=utf-8','xiaojiu-brain-export.json')
            return self._json(404,error='接口不存在')
        except (ValueError,TypeError,json.JSONDecodeError) as e:return self._json(400,error=str(e)[:300])
        except KeyError:return self._json(404,error='记录不存在或无权访问')
        except Exception as e:return self._json(500,error='操作失败：'+type(e).__name__)
    def do_GET(self):self._dispatch('GET')
    def do_POST(self):self._dispatch('POST')
    def do_PATCH(self):self._dispatch('PATCH')
    def do_PUT(self):self._dispatch('PUT')
    def do_DELETE(self):self._dispatch('DELETE')
    def do_OPTIONS(self):self._json(405,error='不支持跨站调用')
