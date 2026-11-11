import assert from 'node:assert/strict';
import {activityView,wavePath} from '../xiaojiu_brain/static/brain-activity.js';

const fixture={kind:'memory_task_activity',observed_at:100,state:'idle',running:{derive:0,embed:0},recent:{recalls:0,completed_jobs:0},waiting:0,last_event_at:null};
assert.equal(activityView(null,100).state,'unavailable');
assert.equal(activityView(fixture,109).state,'unavailable');
assert.equal(activityView({...fixture,observed_at:105},100).state,'unavailable');
assert.equal(activityView(fixture,100).intensity,0);
assert.equal(wavePath(0,0),wavePath(10,0));
assert.match(activityView({...fixture,state:'busy',running:{derive:1,embed:0}},100).detail,/整理/);
assert.ok(activityView({...fixture,state:'busy'},100).intensity>0);
assert.ok(activityView({...fixture,state:'recent',last_event_at:99,recent:{recalls:1}},100).intensity>activityView({...fixture,state:'recent',last_event_at:99},104).intensity);
assert.equal(activityView({...fixture,state:'waiting',waiting:3},100).intensity,0);
assert.doesNotMatch(wavePath(2,.8),/NaN|Infinity/);
console.log('PASS 10 activity display checks: stale/unknown, idle flatline, real-task pulse, bounded recent decay.');
