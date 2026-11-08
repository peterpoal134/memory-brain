"""Persistent trusted-browser sessions; no raw credential is saved server-side."""
from __future__ import annotations

from email.utils import formatdate
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time


class BrowserSessions:
    TTL = 30 * 86400
    MAX_SESSIONS = 32

    def __init__(self, path, management_token, *, ttl=TTL):
        self.path = Path(path)
        self.ttl = int(ttl)
        if not 1 <= self.ttl <= self.TTL:raise ValueError('invalid browser session lifetime')
        self.binding = hmac.new(str(management_token).encode(), b'memory-brain-browser-sessions-v1', hashlib.sha256).hexdigest()
        self.lock = threading.RLock()
        self.sessions = {}
        self._load()

    @staticmethod
    def _digest(token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):return None
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def _load(self):
        try:
            if self.path.stat().st_size > 65536:return
            raw=json.loads(self.path.read_text(encoding='utf8'))
            if raw.get('version')!=1 or not hmac.compare_digest(str(raw.get('binding','')),self.binding):return
            records=raw.get('sessions');now=time.time()
            if not isinstance(records,dict) or len(records)>self.MAX_SESSIONS:return
            accepted={}
            for key,entry in records.items():
                if not re.fullmatch(r'[0-9a-f]{64}',key) or not isinstance(entry,dict):return
                start,end=entry.get('created_at'),entry.get('expires_at')
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (start,end)):return
                if not 0<end-start<=self.TTL or start>now+60:return
                if end>now:accepted[key]={'created_at':start,'expires_at':end}
            self.sessions=accepted
        except (OSError,ValueError,TypeError,AttributeError):
            # A broken login cache cannot grant access or stop the memory service.
            self.sessions={}

    def _save(self, records):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=self.path.parent,
                    prefix='.browser-sessions-',suffix='.tmp',delete=False) as stream:
                temporary=Path(stream.name)
                json.dump({'version':1,'binding':self.binding,'sessions':records},stream,separators=(',',':'),allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o600)
            os.replace(temporary,self.path)
        finally:
            if temporary is not None:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass

    def issue(self, replace_token=None):
        with self.lock:
            now=time.time();token=secrets.token_urlsafe(32);key=self._digest(token)
            records={k:dict(v) for k,v in self.sessions.items() if v['expires_at']>now}
            old=self._digest(replace_token)
            if old:records.pop(old,None)
            while len(records)>=self.MAX_SESSIONS:
                records.pop(min(records,key=lambda k:records[k]['created_at']))
            records[key]={'created_at':now,'expires_at':now+self.ttl}
            self._save(records)
            self.sessions=records
            return token,now+self.ttl

    def valid(self, token):
        key=self._digest(token)
        if not key:return False
        with self.lock:
            entry=self.sessions.get(key)
            return bool(entry and entry['expires_at']>time.time())

    def revoke(self, token):
        key=self._digest(token)
        with self.lock:
            if key not in self.sessions:return
            records={k:dict(v) for k,v in self.sessions.items() if k!=key}
            self._save(records)
            self.sessions=records


def browser_cookie(name, token='', *, expires_at=None, max_age=0):
    # Host-only. Separate cookie names prevent different local ports colliding.
    expires=formatdate(expires_at if expires_at is not None else 0,usegmt=True)
    return f'{name}={token}; Path=/v1; HttpOnly; SameSite=Strict; Max-Age={int(max_age)}; Expires={expires}'
