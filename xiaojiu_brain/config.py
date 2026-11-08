from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib, json, math, os, secrets
from pathlib import Path

@dataclass
class Config:
    data_dir: str = './data'
    host: str = '127.0.0.1'
    port: int = 18880
    owner: str = 'local-owner'
    timezone: str = 'UTC'
    api_token: str = ''
    embed_url: str = ''
    embed_model: str = 'bge-m3'
    embed_key_env: str = 'XIAOJIU_EMBED_KEY'
    llm_url: str = ''
    llm_model: str = ''
    llm_key_env: str = 'DEEPSEEK_API_KEY'
    model_timeout: float = 45
    derive_interval: float = 5
    derive_batch_messages: int = 12
    derive_max_wait_seconds: int = 900
    derive_enabled: bool = True
    token_budget: int = 1200
    profile_sources_file: str | None = None

    @classmethod
    def load(cls, path):
        p=Path(path).resolve();values=json.loads(p.read_text(encoding='utf-8-sig'))
        unknown=set(values)-set(cls.__dataclass_fields__)
        if unknown:raise ValueError('Unknown configuration fields: '+','.join(sorted(unknown)))
        cfg=cls(**values)
        if not Path(cfg.data_dir).is_absolute():cfg.data_dir=str((p.parent/cfg.data_dir).resolve())
        if (isinstance(cfg.profile_sources_file,str) and cfg.profile_sources_file and
            '://' not in cfg.profile_sources_file and '\x00' not in cfg.profile_sources_file and
            not Path(cfg.profile_sources_file).is_absolute()):
            cfg.profile_sources_file=str((p.parent/cfg.profile_sources_file).resolve())
        cfg.api_token=os.getenv('XIAOJIU_BRAIN_TOKEN') or cfg.api_token
        cfg.validate();return cfg

    def validate(self):
        if self.profile_sources_file is not None and (not isinstance(self.profile_sources_file,str) or
            not self.profile_sources_file.strip() or '\x00' in self.profile_sources_file or
            '://' in self.profile_sources_file):raise ValueError('profile_sources_file must be a local manifest path')
        if self.host not in ('127.0.0.1','::1','localhost'):raise ValueError('This release supports loopback listening only')
        if not 1024<=int(self.port)<=65535:raise ValueError('Invalid port')
        if not self.owner or len(self.api_token)<24:raise ValueError('A local owner and a management token of at least 24 characters are required')
        if not 128<=int(self.token_budget)<=8192:raise ValueError('token_budget must be 128..8192')
        if isinstance(self.derive_interval,bool) or not isinstance(self.derive_interval,(int,float)) or not 1<=self.derive_interval<=600 or not math.isfinite(self.derive_interval):
            raise ValueError('derive_interval must be a finite number from 1..600 seconds')
        if type(self.derive_batch_messages) is not int or not 1<=self.derive_batch_messages<=12:
            raise ValueError('derive_batch_messages must be an integer from 1..12 messages')
        if type(self.derive_max_wait_seconds) is not int or not 30<=self.derive_max_wait_seconds<=86400:
            raise ValueError('derive_max_wait_seconds must be an integer from 30..86400 seconds')
        for url in [self.embed_url,self.llm_url]:
            if url and not url.startswith(('http://','https://')):raise ValueError('Model URLs must be HTTP(S)')

    def public(self):
        return {'owner':self.owner,'timezone':self.timezone,'host':self.host,'port':self.port,
          'embedding':{'configured':bool(self.embed_url),'model':self.embed_model},
          'derivation':{'configured':bool(self.llm_url and self.llm_model),'model':self.llm_model},
          'profile_sources':{'configured':bool(self.profile_sources_file)},
          'token_budget':self.token_budget,'data_dir':str(Path(self.data_dir).resolve())}

    @classmethod
    def initialize(cls,path,**overrides):
        p=Path(path).resolve()
        if p.exists():raise FileExistsError('Configuration already exists; it was not overwritten')
        p.parent.mkdir(parents=True,exist_ok=True)
        cfg=cls(api_token=secrets.token_hex(32),**overrides);cfg.validate()
        with p.open('x',encoding='utf8') as f:json.dump(asdict(cfg),f,ensure_ascii=False,indent=2);f.write('\n')
        os.chmod(p,0o600);return cfg

def build_id():
    root=Path(__file__).resolve().parent;digest=hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if p.is_file() and p.suffix in ('.py','.html','.css','.js'):
            digest.update(str(p.relative_to(root)).encode());digest.update(p.read_bytes())
    return digest.hexdigest()[:16]
