/* Merchant conversation + Soundbox controls. All business answers come from the
 * existing API. Only the explicitly labelled payment demo is local simulation. */
const MERCHANT = new URLSearchParams(location.search).get('m') || 'M001';
const sessionStore = globalThis.sessionStorage;
const conversationKey = `saathi-conversation-${MERCHANT}`;
const CONVERSATION = sessionStore?.getItem(conversationKey)
  || globalThis.crypto?.randomUUID?.()
  || `saathi-${Date.now()}-${Math.random().toString(16).slice(2)}`;
sessionStore?.setItem(conversationKey, CONVERSATION);
const $ = id => document.getElementById(id);
let mode='idle', busy=false, lastAnswer='', lastSpoken='', lastQuestion='';
let pendingRun=null, pendingAsk=null, yesTimer=null, volume=.8, serverSide=false;
let rec=null, micStream=null, micContext=null, meterGeneration=0;
let audio=null, utterance=null, speechGeneration=0, finishSpeech=null;
let paymentTimer=null, queuedAlert=null;
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;

function setMode(next,text) {
  mode=next; document.body.dataset.mode=next;
  $('state').textContent=text ?? ({idle:'Baat karein?',listening:'Sun raha hoon…',thinking:'Soch raha hoon…',speaking:'Saathi bol raha hai…',alert:'Saathi ka ek zaroori message'}[next]);
  $('talk-label').textContent=next==='listening'?'Sun raha hoon · Stop':next==='speaking'?'Bol raha hoon · Stop':'Saathi se baat karein';
  for(const id of ['talk','device-talk','tgo']) $(id).disabled=busy;
  document.querySelectorAll('.scenario').forEach(el=>el.disabled=busy||!!pendingRun);
  $('replay').disabled=!lastAnswer||busy;
  $('device-replay').disabled=!lastSpoken||busy;
  $('approve').disabled=busy; $('reject').disabled=busy;
}
function caption(text,label='SAATHI KA JAWAAB') {$('caption').textContent=text;$('caption-label').textContent=label;}
async function api(path,body) {
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
  const data=await response.json();
  if(!response.ok) throw new Error(data.error||`HTTP ${response.status}`);
  return data;
}
function clearProposal() {clearTimeout(yesTimer);pendingRun=null;$('approval').hidden=true;}
function armProposal(runId) {
  clearProposal();pendingRun=runId;$('approval').hidden=false;
  yesTimer=setTimeout(()=>{clearProposal();setMode(mode);$('caption-label').textContent='APPROVAL EXPIRED · ASK AGAIN TO CREATE AN OFFER';},15000);
}

/* Release both browser recognition and our optional amplitude meter. */
function stopMeter() {
  meterGeneration++;
  if(micStream)micStream.getTracks().forEach(t=>t.stop());micStream=null;
  if(micContext)micContext.close().catch(()=>{});micContext=null;
  document.querySelectorAll('#wave i').forEach(el=>el.style.height='');
}
async function startMeter() {
  const generation=++meterGeneration;
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    if(generation!==meterGeneration){stream.getTracks().forEach(t=>t.stop());return;}
    micStream=stream;micContext=new (window.AudioContext||window.webkitAudioContext)();
    const analyser=micContext.createAnalyser();analyser.fftSize=512;
    micContext.createMediaStreamSource(stream).connect(analyser);
    const values=new Uint8Array(analyser.frequencyBinCount);
    function tick(){if(generation!==meterGeneration)return;analyser.getByteTimeDomainData(values);let sum=0;for(const v of values)sum+=(v-128)**2;const level=Math.min(1,Math.sqrt(sum/values.length)/26);document.querySelectorAll('#wave i').forEach((el,i)=>el.style.height=`${4+level*(12-Math.abs(i-2)*3)}px`);requestAnimationFrame(tick);}
    tick();
  } catch { /* Recognition can still work if the optional meter is unavailable. */ }
}
function stopListening(){const current=rec;rec=null;if(current)current.abort();if(recorder)recorder.finish(false);stopMeter();}

/* Server-side capture, used when /api/config reports voice_server_side.
 * Sarvam accepts wav and mp3; MediaRecorder in Chrome produces webm/opus,
 * which it rejects. So we take raw PCM off an AudioContext and encode a
 * 16 kHz mono WAV ourselves. Capture ends on ~1.2s of quiet after speech,
 * or a 12s hard cap, or the merchant pressing stop. */
let recorder=null;
function downsampleTo16k(data,rate) {
  if(rate===16000)return data;
  const ratio=rate/16000,out=new Float32Array(Math.floor(data.length/ratio));
  for(let i=0;i<out.length;i++)out[i]=data[Math.floor(i*ratio)];
  return out;
}
function encodeWav(samples) {
  const buffer=new ArrayBuffer(44+samples.length*2),view=new DataView(buffer);
  const str=(off,s)=>{for(let i=0;i<s.length;i++)view.setUint8(off+i,s.charCodeAt(i));};
  str(0,'RIFF');view.setUint32(4,36+samples.length*2,true);str(8,'WAVE');str(12,'fmt ');
  view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);
  view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);
  view.setUint16(34,16,true);str(36,'data');view.setUint32(40,samples.length*2,true);
  let off=44;
  for(const s of samples){const v=Math.max(-1,Math.min(1,s));view.setInt16(off,v<0?v*0x8000:v*0x7fff,true);off+=2;}
  return new Blob([buffer],{type:'audio/wav'});
}
async function listenServerSide() {
  const generation=++meterGeneration;
  let stream;
  try{stream=await navigator.mediaDevices.getUserMedia({audio:true});}
  catch{setMode('idle','Mic permission needed · You can type instead');showTyped();return;}
  if(generation!==meterGeneration){stream.getTracks().forEach(t=>t.stop());return;}
  micStream=stream;
  const context=new (window.AudioContext||window.webkitAudioContext)();
  micContext=context;
  const source=context.createMediaStreamSource(stream);
  const node=context.createScriptProcessor(4096,1,1);
  const chunks=[];let spoke=false,quiet=0,seconds=0,stopped=false;
  const finish=async send=>{
    if(stopped)return;
    stopped=true;recorder=null;
    try{node.disconnect();source.disconnect();}catch{}
    stopMeter();
    if(!send||!chunks.length){if(mode==='listening')setMode('idle');return;}
    const total=chunks.reduce((n,c)=>n+c.length,0);
    const flat=new Float32Array(total);let o=0;
    for(const c of chunks){flat.set(c,o);o+=c.length;}
    setMode('thinking','Samajh raha hoon…');
    try {
      const form=new FormData();
      form.append('audio',encodeWav(downsampleTo16k(flat,context.sampleRate)),'speech.wav');
      const response=await fetch('/api/stt',{method:'POST',body:form,signal:AbortSignal.timeout(15000)});
      const data=await response.json();
      if(data.ok&&(data.text||'').trim()){submit(data.text,'voice');return;}
    } catch { /* fall through to the browser recogniser */ }
    // Sarvam did not answer in time. Say nothing about it and let the
    // merchant speak again into the browser's own recogniser.
    listenBrowser();
  };
  recorder={finish};
  node.onaudioprocess=e=>{
    if(generation!==meterGeneration){finish(false);return;}
    const input=e.inputBuffer.getChannelData(0);
    chunks.push(new Float32Array(input));
    let sum=0;for(const v of input)sum+=v*v;
    const level=Math.sqrt(sum/input.length);
    document.querySelectorAll('#wave i').forEach((el,i)=>el.style.height=`${4+Math.min(1,level*12)*(12-Math.abs(i-2)*3)}px`);
    const span=input.length/context.sampleRate;seconds+=span;
    if(level>0.015){spoke=true;quiet=0;}
    else if(spoke&&(quiet+=span)>1.2){finish(true);return;}
    if(seconds>12)finish(spoke);
  };
  source.connect(node);node.connect(context.destination);
  setMode('listening');
}

function listen() {
  if(busy)return;
  stopSpeaking();stopListening();
  if(serverSide&&(window.AudioContext||window.webkitAudioContext)&&navigator.mediaDevices)
    return listenServerSide();
  listenBrowser();
}
function listenBrowser() {
  if(!SR){setMode('idle','Mic is unavailable here · Type your question');showTyped();return;}
  const current=new SR();rec=current;current.lang='hi-IN';current.interimResults=false;
  current.onstart=()=>{if(rec!==current)return;setMode('listening');startMeter();};
  current.onresult=e=>{if(rec!==current)return;const text=e.results[0][0].transcript;stopListening();submit(text,'voice');};
  current.onerror=e=>{if(rec!==current)return;stopListening();setMode('idle',e.error==='not-allowed'?'Mic permission needed · You can type instead':'Sun nahi paaya · Phir se try karein');showTyped();};
  current.onend=()=>{if(rec!==current)return;rec=null;stopMeter();if(mode==='listening')setMode('idle');};
  try{setMode('listening','Mic shuru ho raha hai…');current.start();}catch{stopListening();setMode('idle','Mic unavailable · Type your question');showTyped();}
}

/* Cancellation settles the previous promise as well as stopping the sound. */
function stopSpeaking() {
  speechGeneration++;
  if(audio){audio.pause();audio=null;}
  if(window.speechSynthesis)window.speechSynthesis.cancel();utterance=null;
  if(finishSpeech){const finish=finishSpeech;finishSpeech=null;finish(false);}
}
// --- speech: say numbers the way a Hindi speaker says them -----------------
// A hi-IN voice reads "5497" as "five thousand four hundred ninety-seven" in
// English, which is jarring mid-Hinglish. Indian grouping (lakh, hazaar) also
// differs from the Western one, so this spells the figure out rather than
// leaving it to the voice engine. The CAPTION keeps the digits -- only the
// spoken string is rewritten.
// Hindi numbers 0-99 are irregular -- 25 is "pachchees", not "paanch-bees" --
// so they are spelled out rather than composed.
const HI_100=('zero ek do teen chaar paanch chhe saat aath nau das gyarah barah terah '+
  'chaudah pandrah solah satrah atharah unnees bees ikkees baees teees chaubees '+
  'pachchees chhabbees sattaees atthaees unattees tees ikattees battees taintees '+
  'chauntees paintees chhattees saintees adhtees untaalees chalees iktaalees '+
  'bayaalees taintaalees chauvaalees paintaalees chhiyaalees saintaalees adtaalees '+
  'unchaas pachaas ikyaavan baavan tirepan chauvan pachpan chhappan sattavan '+
  'atthaavan unsaath saath iksaath baasath tirsath chausath painsath chhiyasath '+
  'sarsath adsath unhattar sattar ikhattar bahattar tihattar chauhattar pachhattar '+
  'chhihattar sathattar athhattar unyaasi assi ikyaasi bayaasi tirhaasi chauraasi '+
  'pachaasi chhiyaasi sataasi athaasi navaasi nabbe ikyaanve baanve tiraanve '+
  'chauraanve pachaanve chhiyaanve sataanve athaanve ninyaanve').split(' ');
function hiTwo(n){return HI_100[n]||String(n);}
function hiNumber(n){
  n=Math.round(n);
  if(n===0)return 'zero';
  if(n<0)return 'minus '+hiNumber(-n);
  const parts=[];
  const crore=Math.floor(n/10000000); n%=10000000;
  const lakh=Math.floor(n/100000);    n%=100000;
  const hazaar=Math.floor(n/1000);    n%=1000;
  const sau=Math.floor(n/100);        n%=100;
  if(crore)parts.push(`${hiNumber(crore)} crore`);
  if(lakh)parts.push(`${hiTwo(lakh)} lakh`);
  if(hazaar)parts.push(`${hiTwo(hazaar)} hazaar`);
  if(sau)parts.push(`${hiTwo(sau)} sau`);
  if(n)parts.push(hiTwo(n));
  return parts.join(' ');
}
function hiDecimal(raw){
  const value=parseFloat(String(raw).replace(/,/g,''));
  if(Number.isInteger(value))return hiNumber(value);
  // keep the decimal rather than rounding: the figure on screen and the figure
  // spoken aloud must be the same number
  const [whole,frac]=String(value).split('.');
  return `${hiNumber(parseFloat(whole))} point ${frac.split('').map(d=>HI_100[+d]).join(' ')}`;
}
function speakableHindi(text){
  return String(text==null?'':text)
    // "Rs 5,497" / "₹5497" -> "paanch hazaar chaar sau saat-nabbe rupaye"
    .replace(/(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d+)?)/gi,
      (_,d)=>`${hiDecimal(d)} rupaye`)
    // "17.9%" -> "sattar-das point nau percent" is worse than leaving the
    // decimal, so round percentages for speech
    .replace(/([\d,]+(?:\.\d+)?)\s*%/g,
      (_,d)=>`${hiDecimal(d)} percent`)
    // a bare 4+ digit figure that survived the two rules above
    .replace(/(?<![\w.])(\d{1,3}(?:,\d{2,3})+|\d{4,})(?![\w.])/g,
      d=>hiDecimal(d));
}

function speak(text,{remember=true,label='SAATHI KA JAWAAB'}={}) {
  stopSpeaking();stopListening();const generation=speechGeneration;
  lastSpoken=text;if(remember)lastAnswer=text;caption(text,label);
  return new Promise(resolve=>{
    let timer=null;
    const done=completed=>{clearTimeout(timer);if(generation===speechGeneration){finishSpeech=null;audio=null;utterance=null;setMode('idle');}resolve(completed);};
    finishSpeech=done;
    const started=()=>{if(generation!==speechGeneration)return;clearTimeout(timer);setMode('speaking');timer=setTimeout(()=>{stopSpeaking();setMode('idle','Jawab screen par padh sakte hain');},120000);};
    const browserVoice=()=>{
      if(generation!==speechGeneration)return;
      if(!window.speechSynthesis||volume===0){done(true);return;}
      utterance=new SpeechSynthesisUtterance(speakableHindi(text));utterance.lang='hi-IN';utterance.rate=.98;utterance.volume=volume;
      utterance.onstart=started;utterance.onend=()=>done(true);utterance.onerror=()=>done(true);
      // Some embedded browsers expose speech synthesis but never start playback.
      timer=setTimeout(()=>{if(generation!==speechGeneration)return;stopSpeaking();setMode('idle','Audio unavailable · Jawab screen par hai');},6000);
      setMode('thinking','Awaaz taiyaar ho rahi hai…');window.speechSynthesis.speak(utterance);
    };
    if(!serverSide){browserVoice();return;}
    setMode('thinking','Awaaz taiyaar ho rahi hai…');
    api('/api/tts',{text}).then(result=>{
      if(generation!==speechGeneration)return;
      if(!result.ok||!result.audio_b64){browserVoice();return;}
      audio=new Audio('data:audio/wav;base64,'+result.audio_b64);audio.volume=volume;
      audio.onplaying=started;audio.onended=()=>done(true);audio.onerror=browserVoice;
      audio.play().catch(browserVoice);
    }).catch(browserVoice);
  });
}

const YES=/^(haan|han|ha|yes|karo|kar do|kar de|theek hai|thik hai|ok|okay|ji|sahi|हाँ|हां|जी|कर दो|करो)[.!।\s]*$/i;
const NO=/(\bnahi\b|\bnai\b|\bno\b|\bmat\b|rehne do|cancel|नहीं|मत)/i;
async function submit(text,source='text') {
  if(busy||!text.trim())return;
  stopSpeaking();stopListening();
  if(pendingRun&&NO.test(text))return reject();
  if(pendingRun&&YES.test(text))return approve();
  clearProposal();busy=true;caption(text,'AAPNE POOCHHA');setMode('thinking');
  try {
    // Every utterance is an ordinary turn on /api/query. It used to branch
    // here: a reply to a question was posted to /api/context, which stored it
    // as a fact and then re-asked the ORIGINAL question with no conversation
    // attached -- so the merchant heard the same answer again, minus the
    // question. The agent resolves follow-ups against the thread instead.
    lastQuestion=text;
    present(await api('/api/query',{merchant_id:MERCHANT,text,source,conversation_id:CONVERSATION}));
  } catch {busy=false;speak('Abhi connect nahi ho pa raha. Ek minute baad try kijiye.');}
  finally {busy=false;setMode(mode);}
}
function present(result) {
  if(!result.answer?.hinglish)throw new Error('Missing answer');
  busy=false;pendingAsk=result.ask||null;   // kept only so alerts stay quiet mid-question
  if(result.run_id)armProposal(result.run_id);
  speak(result.answer.hinglish);
}
async function reject() {
  if(busy||!pendingRun)return;
  const id=pendingRun;clearProposal();stopSpeaking();stopListening();busy=true;setMode('thinking');
  try{await api(`/api/action/${id}/reject`,{});busy=false;speak('Theek hai, nahi banaya.');}
  catch{busy=false;speak('Confirm nahi ho paaya. Connection check kijiye.');}
  finally{busy=false;setMode(mode);}
}
async function approve() {
  if(busy||!pendingRun)return;
  const id=pendingRun;clearProposal();stopSpeaking();stopListening();busy=true;setMode('thinking','Offer bana raha hoon…');
    try {
      const result=await api(`/api/action/${id}/approve`,{});busy=false;
      if(result.revised){armProposal(id);speak(`Thoda badal raha hoon: ${result.reason}. Theek hai?`);return;}
      if(!['validating','running'].includes(result.state))throw new Error('Campaign not accepted');
      const queued=result.state==='validating';
      speak(queued?'Demo offer workflow mein chala gaya. Status dashboard par dikhega.':'Demo offer chalu ho gaya. Teen din baad result bataunga.',{label:queued?'SIMULATED CAMPAIGN · QUEUED':'SIMULATED CAMPAIGN · APPROVED'});
      // The local adapter needs this demo fast-forward. The n8n workflow owns
      // its measurement and graph write-back, so calling this endpoint as well
      // would race and duplicate the learning loop.
      if(result.orchestrator!=='n8n')setTimeout(async()=>{
        try{const measured=await api(`/api/admin/measure?run_id=${encodeURIComponent(id)}`,{});const o=measured.outcome;if(!o)return;const message=`Demo offer poora hua. Sales mein ${Math.round(Math.abs(o.delta_pct))}% ${o.delta_pct>=0?'badhaav':'giraavat'} aaya. Result save kar liya.`;if(mode==='idle'&&!busy&&!pendingRun&&!pendingAsk)speak(message,{label:'SIMULATED OUTCOME · 3 DAYS FAST-FORWARDED'});else queuedAlert={text:message,label:'SIMULATED OUTCOME · 3 DAYS FAST-FORWARDED'};}catch{/* The ops page retains the action's actual status. */}
      },3400);
  }catch{busy=false;speak('Offer shuru hone ki pushti nahi hui. Dobara poochhiye.');}
  finally{busy=false;setMode(mode);}
}

function talk(){if(busy)return;if(mode==='speaking'||finishSpeech){stopSpeaking();setMode('idle');return;}if(mode==='listening'){if(recorder)recorder.finish(true);else{stopListening();setMode('idle');}return;}listen();}
$('talk').onclick=talk;$('device-talk').onclick=talk;
$('replay').onclick=()=>{if(lastAnswer&&!busy)speak(lastAnswer);};
$('device-replay').onclick=()=>{if(lastSpoken&&!busy)speak(lastSpoken,{remember:false,label:'REPLAY · LAST ANNOUNCEMENT'});};
$('approve').onclick=approve;$('reject').onclick=reject;
function showTyped(open=true){$('typed').hidden=!open;$('type-toggle').setAttribute('aria-expanded',String(open));if(open)$('tin').focus();}
$('type-toggle').onclick=()=>showTyped($('typed').hidden);
$('typed').onsubmit=e=>{e.preventDefault();const text=$('tin').value.trim();if(!text||busy)return;$('tin').value='';submit(text);};
addEventListener('keydown',e=>{if(e.ctrlKey&&e.shiftKey&&e.key.toLowerCase()==='t'){e.preventDefault();showTyped($('typed').hidden);}if(e.code==='Space'&&e.target===document.body){e.preventDefault();talk();}if(e.key==='Escape'){stopListening();stopSpeaking();setMode('idle');}});
function changeVolume(delta){volume=Math.max(0,Math.min(1,Math.round((volume+delta)*10)/10));if(audio)audio.volume=volume;if(utterance)utterance.volume=volume;$('volume-label').textContent=volume?`Vol ${Math.round(volume*100)}%`:'Muted';}
$('volume-down').onclick=()=>changeVolume(-.1);$('volume-up').onclick=()=>changeVolume(.1);
$('payment-demo').onclick=()=>{
  if(busy||pendingRun)return;
  clearTimeout(paymentTimer);$('payment-toast').hidden=false;
  speak('Paytm par ek sau bees rupaye prapt hue.',{remember:false,label:'DEMO PAYMENT · ₹120 · NO MONEY MOVED'});
  paymentTimer=setTimeout(()=>$('payment-toast').hidden=true,6500);
};
for(const el of document.querySelectorAll('[data-question]'))el.onclick=()=>submit(el.dataset.question);
api('/api/config').then(c=>serverSide=!!c.voice_server_side).catch(()=>{});
(function connect(){
  const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
  socket.onopen=()=>{$('connection').innerHTML='<i></i> Saathi connected';$('connection').classList.add('online');};
  socket.onmessage=e=>{let event;try{event=JSON.parse(e.data);}catch{return;}if(event.type==='reset'){location.reload();return;}if(event.type==='proactive_alert'&&event.merchant_id===MERCHANT)queuedAlert={text:event.hinglish,label:'SAATHI KA ALERT'};};
  socket.onclose=()=>{$('connection').innerHTML='<i></i> Reconnecting';$('connection').classList.remove('online');setTimeout(connect,2500);};
})();
setInterval(()=>{if(queuedAlert&&mode==='idle'&&!busy&&!pendingRun&&!pendingAsk){const alert=queuedAlert;queuedAlert=null;speak(alert.text,{label:alert.label});}},1000);
addEventListener('pagehide',()=>{stopListening();stopSpeaking();});
setMode('idle');
