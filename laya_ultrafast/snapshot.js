(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const view = e => e.ownerDocument.defaultView;
  const safe = e => !['password','file','hidden'].includes(e.type);
  // A password field can be observed and typed into, but its value is never read.
  const secret = e => e.tagName==='INPUT' && e.type==='password';
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(e.ownerDocument.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  // Sites sometimes name an input by its value; its description, name, id and placeholder say what it is for.
  const hint = e => {
    if (!['INPUT','TEXTAREA','SELECT'].includes(e.tagName)) return '';
    const described=(e.getAttribute('aria-describedby')||'').split(/\s+/)
      .map(id=>e.ownerDocument.getElementById(id)?.textContent||'').join(' ');
    const parts=[described,e.name,e.id,e.placeholder].map(s=>(s||'')
      .replace(/([a-z])([A-Z])/g,'$1 $2').replace(/[-_]+/g,' ').replace(/\s+/g,' ').trim()).filter(Boolean);
    return [...new Set(parts)].join(' · ').slice(0,100);
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel','password'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  // A frame's content box in its parent's viewport: the iframe rect inside its border and padding.
  cache.box=f=>{
    const r=f.getBoundingClientRect(), s=view(f).getComputedStyle(f);
    const [l,t,rr,b]=['Left','Top','Right','Bottom'].map(side=>parseFloat(s['padding'+side])||0);
    return {x:r.x+f.clientLeft+l,y:r.y+f.clientTop+t,w:f.clientWidth-l-rr,h:f.clientHeight-t-b};
  };
  // Move a point from a frame's viewport into its parent's, hit-testing the iframe at each level.
  cache.hop=(f,x,y)=>{
    if (!f?.isConnected || !visible(f)) return null;
    const b=cache.box(f), w=view(f);
    if (x<0 || y<0 || x>=b.w || y>=b.h) return null;
    x+=b.x; y+=b.y;
    if (x<0 || y<0 || x>=w.innerWidth || y>=w.innerHeight || !f.contains(w.document.elementFromPoint(x,y))) return null;
    return cache.lift(w,x,y);
  };
  // Through every same-origin ancestor; a cross-origin parent is lifted by its own session.
  cache.lift=(w,x,y)=>{
    let parent=null;
    try { parent=w.frameElement; } catch { parent=null; }
    return parent ? cache.hop(parent,x,y) : {x,y};
  };
  const readable = f => {
    try { return f.contentDocument; } catch { return null; }
  };
  // Same-origin frames are read in this call, in this document's coordinates. Others are listed for their
  // own session. Invisible, zero-size and offscreen frames (trackers, beacons) are skipped.
  const scopes = () => {
    const found=[{doc:document,x:0,y:0,clip:[0,0,innerWidth,innerHeight]}], frames=[], described=[];
    for (let i=0; i<found.length; i++) {
      const {doc,x,y,clip}=found[i];
      for (const f of doc.querySelectorAll('iframe,frame')) {
        if (!visible(f)) continue;
        const b=cache.box(f), fx=x+b.x, fy=y+b.y;
        const c=[Math.max(clip[0],fx),Math.max(clip[1],fy),Math.min(clip[2],fx+b.w),Math.min(clip[3],fy+b.h)];
        // A container that hides overflow also hides the part of the frame outside it.
        for (let a=f.parentElement; a && a!==doc.documentElement; a=a.parentElement) {
          const s=view(a).getComputedStyle(a);
          if (s.overflowX==='visible' && s.overflowY==='visible') continue;
          const r=a.getBoundingClientRect(), l=x+r.x+a.clientLeft, t=y+r.y+a.clientTop;
          c[0]=Math.max(c[0],l); c[1]=Math.max(c[1],t);
          c[2]=Math.min(c[2],l+a.clientWidth); c[3]=Math.min(c[3],t+a.clientHeight);
        }
        if (c[0]>=c[2] || c[1]>=c[3]) continue;
        const inner=readable(f);
        described.push([identity(f),f.getAttribute('src'),inner?.URL??null,
          inner?.defaultView?.performance.timeOrigin??null,inner?.defaultView?.scrollX??null,
          inner?.defaultView?.scrollY??null]);
        if (inner?.body) found.push({doc:inner,x:fx,y:fy,clip:c});
        else if (!inner) frames.push({node:identity(f),rect:{x:fx,y:fy,w:b.w,h:b.h},clip:c});
      }
    }
    return {found,frames,described};
  };
  cache.pageKey=()=>{
    const {found,described}=scopes();
    return [performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
      found.flatMap(({doc})=>[...doc.querySelectorAll('input,textarea,select')]).filter(safe)
        .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly]),described];
  };
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),secret(e) ? null : e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  const {found,frames}=scopes();
  const actions=[];
  for (const {doc,x:dx,y:dy,clip} of found) {
    // The element's center must fall inside its frame's visible part of the top viewport.
    const shown=r=>{
      const x=dx+r.x+r.width/2, y=dy+r.y+r.height/2;
      return r.width>0 && r.height>0 && x>=clip[0] && y>=clip[1] && x<clip[2] && y<clip[3];
    };
    const at=r=>({x:dx+r.x,y:dy+r.y,w:r.width,h:r.height});
    for (const e of doc.querySelectorAll(selector)) {
      if ((!safe(e) && !secret(e)) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
      const r=e.getBoundingClientRect(), rname=role(e);
      if (!rname || !shown(r)) continue;
      if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
      const base={node:identity(e),role:rname,label:name(e)||rname,rect:at(r)};
      if (secret(e)) base.secret=true;
      const purpose=hint(e);
      if (purpose && purpose!==base.label) base.hint=purpose;
      for (const key of ['checked','selected','expanded']) {
        const value=e.getAttribute('aria-'+key);
        if (value!==null) base[key]=value;
      }
      if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
      if (e.tagName==='SELECT') {
        for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
          actions.push({...base,kind:'select',value:o.value,
            current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
      } else {
        const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
          (['textbox','searchbox','spinbutton'].includes(rname) ||
            (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
        const value=secret(e) ? '' : 'value' in e ? String(e.value) :
          e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
        actions.push({...base,kind:editable?'fill':'click',value});
        if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
      }
    }
    // Open pickers sometimes list plain clickable items with no role (a trip-type menu of <li>s).
    // Only inside dialogs, menus and listboxes: whole-page pointer scanning would flood the action list.
    const seen=new Set(actions.map(a=>cache.nodes.get(a.node)));
    const pickers='[role="dialog"],dialog[open],[aria-modal="true"],[role="menu"],[role="listbox"],[popover]';
    for (const root of doc.querySelectorAll(pickers)) {
      if (!visible(root)) continue;
      for (const e of root.querySelectorAll('*')) {
        if (seen.has(e) || e.closest(selector) || e.querySelector(selector) || !visible(e)) continue;
        const style=view(e).getComputedStyle;
        if (style(e).cursor!=='pointer' || style(e.parentElement).cursor==='pointer') continue;
        const text=e.innerText?.trim().replace(/\s+/g,' ');
        const r=e.getBoundingClientRect();
        if (!text || text.length>80 || !shown(r)) continue;
        seen.add(e);
        actions.push({node:identity(e),role:'option',label:text,rect:at(r),kind:'click',value:''});
      }
    }
  }
  const words=[]; let length=0;
  for (const {doc,x:dx,y:dy,clip} of found) {
    const walker=doc.createTreeWalker(doc.body,NodeFilter.SHOW_TEXT), range=doc.createRange(); let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      if (r.width>0 && r.height>0 && dy+r.bottom>clip[1] && dy+r.top<clip[3] &&
          dx+r.right>clip[0] && dx+r.left<clip[2]) {
        words.push(value); length+=value.length;
      }
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6],page_key[7]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height},actions,marker,page_key,guards,omitted_actions,frames};
})()
