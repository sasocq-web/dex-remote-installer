(() => {
  'use strict';
  const status=document.querySelector('#status'),screen=document.querySelector('#screen');
  const thread=new URLSearchParams(location.search).get('thread_id')||'';
  let pending=false,timer,landscape=false,down;
  const queue=[];
  const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/android-emulator/ws?thread_id=${encodeURIComponent(thread)}`);
  function send(value){if(socket.readyState!==WebSocket.OPEN)return false;if(pending){if(value.operation==='screenshot')return false;if(queue.length>=8){status.textContent='Aguarde as ações anteriores.';return false;}queue.push(value);return true;}clearTimeout(timer);pending=true;socket.send(JSON.stringify(value));return true;}
  function capture(){send({operation:'screenshot'});}
  socket.onopen=()=>{status.textContent='Android reservado para esta conversa';capture();};
  socket.onmessage=e=>{pending=false;try{const v=JSON.parse(e.data);if(v.image_url)screen.src=v.image_url;if(queue.length)send(queue.shift());else timer=setTimeout(capture,800);}catch{socket.close();}};
  socket.onclose=()=>{clearTimeout(timer);queue.length=0;status.textContent='Visor encerrado. Confira a tela ao reconectar antes de repetir a última ação.';document.querySelectorAll('button').forEach(b=>b.disabled=true);};
  socket.onerror=()=>socket.close();
  document.querySelectorAll('[data-op]').forEach(b=>b.onclick=()=>send({operation:b.dataset.op}));
  document.querySelector('#rotate').onclick=()=>{const next=!landscape;if(send({operation:'orientation',orientation:next?'landscape':'portrait'}))landscape=next;};
  document.querySelector('#send').onclick=()=>{const input=document.querySelector('#text');if(input.value&&send({operation:'type',text:input.value}))input.value='';};
  function point(e){const r=screen.getBoundingClientRect();return {x:Math.max(0,Math.min(screen.naturalWidth-1,Math.round((e.clientX-r.left)*screen.naturalWidth/r.width))),y:Math.max(0,Math.min(screen.naturalHeight-1,Math.round((e.clientY-r.top)*screen.naturalHeight/r.height)))};}
  screen.onpointerdown=e=>{if(!screen.naturalWidth)return;down={...point(e),time:performance.now()};screen.setPointerCapture(e.pointerId);};
  screen.onpointerup=e=>{if(!down)return;const p=point(e),start=down;down=null;const duration=Math.max(100,Math.min(5000,Math.round(performance.now()-start.time)));if(Math.hypot(p.x-start.x,p.y-start.y)>10)send({operation:'swipe',x:start.x,y:start.y,x2:p.x,y2:p.y,duration_ms:duration});else if(duration>500)send({operation:'long_press',x:p.x,y:p.y,duration_ms:duration});else send({operation:'tap',...p});};
  screen.onpointercancel=()=>{down=null;};
  addEventListener('pagehide',()=>socket.close());
})();
