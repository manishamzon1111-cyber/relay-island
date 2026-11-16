'use strict';
const H={
  el(tag,attrs={},...children){const n=document.createElement(tag);for(const [k,v] of Object.entries(attrs)){if(k==='class')n.className=v;else if(k==='text')n.textContent=v;else if(k==='onclick')n.addEventListener('click',v);else if(k==='onchange')n.addEventListener('change',v);else if(k==='value')n.value=v;else if(k==='disabled')n.disabled=!!v;else n.setAttribute(k,v)}for(const c of children.flat(Infinity)){if(c===null||c===undefined)continue;n.append(c instanceof Node?c:document.createTextNode(String(c)))}return n},
  async api(path,opts={}){const r=await fetch(path,{cache:'no-store',...opts});let data;try{data=await r.json()}catch{throw Error(`Server returned HTTP ${r.status}`)}if(!r.ok)throw Error(data.error?.message||`HTTP ${r.status}`);return data},
  post(path,data,admin=false){return this.api(path,{method:'POST',headers:{'Content-Type':'application/json',...(admin?{'X-Relay-Admin':'1'}:{})},body:JSON.stringify(data)})},
  status(s){return this.el('span',{class:'status '+s,text:s||'unknown'})},
  fmtDate(ts){return new Date(ts*1000).toLocaleString([], {dateStyle:'medium',timeStyle:'short'})},
  error(e){return this.el('div',{class:'banner error',text:e.message||String(e)})},
  button(label,action,cls='btn'){return this.el('button',{class:cls,onclick:action,type:'button',text:label})},
  empty(text){return this.el('div',{class:'empty',text})}
};
