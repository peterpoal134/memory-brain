/* Pure display mapping. Intensity is decorative, never neural/CPU measurement. */
export function activityView(activity, now=Date.now()/1000) {
  const unavailable={state:'unavailable',label:'活动状态未读取',detail:'等待记忆服务状态',intensity:0};
  if(!activity || activity.kind!=='memory_task_activity' || !Number.isFinite(activity.observed_at) ||
      now-activity.observed_at>8 || activity.observed_at-now>3 || activity.state==='unavailable')return unavailable;
  const states={busy:'记忆任务处理中',recent:'刚完成记忆任务',waiting:'等待后台处理',idle:'记忆服务待机'};
  if(!Object.hasOwn(states,activity.state))return unavailable;
  const running=activity.running||{},recent=activity.recent||{};
  let detail='主聊天的生成状态未接入',intensity=0;
  if(activity.state==='busy'){
    detail=[running.derive>0?'正在整理记忆':'',running.embed>0?'正在建立语义索引':''].filter(Boolean).join(' · ')||'记忆后台任务处理中';
    intensity=.85;
  }else if(activity.state==='recent'){
    detail=[recent.recalls>0?'刚完成原话检索':'',recent.completed_jobs>0?'后台任务已完成':''].filter(Boolean).join(' · ');
    const age=Number.isFinite(activity.last_event_at)?Math.max(0,now-activity.last_event_at):6;
    intensity=.65*Math.max(0,1-age/6);
  }else if(activity.state==='waiting')detail='待处理 '+(Number.isFinite(activity.waiting)?activity.waiting.toLocaleString('zh-CN'):'—')+' 项 · 尚未运行';
  return {state:activity.state,label:states[activity.state],detail,intensity};
}

export function wavePath(time,intensity) {
  const strength=Math.max(0,Math.min(1,Number.isFinite(intensity)?intensity:0)),points=[];
  for(let x=0;x<=300;x+=2){
    const p=((x/300*2.2-time*.55)%1+1)%1;
    const gauss=(center,width)=>Math.exp(-(((p-center)/width)**2));
    const pulse=1.1*gauss(.49,.018)-.48*gauss(.455,.022)-.30*gauss(.53,.023)+.16*gauss(.65,.06);
    const y=22-strength*(17*pulse+1.8*Math.sin(x*.16-time*3));
    points.push((x===0?'M':'L')+x+' '+y.toFixed(2));
  }
  return points.join(' ');
}
