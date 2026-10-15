/* Local turntables: record rotation follows confirmed playback. Standby motion
 * belongs to the scene and never impersonates audio or a sound level meter. */
(() => {
  "use strict";
  const records = [...document.querySelectorAll("[data-riff-record]")];
  if (records.length) {
    const reducedRecordMotion = matchMedia("(prefers-reduced-motion: reduce)");
    const recordToggle = document.getElementById("riff-motion-toggle");
    const recordMotion = () => !reducedRecordMotion.matches && (!recordToggle || recordToggle.getAttribute("aria-pressed") === "true");
    let playing = false, frame = 0, previous = 0, angle = 0;
    const stop = () => { if (frame) cancelAnimationFrame(frame); frame = 0; previous = 0; };
    const tick = now => {
      frame = 0;
      if (!playing || document.hidden || !recordMotion()) return;
      if (previous) angle = (angle + Math.min(now - previous, 100) * .2) % 360;
      previous = now;
      for (const record of records) { record.style.transform = `rotate(${angle}deg)`; record.dataset.angle = angle.toFixed(2); }
      frame = requestAnimationFrame(tick);
    };
    const refresh = () => { stop(); for (const record of records) record.dataset.animating = String(playing && !document.hidden && recordMotion()); if (playing && !document.hidden && recordMotion()) frame = requestAnimationFrame(tick); };
    const playback = event => { playing = event.detail?.state === "playing"; refresh(); };
    document.addEventListener("riff:companion-playback", playback);
    document.addEventListener("riff:catalog-playback", playback);
    document.addEventListener("visibilitychange", refresh);
    reducedRecordMotion.addEventListener("change", refresh);
    if (recordToggle) new MutationObserver(refresh).observe(recordToggle, {attributes: true, attributeFilter: ["aria-pressed"]});
    refresh();
  }
  const canvases = new Set();
  const toggle = document.getElementById("riff-motion-toggle");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const scenes = new Map();
  const panels = new Set();
  const audio = document.getElementById("catalogAudio");
  const dock = document.getElementById("catalogPlayerDock");
  let playbackState = "idle";
  let economy = false;
  let manualMotion = false;
  try { economy = localStorage.getItem("riff-motion") === "off"; } catch (_) { /* Private browsing can disable persistence. */ }
  const motion = () => !economy && (!reduced.matches || manualMotion) && !document.hidden;
  function syncMotion() {
    if (toggle) {
      toggle.textContent = motion() ? "Motion on" : "Motion off";
      toggle.setAttribute("aria-pressed", String(motion()));
      toggle.title = reduced.matches && !manualMotion ? "Your device asks for reduced motion. Enable movement for this visit." : "Pause decorative 3D movement to save power";
    }
    for (const panel of panels) panel.dataset.motion = motion() ? "on" : "off";
    for (const scene of scenes.values()) scene.refresh();
  }
  toggle?.addEventListener("click", () => {
    const enabled = !economy && (!reduced.matches || manualMotion);
    economy = enabled;
    manualMotion = !economy && reduced.matches;
    try { localStorage.setItem("riff-motion", economy ? "off" : "on"); } catch (_) { /* Optional preference only. */ }
    syncMotion();
  });
  reduced.addEventListener("change", () => { manualMotion = false; syncMotion(); });
  document.addEventListener("visibilitychange", syncMotion);

  function syncListening() {
    const current = Boolean(document.querySelector(".catalog-vinyl-record")?.dataset.catalogKey);
    const state = audio?.error || dock?.dataset.previewError === "true" ? "error"
      : !current && dock?.dataset.playbackState !== "loading" ? "idle"
      : dock?.dataset.playbackState || "paused";
    const changed = state !== playbackState;
    playbackState = state;
    const title = document.getElementById("catalogNowTitle")?.textContent;
    const artist = document.getElementById("catalogNowArtist")?.textContent;
    const status = document.getElementById("catalogPlayerStatus")?.textContent;
    const sourceAction = document.getElementById("catalogVinylButton");
    for (const panel of panels) {
      panel.dataset.playbackState = state;
      panel.querySelector("[data-listening-state]").textContent = ({idle: "STANDBY", playing: audio?.muted ? "PLAYING · MUTED" : "PLAYING", loading: "OPENING AUDIO", buffering: "BUFFERING", error: "PREVIEW UNAVAILABLE", paused: "PAUSED"})[state] || "PAUSED";
      panel.querySelector("[data-listening-title]").textContent = current ? title : "Your next record is waiting.";
      panel.querySelector("[data-listening-artist]").textContent = current ? artist : "Browse charts. Hear a preview. Find your next song.";
      panel.querySelector("[data-listening-note]").textContent = state === "idle" ? "Chart audio previews. Your ZIP choices stay separate."
        : state === "paused" ? "Preview paused. Play to continue, or find another record." : status;
      const action = panel.querySelector("[data-listening-action]");
      action.textContent = state === "idle" || sourceAction?.disabled ? "Find a record ↗" : state === "loading" ? "Cancel loading" : state === "playing" || state === "buffering" ? "Pause preview" : "Play preview";
      action.dataset.action = state === "idle" || sourceAction?.disabled ? "browse" : "playback";
    }
    if (changed) for (const scene of scenes.values()) scene.refresh();
  }
  function panelAction(event) {
    if (event.currentTarget.dataset.action === "browse" && window.RiffCatalog) { window.RiffCatalog.browse(); return; }
    const target = event.currentTarget.dataset.action === "browse" ? "catalogBrowseButton" : "catalogVinylButton";
    document.getElementById(target)?.click();
  }
  if (dock) new MutationObserver(syncListening).observe(dock, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["data-playback-state", "data-preview-error", "data-catalog-key", "disabled"]});
  for (const event of ["playing", "pause", "waiting", "error", "volumechange", "emptied"]) audio?.addEventListener(event, syncListening);
  document.addEventListener("riff:catalog-playback", syncListening);
  syncListening();

  const VS = `attribute vec3 aPosition; attribute vec3 aNormal;
    uniform mat4 uModel; uniform mat4 uViewProjection; uniform mat3 uNormal;
    varying vec3 vPosition; varying vec3 vNormal;
    void main(){ vec4 p=uModel*vec4(aPosition,1.0); vPosition=p.xyz;
      vNormal=normalize(uNormal*aNormal); gl_Position=uViewProjection*p; }`;
  const FS = `precision mediump float; varying vec3 vPosition; varying vec3 vNormal;
    uniform vec3 uColor; uniform vec3 uEye; uniform float uMetal;
    void main(){ vec3 n=normalize(vNormal); vec3 view=normalize(uEye-vPosition);
      vec3 l=normalize(vec3(-3.0,6.0,3.0)-vPosition);
      vec3 rim=normalize(vec3(4.0,3.0,-4.0)-vPosition);
      float d=max(dot(n,l),0.0); float r=max(dot(n,rim),0.0);
      float spec=pow(max(dot(n,normalize(l+view)),0.0),46.0)*uMetal;
      vec3 lit=uColor*(0.22+d*0.8)+vec3(0.65,0.5,0.9)*r*0.18+vec3(1.0)*spec*0.9;
      gl_FragColor=vec4(lit,1.0); }`;
  function mul(a,b) {
    const out = new Float32Array(16);
    for(let c=0;c<4;c++) for(let r=0;r<4;r++)
      out[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3];
    return out;
  }
  function model(p,s=[1,1,1],angle=0) {
    const c=Math.cos(angle), n=Math.sin(angle);
    return new Float32Array([c*s[0],0,-n*s[0],0,0,s[1],0,0,n*s[2],0,c*s[2],0,...p,1]);
  }
  const dot = (a,b) => a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
  const sub = (a,b) => a.map((v,i)=>v-b[i]);
  const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const normalize = a => { const n=Math.hypot(...a)||1; return a.map(v=>v/n); };
  function view(eye,target) {
    const z=normalize(sub(eye,target)),x=normalize(cross([0,1,0],z)),y=cross(z,x);
    return new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,-dot(x,eye),-dot(y,eye),-dot(z,eye),1]);
  }
  function perspective(aspect,zoom=1) {
    const f=zoom/Math.tan(Math.PI/7), near=.1, far=30;
    return new Float32Array([f/aspect,0,0,0,0,f,0,0,0,0,(far+near)/(near-far),-1,0,0,2*far*near/(near-far),0]);
  }
  function normals(m) {
    const a=[m[0],m[1],m[2]],b=[m[4],m[5],m[6]],c=[m[8],m[9],m[10]];
    const bc=cross(b,c),ca=cross(c,a),ab=cross(a,b),d=dot(a,bc)||1;
    return new Float32Array([...bc,...ca,...ab].map(v=>v/d));
  }
  function geometry() {
    const data=[];
    function vertex(p,n) { data.push(...p,...n); }
    function triangle(a,b,c,n1,n2=n1,n3=n1) { vertex(a,n1);vertex(b,n2);vertex(c,n3); }
    function quad(a,b,c,d,n) {triangle(a,b,c,n);triangle(a,c,d,n);}
    return {data,vertex,triangle,quad};
  }
  function box() {
    const g=geometry(),p=[[-.5,-.5,-.5],[.5,-.5,-.5],[.5,.5,-.5],[-.5,.5,-.5],[-.5,-.5,.5],[.5,-.5,.5],[.5,.5,.5],[-.5,.5,.5]];
    [[0,3,2,1,[0,0,-1]],[4,5,6,7,[0,0,1]],[0,4,7,3,[-1,0,0]],[1,2,6,5,[1,0,0]],[3,7,6,2,[0,1,0]],[0,1,5,4,[0,-1,0]]].forEach(f=>g.quad(...f.slice(0,4).map(i=>p[i]),f[4]));
    return g.data;
  }
  function cylinder(steps=64) {
    const g=geometry();
    for(let i=0;i<steps;i++) {
      const a=i*Math.PI*2/steps,b=(i+1)*Math.PI*2/steps;
      const n1=[Math.cos(a),0,Math.sin(a)],n2=[Math.cos(b),0,Math.sin(b)];
      const p1=[n1[0],-.5,n1[2]],p2=[n2[0],-.5,n2[2]],p3=[n2[0],.5,n2[2]],p4=[n1[0],.5,n1[2]];
      g.triangle(p1,p2,p3,n1,n2,n2);g.triangle(p1,p3,p4,n1,n2,n1);
      g.triangle([0,.5,0],p4,p3,[0,1,0]);g.triangle([0,-.5,0],p2,p1,[0,-1,0]);
    }
    return g.data;
  }
  function ring(radius,tube=.01,steps=96) {
    const g=geometry(),sides=6;
    function point(i,j) { const a=i*Math.PI*2/steps,b=j*Math.PI*2/sides;
      return {p:[(radius+tube*Math.cos(b))*Math.cos(a),tube*Math.sin(b),(radius+tube*Math.cos(b))*Math.sin(a)],n:[Math.cos(b)*Math.cos(a),Math.sin(b),Math.cos(b)*Math.sin(a)]}; }
    for(let i=0;i<steps;i++) for(let j=0;j<sides;j++) {
      const a=point(i,j),b=point(i+1,j),c=point(i+1,j+1),d=point(i,j+1);
      g.triangle(a.p,b.p,c.p,a.n,b.n,c.n);g.triangle(a.p,c.p,d.p,a.n,c.n,d.n);
    }
    return g.data;
  }
  function grooves() {
    const out=[];
    for(let i=0;i<15;i++) out.push(...ring(.54+i*.061,.0045,80));
    return out;
  }
  function start(canvas) {
    if(scenes.has(canvas)) return scenes.get(canvas);
    const wrap=canvas.parentElement;
    let gl;
    try { gl=canvas.getContext("webgl",{alpha:true,antialias:true,powerPreference:"low-power",preserveDrawingBuffer:false}); } catch (_) { /* The CSS turntable remains visible. */ }
    if(!gl) {canvas.dataset.renderer="css-fallback"; return null;}
    function shader(type,source) {
      const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);
      if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw new Error("Riff decorative shader unavailable");
      return s;
    }
    let program;
    try {
      program=gl.createProgram();const v=shader(gl.VERTEX_SHADER,VS),f=shader(gl.FRAGMENT_SHADER,FS);
      gl.attachShader(program,v);gl.attachShader(program,f);gl.linkProgram(program);
      gl.deleteShader(v);gl.deleteShader(f);
      if(!gl.getProgramParameter(program,gl.LINK_STATUS)) throw new Error("Riff decorative renderer unavailable");
    } catch (_) {canvas.dataset.renderer="css-fallback";return null;}
    gl.useProgram(program);gl.enable(gl.DEPTH_TEST);
    const attr={p:gl.getAttribLocation(program,"aPosition"),n:gl.getAttribLocation(program,"aNormal")};
    const u={}; ["Model","ViewProjection","Normal","Color","Eye","Metal"].forEach(k=>u[k]=gl.getUniformLocation(program,`u${k}`));
    let vertexCount=0;
    function mesh(data) {
      const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(data),gl.STATIC_DRAW);
      vertexCount+=data.length/6;return {buffer,count:data.length/6};
    }
    const meshes={box:mesh(box()),cylinder:mesh(cylinder()),rim:mesh(ring(1.53,.026)),grooves:mesh(grooves())};
    const objects=[];
    function object(kind,p,s,color,metal=.5,angle=0) {const o={mesh:meshes[kind],matrix:model(p,s,angle),color,metal};objects.push(o);return o;}
    function tube(a,b,r,color) {
      const d=sub(b,a),length=Math.hypot(...d),y=normalize(d),x=normalize(cross(y,Math.abs(y[2])>.9?[1,0,0]:[0,0,1])),z=cross(x,y);
      const m=new Float32Array([...x.map(v=>v*r),0,...y.map(v=>v*length),0,...z.map(v=>v*r),0,...a.map((v,i)=>(v+b[i])/2),1]);
      objects.push({mesh:meshes.cylinder,matrix:m,color,metal:.95});
    }
    object("box",[0,-.05,0],[4.2,.34,3.55],[.24,.26,.3],.7);
    object("box",[0,.14,0],[4.16,.045,3.51],[.1,.12,.15],.55);
    object("box",[0,-.09,1.79],[4.15,.12,.028],[.53,.56,.61],.95);
    [[-1.6,-1.25],[1.6,-1.25],[-1.6,1.25],[1.6,1.25]].forEach(p=>object("cylinder",[p[0],-.32,p[1]],[.17,.26,.17],[.055,.06,.07],.25));
    object("cylinder",[-.25,.2,0],[1.56,.13,1.56],[.48,.5,.54],.96);
    const record=object("cylinder",[-.25,.31,0],[1.50,.08,1.50],[.03,.034,.043],.55);
    object("rim",[-.25,.30,0],[1,1,1],[.68,.71,.77],1);
    object("grooves",[-.25,.353,0],[1,1,1],[.13,.145,.17],.7);
    const label=object("cylinder",[-.25,.359,0],[.455,.012,.455],[.65,.79,.30],.25);
    const labelMarks=[0,1,2].map(i=>object("box",[-.25,.368,-.21+i*.07],[.24-i*.055,.008,.024],[.17,.22,.075],.15));
    object("cylinder",[-.25,.382,0],[.032,.085,.032],[.74,.77,.82],1);
    object("cylinder",[1.56,.26,-1.28],[.18,.24,.18],[.46,.49,.57],1);
    object("cylinder",[1.56,.47,-1.28],[.10,.22,.10],[.64,.67,.72],1);
    tube([1.56,.57,-1.28],[1.2,.57,-.85],.031,[.67,.71,.77]);
    tube([1.2,.57,-.85],[.64,.41,.2],.031,[.73,.76,.82]);
    object("box",[.59,.39,.32],[.11,.10,.29],[.76,.78,.83],1,-.35);
    object("box",[.56,.32,.44],[.075,.08,.09],[.09,.09,.1],.4,-.35);
    object("box",[-1.78,.18,1.33],[.30,.07,.24],[.72,.76,.82],.9);
    object("box",[-1.78,.23,1.33],[.23,.05,.17],[.62,.77,.27],.35);
    for(let i=0;i<7;i++) {
      const x=.38+i*.19,h=.06+Math.sin(i*1.6)*.035;
      object("box",[x,.19+h/2,1.42],[.09,h,.045],i%2?[.49,.36,.79]:[.66,.81,.30],.25);
    }
    canvas.dataset.renderer="webgl";canvas.dataset.vertices=String(vertexCount);wrap.classList.add("has-webgl");
    let visible=true,frame=0,last=0,angle=.25,pointer=[0,0],smoothed=[0,0],lost=false,frames=0,previousDraw=0;
    const compact=wrap.classList.contains("studio-deck-art");
    const hero=canvas.hasAttribute("data-listening-hero");
    function resize() {
      const rect=canvas.getBoundingClientRect(),dpr=Math.min(devicePixelRatio||1,1.5);
      const width=Math.max(1,Math.round(rect.width*dpr)),height=Math.max(1,Math.round(rect.height*dpr));
      if(canvas.width!==width||canvas.height!==height) {canvas.width=width;canvas.height=height;}
      gl.viewport(0,0,width,height);
    }
    function draw(now=0) {
      if(lost||!visible||document.hidden) return;
      resize();
      const spinning = playbackState === "playing" && (!audio || !audio.paused && !audio.ended) && motion();
      if(spinning && previousDraw) angle=(angle+Math.min(now-previousDraw,100)*.0024)%(Math.PI*2);
      previousDraw=now;
      canvas.dataset.recordAngle=angle.toFixed(4);
      canvas.dataset.recordSpinning=String(spinning);
      if(motion()) smoothed=smoothed.map((v,i)=>v+(pointer[i]-v)*.075);
      const aspect=canvas.width/canvas.height;
      const distance=hero?(aspect<1.2?5.5:4.6):compact&&aspect>=.8?4.6:(aspect<.8?7.3:6.8);
      const idleLift=hero&&playbackState==="idle"&&motion()?Math.sin(now*.00065)*.045:0;
      const eyeHeight=(hero?3.5:compact&&aspect>=.8?3.2:4.4)+idleLift;
      const eye=[Math.sin(.35+smoothed[0]*.16)*distance,eyeHeight+smoothed[1]*.45,Math.cos(.35+smoothed[0]*.16)*distance];
      const target=[0,.08,0];gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(program);
      const zoom=hero?Math.min(1.6,Math.max(1.08,aspect*.78)):1;
      gl.uniformMatrix4fv(u.ViewProjection,false,mul(perspective(aspect,zoom),view(eye,target)));gl.uniform3fv(u.Eye,eye);
      record.matrix=model([-.25,.31,0],[1.5,.08,1.5],angle);
      label.matrix=model([-.25,.359,0],[.455,.012,.455],angle);
      labelMarks.forEach((mark,i)=>{
        const z=-.21+i*.07;
        mark.matrix=model([-.25+Math.sin(angle)*z,.368,Math.cos(angle)*z],[.24-i*.055,.008,.024],angle);
      });
      // These are fixed deck buttons, not a simulated audio meter.
      for(const o of objects) {
        gl.bindBuffer(gl.ARRAY_BUFFER,o.mesh.buffer);gl.enableVertexAttribArray(attr.p);gl.enableVertexAttribArray(attr.n);
        gl.vertexAttribPointer(attr.p,3,gl.FLOAT,false,24,0);gl.vertexAttribPointer(attr.n,3,gl.FLOAT,false,24,12);
        gl.uniformMatrix4fv(u.Model,false,o.matrix);gl.uniformMatrix3fv(u.Normal,false,normals(o.matrix));
        gl.uniform3fv(u.Color,o.color);gl.uniform1f(u.Metal,o.metal);gl.drawArrays(gl.TRIANGLES,0,o.mesh.count);
      }
      canvas.dataset.frames=String(++frames);
    }
    function tick(now) {
      frame=0;
      if(!visible||!motion()||lost) {canvas.dataset.animating="false";return;}
      if(now-last>=1000/30) {draw(now);last=now;}
      frame=requestAnimationFrame(tick);
    }
    function refresh() {
      if(frame) cancelAnimationFrame(frame);frame=0;previousDraw=0;
      canvas.dataset.animating=String(visible&&motion()&&!lost);
      if(visible&&!document.hidden&&!lost) draw();
      if(visible&&motion()&&!lost) frame=requestAnimationFrame(tick);
    }
    const observer=new ResizeObserver(()=>{if(visible) {resize();if(!motion()) draw();}});observer.observe(wrap);
    const visibility=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;refresh();},{threshold:.05});visibility.observe(canvas);
    wrap.addEventListener("pointermove",event=>{
      if(!motion()||event.pointerType==="touch") return;
      const rect=wrap.getBoundingClientRect();pointer=[(event.clientX-rect.left)/rect.width-.5,(event.clientY-rect.top)/rect.height-.5];
    },{passive:true});
    wrap.addEventListener("pointerleave",()=>{pointer=[0,0];},{passive:true});
    canvas.addEventListener("webglcontextlost",event=>{event.preventDefault();lost=true;if(frame)cancelAnimationFrame(frame);frame=0;canvas.dataset.animating="false";canvas.dataset.renderer="css-fallback";wrap.classList.remove("has-webgl");});
    const scene={refresh};scenes.set(canvas,scene);refresh();
    canvas.dispatchEvent(new CustomEvent("riff:scene-ready",{bubbles:true,detail:{renderer:"webgl",vertices:vertexCount}}));
    return scene;
  }
  const lazy=new IntersectionObserver(entries=>{
    for(const entry of entries) if(entry.isIntersecting) {
      lazy.unobserve(entry.target);
      if("requestIdleCallback" in window) requestIdleCallback(()=>start(entry.target),{timeout:600});
      else setTimeout(()=>start(entry.target),0);
    }
  },{rootMargin:"80px",threshold:.01});
  function mount(root=document) {
    for (const panel of root.querySelectorAll("[data-riff-listening-panel]")) {
      if (panels.has(panel)) continue;
      panels.add(panel);
      panel.querySelector("[data-listening-action]").addEventListener("click", panelAction);
    }
    for (const canvas of root.querySelectorAll("[data-riff-scene]")) {
      if (canvases.has(canvas)) continue;
      canvases.add(canvas); lazy.observe(canvas);
    }
    if (toggle) toggle.hidden = !canvases.size && !records.length;
    syncListening(); syncMotion();
  }
  window.RiffScene = Object.freeze({mount});
  mount();
})();
