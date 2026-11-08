"""Temporary new-Brain HTTP process; synthetic DB only, no provider calls."""
import json, os, sys, threading, time
from pathlib import Path
if os.environ.get('MEMORY_BRAIN_INSTALLED_ONLY') != '1':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from xiaojiu_brain.config import Config
from xiaojiu_brain.app import BrainApp
from xiaojiu_brain.api import BrainServer

app = BrainApp(Config(data_dir=sys.argv[1], owner='loop-fixture', api_token=os.environ['LOOP_TEST_TOKEN'], derive_enabled=False), start_worker=False)
# Isolated fixture control: emulate a slow retrieval provider without holding
# the SQLite lock. The real HTTP service and real life context remain in use.
fixture_root = Path(sys.argv[1]).parent
delay_file = fixture_root/'recall-delay.json'
timing_file = fixture_root/'fixture-timing.jsonl'
timing_lock = threading.Lock()
def timing(stage):
    with timing_lock:
        with timing_file.open('a', encoding='utf-8') as out:
            out.write(json.dumps({'stage':stage,'at':time.time()})+'\n')
original_recall = app.retriever.recall
def controlled_recall(*args, **kwargs):
    if delay_file.is_file():
        delay = float(json.loads(delay_file.read_text())['seconds'])
        assert 0 <= delay <= 10
        timing('slow_recall_started')
        time.sleep(delay)
        timing('slow_recall_finished')
    return original_recall(*args, **kwargs)
app.retriever.recall = controlled_recall
original_life_context = app.life_context
def observed_life_context(*args, **kwargs):
    result = original_life_context(*args, **kwargs)
    timing('life_context_finished')
    return result
app.life_context = observed_life_context
original_profile_context=app.profile_context
def observed_profile_context(*args,**kwargs):
    result=original_profile_context(*args,**kwargs);timing('profile_context_finished');return result
app.profile_context=observed_profile_context
server = BrainServer(('127.0.0.1', 0), app)
print(json.dumps({'port': server.server_port}), flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    app.close()
