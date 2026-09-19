const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync('frontend/merchant.js','utf8');
function harness(respond=()=>({answer:{hinglish:'Backend answer'}})) {
  const elements=new Map(),calls=[],timers=new Map();let timerId=0;
  const el=id=>{if(!elements.has(id))elements.set(id,{id,textContent:'',hidden:true,disabled:false,value:'',style:{},dataset:{},classList:{add(){},remove(){}},setAttribute(){},focus(){}});return elements.get(id);};
  const context={URLSearchParams,AbortSignal,console,location:{search:'',protocol:'http:',host:'localhost',reload(){}},document:{body:{dataset:{}},getElementById:el,querySelectorAll:()=>[]},window:{},navigator:{},addEventListener(){},setInterval(){},requestAnimationFrame(){},setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),WebSocket:class{},fetch:async(path,options)=>{calls.push({path,options});const result=path==='/api/config'?{voice_server_side:false}:await respond(path,options);return {ok:!result.error,json:async()=>result};}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,el,calls,timers,run:code=>vm.runInContext(code,context)};
}
test('typed questions use the API and cannot double-submit while in flight',async()=>{
  let release;const h=harness(()=>new Promise(resolve=>release=resolve));
  const first=h.run("submit('sales?')");await h.run("submit('second?')");
  assert.equal(h.calls.filter(c=>c.path==='/api/query').length,1);
  assert.equal(JSON.parse(h.calls.find(c=>c.path==='/api/query').options.body).source,'text');
  release({answer:{hinglish:'Computed answer'}});await first;
  assert.equal(h.el('caption').textContent,'Computed answer');assert.equal(h.context.document.body.dataset.mode,'idle');
});
test('payment demo does not call any payment or business mutation endpoint',()=>{
  const h=harness();h.el('payment-demo').onclick();
  assert.equal(h.calls.filter(c=>c.path!=='/api/config').length,0);
  assert.match(h.el('caption-label').textContent,/DEMO PAYMENT/);assert.equal(h.el('payment-toast').hidden,false);
});
test('negative approval wins over an embedded yes',async()=>{
  const h=harness(()=>({state:'rejected'}));h.run("armProposal('r1')");
  await h.run("submit('nahi haan')");
  assert.ok(h.calls.some(c=>c.path==='/api/action/r1/reject'));assert.ok(!h.calls.some(c=>c.path.endsWith('/approve')));
});
test('failed approval never announces a running campaign or measures it',async()=>{
  const h=harness(()=>({error:'unknown_run'}));h.run("armProposal('r1')");await h.run('approve()');
  assert.match(h.el('caption').textContent,/pushti nahi hui/);
  assert.ok(![...h.timers.values()].some(t=>t.delay===3400));
});
test('n8n queued approval is accepted and does not duplicate measurement',async()=>{
  const h=harness(path=>path.endsWith('/approve')?{state:'validating',orchestrator:'n8n'}:{answer:{hinglish:'ok'}});
  h.run("armProposal('r1')");await h.run('approve()');
  assert.match(h.el('caption').textContent,/workflow mein chala gaya/);
  assert.ok(![...h.timers.values()].some(t=>t.delay===3400));
  assert.ok(!h.calls.some(c=>c.path.startsWith('/api/admin/measure')));
});
test('silence expires an offer and a later yes cannot approve it',async()=>{
  const h=harness();h.run("armProposal('r1')");[...h.timers.values()].find(t=>t.delay===15000).fn();await h.run("submit('haan')");
  assert.ok(!h.calls.some(c=>c.path.endsWith('/approve')));assert.equal(h.el('approval').hidden,true);
});
test('late microphone permission cannot leave a live stream after stop',async()=>{
  const h=harness();let release,stopped=false;
  h.context.navigator.mediaDevices={getUserMedia:()=>new Promise(resolve=>release=resolve)};
  const start=h.run('startMeter()');h.run('stopMeter()');release({getTracks:()=>[{stop:()=>stopped=true}]});await start;assert.equal(stopped,true);
});
test('speech cancellation settles its promise and returns control',async()=>{
  const h=harness();h.context.window.speechSynthesis={cancel(){},speak(){}};h.context.SpeechSynthesisUtterance=class{};
  const speech=h.run("speak('hello')");h.run('stopSpeaking()');assert.equal(await speech,false);
});
