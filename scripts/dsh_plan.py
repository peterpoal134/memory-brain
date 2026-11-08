"""Generate a reviewable DSH insertion plan, never modifies a live profile."""
from pathlib import Path
import argparse, json, hashlib
from xiaojiu_brain.config import Config

def main():
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True);p.add_argument('--config',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();root=Path(__file__).resolve().parents[1];runtime=Path(a.runtime).resolve();out=Path(a.out).resolve()
    config=Config.load(a.config);loop=runtime/'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js'
    session=runtime/'node_modules/@deepseek-ai/dsh-session/lib/index.js'
    if not loop.is_file() or not session.is_file():raise SystemExit('Installed DSH modules not found; no files changed.')
    code=loop.read_text(encoding='utf8');hook='withRequestMessages' in code and 'decision.requestMessages' in code
    out.mkdir(parents=True,exist_ok=True)
    snippet=[{'insert':[{'id':'xiaojiu-brain','name':(root/'adapters/dsh/xiaojiu-brain.mjs').as_uri(),'config':{
      'enabled':True,'baseUrl':f'http://127.0.0.1:{config.port}','queueDir':str(Path(config.data_dir)/'dsh-queue'),
      'sessionModule':session.as_uri(),'captureEnabled':True,'recallEnabled':True,'timeoutMs':3000,'tokenBudget':config.token_budget}}]}]
    # JSON is a valid YAML subset. No secret or mutable production patch is copied.
    (out/'candidate-snippet.yml').write_text(json.dumps(snippet,ensure_ascii=False,indent=2),encoding='utf8')
    plan={'applied':False,'runtime':str(runtime),'request_hook_present':hook,
      'runtime_loop_sha256':hashlib.sha256(loop.read_bytes()).hexdigest(),
      'disable_duplicate_memory_plugins':['fish-self','fish-rawqueue','fish-rawrecall','fish-instant','dsh-persona-memory'],
      'management_token_env':'XIAOJIU_BRAIN_TOKEN','token_value_included':False,
      'model_route_requirement':'Use a direct answer model in the candidate profile; do not leave it pointing to the old memory-core proxy.',
      'preserve':'Existing personality files, tools, user conversations, old brain.db and all production launchers.',
      'rollback':'Stop only the new profile, restore backed-up old profile; preserve/export new raw messages. No database overwrite.'}
    (out/'PLAN.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'plan':str(out/'PLAN.json'),'applied':False,'request_hook_present':hook},ensure_ascii=False))

if __name__=='__main__':main()
