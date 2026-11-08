from __future__ import annotations
import argparse, contextlib, json, os, subprocess, sys, threading, time, webbrowser
from pathlib import Path
from urllib.request import Request, urlopen
from .config import Config

class InstanceLock:
    """OS lock; a stale PID file alone never authorizes killing another process."""
    def __init__(self,directory):self.path=Path(directory)/'instance.lock';self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True);self.file=self.path.open('a+b')
        try:
            self.file.seek(0);self.file.write(b'0');self.file.flush();self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None;raise RuntimeError('该数据目录已有运行实例；没有启动第二个实例') from None
        return self
    def __exit__(self,*args):
        if not self.file:return
        self.file.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        self.file.close();self.file=None

def base_url(config):
    host='['+config.host+']' if ':' in config.host else config.host
    return f'http://{host}:{config.port}'

def api(config,path,body=None):
    request=Request(base_url(config)+path,data=json.dumps(body).encode() if body is not None else None,
      headers={'Authorization':'Bearer '+config.api_token,'Content-Type':'application/json'})
    # Full-database backups can exceed an interactive status request budget.
    with urlopen(request,timeout=120 if path=='/v1/backup' else 10) as response:result=json.load(response)
    if not result.get('ok'):raise RuntimeError('服务拒绝操作')
    return result['data']

def open_panel(config,app=None):
    ticket=app.issue_ticket() if app else api(config,'/v1/session/ticket',{})['ticket']
    webbrowser.open(base_url(config)+'/brain#ticket='+ticket)
    return base_url(config)+'/brain'

def main(argv=None):
    parser=argparse.ArgumentParser(description='忆核大脑 Memory Brain：独立记忆服务与本机控制面板')
    parser.add_argument('--config',default=str(Path.home()/'.xiaojiu-brain'/'config.json'))
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='创建独立配置，不覆盖已有文件')
    init.add_argument('--data-dir',default='./data');init.add_argument('--port',type=int,default=18880)
    init.add_argument('--owner',default='local-owner');init.add_argument('--timezone',default='Asia/Shanghai')
    init.add_argument('--embed-url',default='');init.add_argument('--embed-model',default='bge-m3')
    init.add_argument('--llm-url',default='');init.add_argument('--llm-model',default='')
    serve=sub.add_parser('serve',help='启动服务');serve.add_argument('--open-panel',action='store_true')
    start=sub.add_parser('start',help='在后台启动本机服务');start.add_argument('--open-panel',action='store_true')
    for name,helptext in [('open','打开并登录控制面板'),('status','查看运行状态'),('stop','正常停止此配置对应的服务'),('backup','在线备份')]:sub.add_parser(name,help=helptext)
    imp=sub.add_parser('import-legacy',help='只读导入旧库；默认只预览')
    imp.add_argument('--source',required=True);imp.add_argument('--apply',action='store_true');imp.add_argument('--origin-key')
    imp.add_argument('--private-notes',choices=['explicit_only','owner'],default='explicit_only',
        help='旧private笔记：默认保守；owner保留同一使用者的私聊可见性，显式禁说规则不变')
    args=parser.parse_args(argv)
    try:
        if args.command=='init':
            values={k:getattr(args,k) for k in ['data_dir','port','owner','timezone','embed_url','embed_model','llm_url','llm_model']}
            Config.initialize(args.config,**values)
            print('独立配置已创建：'+str(Path(args.config).resolve()));print('管理口令保存在本机配置中，不会输出。');return 0
        config=Config.load(args.config)
        if args.command=='start':
            try:api(config,'/v1/status');running=True
            except Exception:running=False
            if not running:
                logdir=Path(config.data_dir)/'logs';logdir.mkdir(parents=True,exist_ok=True)
                flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
                with (logdir/'service.log').open('ab') as logfile:
                    process=subprocess.Popen([sys.executable,'-m','xiaojiu_brain','--config',str(Path(args.config).resolve()),'serve'],
                      stdin=subprocess.DEVNULL,stdout=logfile,stderr=logfile,creationflags=flags,start_new_session=os.name!='nt')
                for _ in range(50):
                    try:api(config,'/v1/status');running=True;break
                    except Exception:
                        if process.poll() is not None:break
                        time.sleep(.2)
                if not running:raise RuntimeError('新服务未就绪，请查看该数据目录 logs/service.log；未停止其它服务')
            if args.open_panel:open_panel(config)
            print('忆核大脑已就绪：'+base_url(config)+'/brain');return 0
        if args.command=='open':print('3D大脑主页：'+open_panel(config));return 0
        if args.command in ('status','backup','stop'):
            endpoint={'status':'/v1/status','backup':'/v1/backup','stop':'/v1/admin/shutdown'}[args.command]
            result=api(config,endpoint,None if args.command=='status' else {})
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.command=='import-legacy':
            from .migration import import_legacy
            from .storage import Store
            with InstanceLock(config.data_dir):
                store=Store(Path(config.data_dir)/'brain.db')
                try:result=import_legacy(store,args.source,config.owner,apply=args.apply,origin_key=args.origin_key,
                    private_notes_policy=args.private_notes)
                finally:store.close()
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result.get('ok') else 1
        if args.command=='serve':
            from .app import BrainApp
            from .api import BrainServer
            with InstanceLock(config.data_dir):
                app=BrainApp(config);server=None
                try:
                    server=BrainServer((config.host,config.port),app)
                    if args.open_panel:
                        timer=threading.Timer(.35,lambda:open_panel(config,app));timer.daemon=True;timer.start()
                    print('忆核大脑已启动：'+base_url(config)+'/brain',flush=True)
                    print('数据目录：'+str(Path(config.data_dir).resolve()),flush=True)
                    server.serve_forever(poll_interval=.2)
                except KeyboardInterrupt:pass
                finally:
                    if server:server.server_close()
                    app.close()
            return 0
    except Exception as exc:
        # Avoid dumping HTTP requests, configuration, model key or private source text.
        print('操作未完成：'+type(exc).__name__+('：'+str(exc) if isinstance(exc,(ValueError,FileExistsError,RuntimeError)) else ''),file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
