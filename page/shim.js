/* Stand-in for claude.ai's page runtime: gives the ledger the same claude.use("db") API,
   backed by this server's REST endpoints and its server-sent event stream. */
(() => {
  const store = {positions:new Map(), loans:new Map(), settings:new Map(), plans:new Map()};
  const listeners = new Set();
  let ready = null;
  const notify = () => listeners.forEach(fn => { try { fn(); } catch (e) { console.error(e); } });
  const freeze = d => Object.freeze(JSON.parse(JSON.stringify(d)));

  async function load(){
    const r = await fetch('/api/data', {cache:'no-store'});
    if (!r.ok) throw new Error('load failed');
    const all = await r.json();
    for (const c of Object.keys(store)) { store[c] = new Map(Object.entries(all[c] || {}).map(([k,v]) => [k, freeze(v)])); }
    notify();
  }
  function connect(){
    const es = new EventSource('/api/events');
    es.onopen = () => { load().catch(() => {}); };           // resync after any reconnect
    es.onmessage = ev => { const m = JSON.parse(ev.data); if (!store[m.coll]) return;
      if (m.data === null) store[m.coll].delete(m.id); else store[m.coll].set(m.id, freeze(m.data)); notify(); };
  }
  async function send(method, coll, id, body){
    const r = await fetch(`/api/doc/${encodeURIComponent(coll)}/${encodeURIComponent(id)}`,
      {method, headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    if (!r.ok) throw {code: r.status === 400 || r.status === 404 ? 'invalid_argument' : 'unavailable', message: 'Server refused the write (' + r.status + ')'};
  }
  const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(10)), b => 'abcdefghijklmnopqrstuvwxyz0123456789'[b % 36]).join('');
  function docRef(coll, id){
    return { id, path: coll + '/' + id,
      async set(data){ store[coll].set(id, freeze(data)); notify(); await send('PUT', coll, id, data); },
      async update(data){ const cur = store[coll].get(id) || {}; store[coll].set(id, freeze({...cur, ...data})); notify(); await send('PATCH', coll, id, data); },
      async delete(){ store[coll].delete(id); notify(); await send('DELETE', coll, id); },
      onSnapshot(next){ const fn = () => { const d = store[coll].get(id); next({id, exists: !!d, data: () => d, metadata:{fromCache:false, hasPendingWrites:false}}); };
        listeners.add(fn); ready.then(fn); return () => listeners.delete(fn); } };
  }
  function collRef(coll){
    return { path: coll,
      doc: id => docRef(coll, id || newId()),
      onSnapshot(next){ const fn = () => { const docs = [...store[coll].entries()].map(([id,d]) => ({id, exists:true, data: () => d}));
          next({docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata:{fromCache:false, hasPendingWrites:false}}); };
        listeners.add(fn); ready.then(fn); return () => listeners.delete(fn); } };
  }
  const db = Object.freeze({
    doc(path){ const [c, id] = path.split('/'); if (!store[c] || !id) throw new TypeError('bad path ' + path); return docRef(c, id); },
    collection(path){ if (!store[path]) throw new TypeError('bad collection ' + path); return collRef(path); },
  });
  const user = Object.freeze({ can: async () => true, canEdit: async () => true, isOwner: async () => true });
  ready = load().then(connect).catch(() => { connect(); });
  window.claude = Object.freeze({ use: async name => name === 'db' ? db : name === 'user' ? user : null });
})();
