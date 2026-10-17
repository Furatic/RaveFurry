/* Run with Node; exercises the actual HTML, not a copy of its renderer.
 * Native calls are instrumented for resource ownership. This is not an OBS/AMD
 * VRAM benchmark; the real GLSL is validated separately on a GLES3 context.
 */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../visualizer/obs-background.html'),'utf8');
let script=source.match(/<script>([\s\S]*?)<\/script>/)[1];
script=script.replace("window.addEventListener('pagehide',cleanup,{once:true});", "globalThis.inspect={receive,update,birth,getFlow:()=>flowClock,setFlow:v=>{flowClock=v;},getLights:()=>lights.map(x=>({...x})),getPositions:()=>[...clouds],getSizes:()=>[...sizes],getLightTargets:()=>[...lightTargets],getStats:()=>({rendered,gpuSkipped})};window.addEventListener('pagehide',cleanup,{once:true});");
function harness(search=''){
 let now=0,next=1,gpuBusy=false,fenceFailure=false,draws=0,resizes=0;
 const resources={program:0,buffer:0,shader:0,sync:0,texture:0,fbo:0},peak={...resources};
 const pending=new Map(),timers=new Map(),hooks=new Map(),docHooks=new Map(),sockets=[],observers=[],uniforms={};
 const owned=new Map();let ids=0;
 function acquire(type){const item={type,id:++ids};owned.set(item.id,item);resources[type]++;peak[type]=Math.max(peak[type],resources[type]);return item;}
 function release(type,item){assert(item&&owned.has(item.id),`Double release of ${type}`);assert.equal(item.type,type);owned.delete(item.id);resources[type]--;}
 const gl=new Proxy({COMPILE_STATUS:1,LINK_STATUS:2,FRAMEBUFFER_COMPLETE:3,ALREADY_SIGNALED:4,TIMEOUT_EXPIRED:5,WAIT_FAILED:6,
 createShader:()=>acquire('shader'),deleteShader:s=>release('shader',s),createProgram:()=>acquire('program'),deleteProgram:p=>release('program',p),
 createBuffer:()=>acquire('buffer'),deleteBuffer:b=>release('buffer',b),createTexture:()=>acquire('texture'),deleteTexture:t=>release('texture',t),createFramebuffer:()=>acquire('fbo'),deleteFramebuffer:f=>release('fbo',f),
 getShaderParameter:()=>true,getProgramParameter:()=>true,getAttribLocation:()=>0,getUniformLocation:(_,name)=>name,
 fenceSync:()=>{assert.equal(resources.sync,0,'A second unfinished frame was submitted');return acquire('sync');},deleteSync:s=>release('sync',s),
 clientWaitSync:(s,flags,timeout)=>{assert.equal(flags,0);assert.equal(timeout,0);return fenceFailure?6:gpuBusy?5:4;},isContextLost:()=>false,
 uniform4fv:(name,data)=>{uniforms[name]=Array.from(data);},uniform1fv:(name,data)=>{uniforms[name]=Array.from(data);},uniform2f:(name,a,b)=>{uniforms[name]=[a,b];},
 drawArrays:()=>draws++,
 },{get:(target,key)=>key in target?target[key]:(()=>{})});
 let cw=300,ch=150;
 const canvas={clientWidth:1920,clientHeight:1080,listeners:{},get width(){return cw;},set width(v){cw=v;resizes++;},get height(){return ch;},set height(v){ch=v;resizes++;},getContext:()=>gl,addEventListener(name,cb){this.listeners[name]=cb;}};
 const status={hidden:true,textContent:''};
 class WS{constructor(url){assert.match(url,/^ws:\/\/127\.0\.0\.1:8766\/features$/);sockets.push(this);this.closed=false;}close(){if(!this.closed){this.closed=true;this.onclose?.();}}}
 const doc={hidden:false,getElementById:id=>id==='background'?canvas:status,addEventListener:(k,cb)=>docHooks.set(k,cb),removeEventListener:k=>docHooks.delete(k)};
 const ctx={document:doc,window:{devicePixelRatio:1,addEventListener:(k,cb)=>hooks.set(k,cb),removeEventListener:k=>hooks.delete(k)},location:{search,protocol:'file:',hostname:'',port:''},performance:{now:()=>now},console,Float32Array,Float64Array,URLSearchParams,Math,WebSocket:WS,
 requestAnimationFrame(cb){const id=next++;pending.set(id,cb);return id;},cancelAnimationFrame:id=>pending.delete(id),setTimeout(cb){const id=next++;timers.set(id,cb);return id;},clearTimeout:id=>timers.delete(id),
 ResizeObserver:class{constructor(cb){this.cb=cb;observers.push(this);}observe(){this.cb();}disconnect(){this.closed=true;}}};
 vm.runInNewContext(script,ctx);
 function frames(n,hz=60){for(let i=0;i<n;i++){now+=1000/hz;assert.equal(pending.size,1);const [id,cb]=pending.entries().next().value;pending.delete(id);cb(now);}}
 function packet(extra={}){return {...{version:1,level:.65,bass:.7,mid:.5,treble:.4,activity:.5,bands:Array(48).fill(.3),serverTime:now/1000,kickAt:now/1000,accentAt:now/1000,kickId:1,accentId:1,kick:.7,accent:.5,status:'capturing',device:'test',cloudLevels:[.85,.6,.5,.35],cloudBalance:[.64,.16,.12,.08],cloudIds:[1,1,1,1],cloudAt:Array(4).fill(now/1000),cloudStrength:[.8,.6,.5,.4]},...extra};}
 function send(extra={}){ctx.inspect.receive(JSON.stringify(packet(extra)));}
 function snapshot(label){return {label,music:uniforms.u_music,bands:uniforms['u_bands[0]'],lights:uniforms.u_lights,clouds:uniforms['u_clouds[0]'],motion:uniforms.u_motion,energy:uniforms.u_energy,sizes:uniforms.u_sizes,edgeEnergy:uniforms.u_edgeEnergy,edgeLights:uniforms.u_edgeLights,edgeMusic:uniforms.u_edgeMusic,edgeBands:uniforms['u_edgeBands[0]']};}
 return {ctx,gl,doc,canvas,status,resources,peak,pending,timers,hooks,docHooks,sockets,observers,uniforms,frames,packet,send,snapshot,get now(){return now;},get draws(){return draws;},get resizes(){return resizes;},set busy(v){gpuBusy=v;},set fenceFailure(v){fenceFailure=v;},lose(){canvas.listeners.webglcontextlost({preventDefault(){}});for(const type of Object.keys(resources))resources[type]=0;owned.clear();},restore(){canvas.listeners.webglcontextrestored();},cleanup(){hooks.get('pagehide')();}};
}
const h=harness(),states=[];
h.sockets[0].onopen();h.send();h.frames(1);
assert(h.ctx.inspect.getLights().every(l=>l.strength===0),'New connections must not replay old onsets');
h.send({cloudIds:[2,2,2,2]});h.frames(8);states.push(h.snapshot('live-rhythmic'));
assert.deepEqual([...new Set(h.ctx.inspect.getLights().filter(l=>l.strength>0).map(l=>l.cloud))],[0,1,2,3],'All four live clouds must light independently');
h.send({cloudIds:[2,2,2,3]});h.frames(2);assert.equal(h.ctx.inspect.getLights().filter(l=>l.strength>0).length,5,'Green-only hits must not reset or relight other clouds');
h.send({cloudIds:[3,3,3,4],level:.95,activity:1,cloudLevels:[.95,.8,.7,.65]});h.frames(8);states.push(h.snapshot('live-intense'));
// Summing envelopes per cloud must preserve every overlapping original fade.
function validateLightSum(){
 const sums=[0,0,0,0];
 for(const l of h.ctx.inspect.getLights()){
  const age=h.now/1000-l.born;if(!l.strength||age<0||age>=1.9)continue;
  const fade=Math.max(0,Math.min(1,(age-1.45)/.45));
  sums[l.cloud]+=(1-Math.exp(-age/.018))*(Math.exp(-age/.23)+.16*Math.exp(-age/.75))*(1-fade*fade*(3-2*fade))*l.strength;
 }
 for(let i=0;i<4;i++)assert(Math.abs(sums[i]-h.ctx.inspect.getLightTargets()[i])<.00001,'GPU light summation must preserve the original envelopes');
}
validateLightSum();
const earlyDraws=h.draws;h.busy=true;for(let i=0;i<400;i++){h.canvas.clientWidth=1280;h.observers[0].cb();h.frames(1);}
assert.equal(h.draws,earlyDraws,'A busy GPU must not accumulate more draws');assert.equal(h.resources.sync,1);assert.equal(h.peak.sync,1);assert.equal(h.canvas.width,1920,'Resize must wait for the owned frame');
h.busy=false;h.frames(1);assert.equal(h.canvas.width,1280);assert.equal(h.resources.sync,1);
const allocations=h.resizes;for(let i=0;i<1000;i++){h.observers[0].cb();h.frames(1);}assert.equal(h.resizes,allocations,'Unchanged ResizeObserver notifications must not reset the drawing buffer');
// Unpredictable flow, bounded speed/size and no population growth over 60,000 frames.
let minSeparation=1,maxStep=0,previous=h.ctx.inspect.getPositions();const bounds=[1,0,1,0],visits=Array.from({length:4},()=>new Set());
for(let n=0;n<60000;n++){
 h.frames(1);const centers=h.ctx.inspect.getPositions();
 for(let i=0;i<4;i++){const x=centers[i*4],y=centers[i*4+1];bounds[0]=Math.min(bounds[0],x);bounds[1]=Math.max(bounds[1],x);bounds[2]=Math.min(bounds[2],y);bounds[3]=Math.max(bounds[3],y);visits[i].add((x>.5?1:0)+(y>.5?2:0));maxStep=Math.max(maxStep,Math.hypot(x-previous[i*4],y-previous[i*4+1]));for(let j=0;j<i;j++)minSeparation=Math.min(minSeparation,Math.hypot(x-centers[j*4],y-centers[j*4+1]));}
 previous=centers;if(n===10000)states.push(h.snapshot('fluid-drift'));
}
assert(bounds.every(Number.isFinite));assert(bounds[0]>.02&&bounds[1]<.98&&bounds[2]>.02&&bounds[3]<.98,JSON.stringify(bounds));
assert(minSeparation>.20,'Clouds must retain distinct centers');assert(maxStep<.0003,'Motion must not teleport');assert(visits.every(v=>v.size>=3),'Each cloud must travel into other areas');
assert.equal(h.peak.texture,0);assert.equal(h.peak.fbo,0);assert.equal(h.peak.program,1);assert.equal(h.peak.buffer,1);assert.equal(h.peak.sync,1);assert.equal(h.resources.shader,0);
assert(h.uniforms.u_music.every(v=>v<.0001));states.push(h.snapshot('idle'));
// Smooth, spectrally-driven sizes: bass or treble can dominate without equal sizes.
for(let n=0;n<120;n++){h.send({cloudIds:[10,10,10,10],level:.8,cloudLevels:[.9,.1,.1,.05],cloudBalance:[.97,.01,.015,.005]});h.frames(1);}states.push(h.snapshot('bass-dominant'));
const bassSizes=h.ctx.inspect.getSizes();assert(bassSizes[0]>bassSizes[3]+.20);
h.send({cloudIds:[11,11,11,11],cloudLevels:[.05,.10,.10,.95],cloudBalance:[.005,.015,.01,.97]});const size0=h.ctx.inspect.getSizes();h.frames(1);assert(Math.max(...h.ctx.inspect.getSizes().map((v,i)=>Math.abs(v-size0[i])))<.05,'Size targets must ease, not snap');
for(let i=0;i<90;i++){h.send({cloudLevels:[.05,.1,.1,.95],cloudBalance:[.005,.015,.01,.97]});h.frames(2);}states.push(h.snapshot('treble-dominant'));assert(h.ctx.inspect.getSizes()[3]>h.ctx.inspect.getSizes()[0]+.12);
// OBS hiding does no rendering/reallocation. Showing resumes one loop/socket.
const hiddenDraws=h.draws;h.hooks.get('obsSourceVisibleChanged')({detail:{visible:false}});assert.equal(h.pending.size,0);assert(h.sockets.at(-1).closed);
h.doc.hidden=true;h.docHooks.get('visibilitychange')();h.hooks.get('obsSourceVisibleChanged')({detail:{visible:true}});assert.equal(h.pending.size,0);
h.doc.hidden=false;h.docHooks.get('visibilitychange')();assert.equal(h.pending.size,1);h.frames(2);assert(h.draws>hiddenDraws);h.sockets.at(-1).onopen();h.send({cloudIds:[1000,1000,1000,1000]});h.frames(1);assert(h.ctx.inspect.getLights().every(l=>l.strength===0));
for(let n=0;n<300;n++){h.frames(1);if(n%10===0)for(let i=0;i<4;i++)h.ctx.inspect.birth(i,.7,h.now/1000+.25);}assert.equal(h.ctx.inspect.getLights().length,96);states.push(h.snapshot('dense-onsets'));validateLightSum();
h.ctx.inspect.setFlow(60*60*24*14);h.frames(2);assert(h.uniforms.u_motion.every(v=>Math.abs(v)<=1));states.push(h.snapshot('two-weeks'));
for(let i=0;i<25;i++){h.lose();assert.equal(h.pending.size,0);h.restore();h.frames(2);assert.equal(h.resources.program,1);assert.equal(h.resources.buffer,1);h.restore();assert.equal(h.resources.program,1,'Duplicate restoration events must not allocate another program');assert.equal(h.pending.size,1);}
h.cleanup();assert.equal(h.pending.size,0);assert.equal(h.timers.size,0);assert(Object.values(h.resources).every(v=>v===0),'All owned native objects must be released');
// Pacing at high refresh rates must target 60, not 144 or a naive 48 FPS cap.
for(const hz of [60,120,144,240]){const p=harness('?demo=rhythm');p.frames(hz*4,hz);assert(p.draws>=238&&p.draws<=241,`${hz} Hz rendered ${p.draws} frames`);p.cleanup();}
// A bad native completion check must fail closed, never create an unlimited queue.
const broken=harness();broken.frames(1);broken.fenceFailure=true;broken.frames(1);assert.equal(broken.pending.size,0);assert.equal(broken.resources.sync,0);assert.equal(broken.resources.program,0);

// Optional rhythm metadata changes flow speed, never cloud position directly.
const rates={};
for(const [name,level,rhythmDrive] of [['quiet',.25,1],['loudCalm',.95,0],['loudRhythm',.95,1]]){
 const p=harness();p.sockets[0].onopen();p.frames(1);
 for(let n=0;n<600;n++){p.send({level,activity:1,rhythmDrive});p.frames(1);}
 const start=p.ctx.inspect.getFlow();
 for(let n=0;n<120;n++){p.send({level,activity:1,rhythmDrive});p.frames(1);}
 rates[name]=(p.ctx.inspect.getFlow()-start)/2;
 p.cleanup();assert(Object.values(p.resources).every(v=>v===0));
}
assert(Math.abs(rates.quiet-1.27)<.001,'Quiet movement must retain the previous speed even with high rhythm activity');
assert(rates.loudRhythm>rates.loudCalm+.18);assert(rates.loudRhythm<=1.921);
const output=process.argv.indexOf('--states');if(output>=0)fs.writeFileSync(process.argv[output+1],JSON.stringify(states));
console.log(JSON.stringify({passed:true,frames:60000,maxLiveResources:h.peak,flowBounds:bounds,minSeparation,maxStep,visitedQuadrants:visits.map(v=>v.size),checks:'GPU backpressure, unchanged resize, visibility, context recovery, all-cloud pulses, size smoothing, high-refresh pacing and disposal'}));
