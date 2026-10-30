'use strict';

function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function formatAtomic(value) {
  if (value === null || value === undefined) return 'Unavailable';
  const amount = BigInt(value), negative = amount < 0n, abs = negative ? -amount : amount;
  const whole = (abs / 100000000n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = (abs % 100000000n).toString().padStart(8, '0').replace(/0+$/, '');
  return (negative ? '-' : '') + whole + (fraction ? '.' + fraction : '');
}
async function loadAssetData(id, query, fetchAPI) {
  const data = await fetchAPI('assets/'+encodeURIComponent(id));
  let holders;
  try { holders = await fetchAPI('assets/'+encodeURIComponent(id)+'/holders?'+query); }
  catch (error) { holders = {items:[], next_offset:null, error:error.message}; }
  return {data,holders};
}
function progressLabel(core) {
  const raw = core.verificationprogress;
  const value = typeof raw==='number' || (typeof raw==='string' && raw.trim()!=='') ? Number(raw) : NaN;
  return Number.isFinite(value) ? (Math.max(0,Math.min(1,value))*100).toFixed(3)+'%' : 'Unavailable';
}
function refreshDelay(failures) { return Math.min(120000,30000 * 2**failures); }
if (typeof module !== 'undefined') module.exports = {escapeHTML, formatAtomic, loadAssetData, progressLabel, refreshDelay};
if (typeof document !== 'undefined') {
  const e = escapeHTML, enc = encodeURIComponent, $ = id => document.getElementById(id);
  let currentStatus = null, generation = 0, refreshing = false, pollTimer, failures = 0;
  const integer = value => value === undefined || value === null ? '—' : Number(value).toLocaleString('en-US');
  const shorten = value => value ? String(value).slice(0, 13) + '…' + String(value).slice(-7) : '—';
  const link = (kind, id, label) => `<a class="mono" href="#/${kind}/${enc(id)}">${e(label ?? shorten(id))}</a>`;
  const age = timestamp => {
    if (!timestamp) return '—';
    const seconds = Math.max(0, Math.floor(Date.now()/1000 - timestamp));
    return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds/60)}m ago` : seconds < 86400 ? `${Math.floor(seconds/3600)}h ago` : `${Math.floor(seconds/86400)}d ago`;
  };
  const table = (headers, rows, empty = 'No records found.') => `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th>${e(h)}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${headers.length}" class="empty">${e(empty)}</td></tr>`}</tbody></table></div>`;
  const row = (key, value) => `<div class="detail-row"><span class="detail-key">${e(key)}</span><div class="detail-value">${value}</div></div>`;
  const heading = (title, sub = '', extra = '') => `<div class="section-heading"><div><div class="eyebrow-orange">Mainnet explorer</div><h2>${e(title)}</h2>${sub ? `<p class="mono">${e(sub)}</p>` : ''}</div>${extra}</div>`;
  const badge = text => `<span class="badge">${e(text)}</span>`;
  const copy = value => `<button class="copy-button" data-copy="${e(value)}" aria-label="Copy ${e(value)}">Copy</button>`;
  const pager = (path, params, field, next) => `<div class="pager">${params.has(field) ? `<a class="button" href="#${path}">First page</a>` : ''}${next !== null && next !== undefined ? `<a class="button" href="#${path}?${e(new URLSearchParams({...Object.fromEntries(params), [field]:next}).toString())}">Next page →</a>` : ''}</div>`;

  async function api(path) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 47000);
    try {
      const response = await fetch('/api/v1/' + path, {signal:controller.signal});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error?.message || 'Data is temporarily unavailable.');
      return body;
    } finally { clearTimeout(timeout); }
  }

  function updateStatus(status) {
    currentStatus = status;
    const core = status.core || {}, index = status.electrumx || {};
    $('strip-status').textContent = status.ready ? `Block #${integer(core.blocks)} · Connected` : `${status.core_stale ? 'Last known' : status.phase==='core_unavailable' ? 'Core unavailable' : 'Synchronizing'} · ${integer(core.blocks)} blocks`;
    $('refresh-info').textContent = `${status.core_stale ? 'Last Core response' : 'Checked'} ${status.core_checked_at ? new Date(status.core_checked_at*1000).toLocaleTimeString() : '—'} · 30s refresh`;
    if (status.ready) { $('sync-banner').innerHTML = ''; return; }
    const progress = progressLabel(core), percent = progress==='Unavailable' ? null : parseFloat(progress);
    const label = {core_syncing:'Raptoreum Core is synchronizing', index_syncing:'The ElectrumX asset index is catching up', index_unavailable:'Waiting for the ElectrumX asset service', core_unavailable:'Waiting for Raptoreum Core', wrong_network:'Mainnet connection required'}[status.phase] || 'Connecting to the network';
    $('sync-banner').innerHTML = `<div class="banner"><div class="banner-symbol">◷</div><div><strong>${e(label)}</strong><p>Balances and transaction data become available after Core and the asset index are synchronized.</p>${percent===null ? '' : `<progress max="100" value="${percent}"></progress>`}<small>${e(progress)} verified${status.core_stale ? ' (last known; connection unavailable)' : ''} · ${integer(core.blocks)} blocks processed · ${integer(core.headers)} headers${index.indexed_height !== undefined ? ` · Index ${integer(index.indexed_height)}` : ''}</small></div></div>`;
  }

  function stats(status) {
    const core = status.core || {}, index = status.electrumx || {};
    return `<div class="card stats">${[
      ['Latest block', integer(core.blocks), status.ready ? 'Core synchronized' : 'Blocks processed so far'],
      ['Network headers', integer(core.headers), 'Raptoreum mainnet'],
      ['Asset index', integer(index.indexed_height), index.ready ? 'Indexed and ready' : 'Waiting for synchronization'],
      ['Network status', status.ready ? 'Connected' : status.phase==='core_unavailable' ? 'Unavailable' : 'Syncing', status.core_stale ? 'Last known Core data' : 'Progress updates automatically']
    ].map(([label,value,note]) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value">${value}</div><div class="stat-note">${note}</div></div>`).join('')}</div>`;
  }

  function syncView() {
    const core = currentStatus?.core || {};
    return stats(currentStatus || {}) + heading('Network synchronization', 'The explorer is installed and waiting for complete mainnet data.') + `<div class="card status-card"><div><h3>Raptoreum Core</h3><div class="status-line"><span>Network</span><strong>Mainnet</strong></div><div class="status-line"><span>Processed blocks</span><strong>${integer(core.blocks)}</strong></div><div class="status-line"><span>Received headers</span><strong>${integer(core.headers)}</strong></div><div class="status-line"><span>Verification progress</span><strong>${e(progressLabel(core))}${currentStatus?.core_stale ? ' (last known)' : ''}</strong></div></div><div><h3>Your explorer is ready to connect</h3><p class="muted">Explore blocks, transactions, addresses, assets and holders once synchronization finishes. This page refreshes automatically.</p><a class="button" href="#/api">View the API reference ↗</a></div></div>`;
  }

  async function overview() {
    const snapshot = await api('overview?limit=5');
    const blocks = {items:snapshot.blocks}, txs = {items:snapshot.transactions};
    const blockRows = blocks.items.map(b => `<div class="entry"><div class="entry-icon">Bk</div><div class="entry-body">${link('block',b.height,integer(b.height))}<small>${age(b.time)}</small></div><div class="entry-right"><span class="count-pill">${integer(b.tx_count)} transactions</span></div></div>`).join('');
    const txRows = txs.items.map(t => `<div class="entry"><div class="entry-icon">Tx</div><div class="entry-body">${link('tx',t.txid)}<small>${age(t.time)}</small></div><div class="entry-right">${t.coinbase ? 'Block reward' : 'Transaction'}<br>${integer(t.confirmations)} confirmations</div></div>`).join('');
    return stats(currentStatus) + `<div class="section-heading"><div><div class="eyebrow-orange">On the network</div><h2>Raptoreum at a glance</h2></div>${badge('As of '+new Date(snapshot.snapshot_at*1000).toLocaleTimeString())}</div><div class="two-column"><section class="card"><div class="card-head"><h3>Latest blocks</h3><a href="#/blocks">View all ↗</a></div>${blockRows}<div class="card-foot"><a href="#/blocks">View all blocks →</a></div></section><section class="card"><div class="card-head"><h3>Transactions in the latest block</h3><a href="#/transactions">View all</a></div>${txRows || '<div class="empty">No transactions in this block.</div>'}<div class="card-foot"><a href="#/block/${currentStatus.core.blocks}">Explore the latest block →</a></div></section></div>`;
  }

  async function blocksPage(params, path) {
    const result = await api('blocks?' + params);
    return heading('Blocks', 'The latest blocks on Raptoreum mainnet') + `<div class="card">${table(['Block','Age','Transactions','Size','Hash'], result.items.map(b => [link('block',b.height,integer(b.height)),e(age(b.time)),integer(b.tx_count),b.size ? integer(b.size)+' bytes' : '—',link('block',b.hash)]))}</div>` + pager(path,params,'start',result.next_start);
  }

  async function blockPage(id, params, path) {
    const b = await api('blocks/'+enc(id)+'?'+params);
    return heading('Block #'+integer(b.height), b.hash, badge(integer(b.confirmations)+' confirmations')) + `<div class="card details">${row('Block hash',e(b.hash)+' '+copy(b.hash))}${row('Timestamp',e(new Date(b.time*1000).toUTCString()))}${row('Transactions',integer(b.tx_count))}${row('Size',b.size ? integer(b.size)+' bytes' : '—')}${row('Difficulty',e(b.difficulty ?? '—'))}${row('Previous block',b.previousblockhash ? link('block',b.previousblockhash) : 'Genesis')}</div>` + heading('Transactions') + `<div class="card">${table(['Transaction hash'],b.tx.map(id=>[link('tx',id,id)]))}</div>` + pager(path,params,'offset',b.next_offset);
  }

  async function transactionPage(id) {
    const t = await api('transactions/'+enc(id));
    const inputs = (t.vin || []).map(v => [v.coinbase ? '<span class="badge">Block reward</span>' : link('tx',v.txid),v.coinbase ? 'Coinbase input' : e(v.vout),v.prevout ? e(formatAtomic(v.prevout.value_atomic))+' RTM' : '—']);
    const outputs = (t.vout || []).map(v => {
      const addresses = v.scriptPubKey?.addresses || (v.scriptPubKey?.address ? [v.scriptPubKey.address] : []);
      return [e(v.n),addresses.length ? addresses.map(a=>link('address',a,a)).join('<br>') : `<span class="muted">${e(v.scriptPubKey?.type || 'Script output')}</span>`,e(formatAtomic(v.value_atomic))+' RTM',v.asset ? link('asset',v.asset.asset_id)+`<br>${e(formatAtomic(v.asset.amount_atomic))}${v.asset.is_unique ? '<br>Unique IDs '+e(v.asset.unique_id)+'–'+e(v.asset.unique_end) : ''}` : '—'];
    });
    return heading('Transaction', t.txid, badge(t.confirmations ? integer(t.confirmations)+' confirmations' : 'Pending')) + `<div class="card details">${row('Transaction hash',e(t.txid)+' '+copy(t.txid))}${row('Block',t.blockhash ? link('block',t.blockhash) : 'Pending')}${row('Timestamp',t.time ? e(new Date(t.time*1000).toUTCString()) : 'Pending')}${row('Transaction type',t.coinbase ? 'Block reward' : e(t.type ?? 'Standard'))}${row('Fee',t.coinbase ? 'Block reward transaction' : t.fee_atomic !== null ? e(formatAtomic(t.fee_atomic))+' RTM' : 'Unavailable (input resolution limited to 32 inputs)')}${row('Size',t.size ? integer(t.size)+' bytes' : '—')}</div>` + heading('Inputs') + `<div class="card">${table(['Previous transaction','Output','Value'],inputs)}</div>` + heading('Outputs') + `<div class="card">${table(['Index','Recipient / script','RTM value','Asset'],outputs)}</div>`;
  }

  async function addressPage(id, params, path, assetId) {
    const a = await api('addresses/'+enc(id));
    const tab = params.get('tab') || (assetId ? 'history' : 'history');
    const tabs = assetId ? ['history','utxos'] : ['history','assets','utxos'];
    const query = new URLSearchParams(params); query.delete('tab');
    const endpoint = 'addresses/'+enc(id)+(assetId ? '/assets/'+enc(assetId) : '')+'/'+tab;
    if (!tabs.includes(tab)) throw new Error('Unknown address tab.');
    const data = await api(endpoint+'?'+query);
    let rows, headers;
    if (tab === 'assets') {
      headers = ['Asset','Confirmed balance','Pending change','Activity'];
      rows = data.items.map(item => [link('asset',item.asset_id),e(formatAtomic(item.confirmed)),e(formatAtomic(item.unconfirmed)),`<a href="#/address/${enc(id)}/asset/${enc(item.asset_id)}">Transfers &amp; outputs ↗</a>`]);
    } else if (tab === 'utxos') {
      headers = ['Transaction','Output','Height','Amount','Spendable'];
      rows = data.items.map(item=>[link('tx',item.tx_hash),e(item.tx_pos),integer(item.height),e(formatAtomic(assetId ? item.amount : item.value))+(assetId ? '' : ' RTM'),item.spendable === false ? 'No' : 'Yes']);
    } else {
      headers = ['Transaction','Height','Status'];
      rows = data.items.map(item=>[link('tx',item.tx_hash),integer(item.height),item.height > 0 ? badge('Confirmed') : '<span class="badge amber">Pending</span>']);
    }
    const links = tabs.map(name=>`<a ${name===tab ? 'class="active"' : ''} href="#${path}?tab=${name}">${name==='utxos' ? 'Unspent outputs' : name==='assets' ? 'Assets' : 'Transactions'}</a>`).join('');
    return heading('Address', id,copy(id)) + `<div class="card details">${row('Confirmed RTM balance',`<strong>${e(formatAtomic(a.balance.confirmed))} RTM</strong>`)}${row('Pending RTM change',e(formatAtomic(a.balance.unconfirmed))+' RTM')}${assetId ? row('Asset',link('asset',assetId,assetId)) : ''}</div>` + heading(assetId ? 'Asset activity' : 'Address activity') + `<div class="card"><div class="tabs">${links}</div>${table(headers,rows)}</div>` + pager(path,new URLSearchParams({...Object.fromEntries(params),tab}),data.cursor !== undefined ? 'cursor' : 'offset', data.cursor !== undefined ? data.cursor : data.next_offset) + (data.note ? `<p class="subtitle">${e(data.note)}</p>` : '');
  }

  function assetName(item) { return item.Asset_name || item.name || item.Asset_Name || 'Asset'; }
  function assetID(item) { return item.Asset_id || item.Asset_Id || item.asset_id; }
  async function assetsPage(params,path) {
    const data = await api('assets?'+params);
    return heading('Assets', 'Assets issued on Raptoreum mainnet') + `<div class="card">${table(['Asset','Asset ID','Type','Supply','Owner'],data.items.map(item=>[link('asset',assetID(item),assetName(item)),link('asset',assetID(item)),item.Isunique ? badge('Unique') : 'Fungible',e(item.Circulating_supply ?? '—'),item.owner ? link('address',item.owner) : '—']))}</div>` + pager(path,params,'offset',data.next_offset);
  }
  async function assetPage(id,params,path) {
    const data = await api('assets/'+enc(id)), tab=params.get('tab') || 'overview';
    const name=assetName(data), sub=name.includes('|'), root=name.split('|')[0];
    const tabs=`<div class="tabs">${['overview','holders','metadata'].map(t=>`<a class="${t===tab ? 'active' : ''}" href="#${path}?tab=${t}">${t[0].toUpperCase()+t.slice(1)}</a>`).join('')}</div>`;
    let body, pagination='';
    if(tab==='holders') {
      const holders=await api('assets/'+enc(id)+'/holders?'+params);
      body=table(['Address','Confirmed asset balance'],holders.items.map(item=>[link('address',item.address,item.address),e(formatAtomic(item.balance_atomic))]));
      pagination=pager(path,params,'offset',holders.next_offset);
    } else if(tab==='metadata') {
      body=`<div class="details">${Object.entries(data).map(([key,value])=>row(key,typeof value==='object' ? `<pre>${e(JSON.stringify(value,null,2))}</pre>` : e(value))).join('')}</div>`;
    } else if(tab==='overview') {
      body=`<div class="details">${row('Asset ID',e(id)+' '+copy(id))}${row('Qualified name',e(name))}${sub ? row('Parent asset',`<a href="#/search/${enc(root)}">${e(root)}</a>`) : ''}${row('Owner',data.owner ? link('address',data.owner,data.owner) : 'Unavailable')}${row('Circulating supply',e(data.Circulating_supply ?? 'Unavailable'))}${row('Decimal places',e(data.Decimalpoint ?? 'Unavailable'))}${row('Updatable',data.Updatable===undefined ? 'Unavailable' : data.Updatable ? 'Yes' : 'No')}${row('Reference',e(data.ReferenceHash ?? 'Unavailable'))}</div>`;
    } else throw new Error('Unknown asset tab.');
    return heading(name,id,badge(sub ? 'Subasset' : 'Root asset')+' '+badge(data.Isunique ? 'Unique' : 'Fungible'))+`<section class="card">${tabs}${body}</section>`+pagination;
  }

  async function transactionsPage(params,path) {
    const data=await api('transactions?'+params);
    params.set('block',data.block_hash || data.block);
    return heading('Transactions','From block #'+integer(data.block),link('block',data.block,'View block'))+`<div class="card">${table(['Transaction','Type','Confirmations','Size'],data.items.map(t=>[link('tx',t.txid,t.txid),t.coinbase ? 'Block reward' : e(t.type ?? 'Standard'),integer(t.confirmations),t.size ? integer(t.size)+' bytes' : '—']))}</div>`+pager(path,params,'offset',data.next_offset);
  }

  async function networkPage() {
    const data=await api('network');
    const sections=[['Mining & chain',data.mining],['Mempool',data.mempool],['Smartnodes',data.smartnodes]];
    return heading('Network','Public mainnet snapshot · '+new Date(data.snapshot_at*1000).toLocaleTimeString())+`<div class="network-grid">${sections.map(([title,values])=>`<section class="card"><div class="card-head"><h3>${e(title)}</h3></div><div class="details">${values ? Object.entries(values).map(([key,value])=>row(key,typeof value==='object' ? `<pre>${e(JSON.stringify(value,null,2))}</pre>` : e(value))).join('') : '<p class="muted">Data unavailable from Core.</p>'}</div></section>`).join('')}</div>`;
  }

  async function smartnodesPage(params,path) {
    const data=await api('smartnodes?'+params);
    return heading('Smartnodes',integer(data.total)+' nodes · snapshot '+new Date(data.snapshot_at*1000).toLocaleTimeString())+`<div class="card">${table(['Node / outpoint','Service address','Status','Payee','Last paid block'],data.items.map(n=>[e(n.proTxHash || n.id),e(n.address ?? '—'),badge(n.status ?? 'Unavailable'),n.payee ? link('address',n.payee) : '—',integer(n.lastpaidblock)]))}</div>`+pager(path,params,'offset',data.next_offset);
  }

  function apiPage() {
    const endpoints = [
      ['status','Synchronization, indexed tip and readiness. Available during sync.'],
      ['health','Process health without calling Core; does not indicate chain readiness.'],
      ['overview?limit=5','Shared public block/transaction snapshot, at most 10 entries.'],
      ['network','Mining, mempool and smartnode count; null if a metric is unavailable.'],
      ['smartnodes?limit=25&offset=0','Paginated public smartnode list, with snapshot time.'],
      ['search?q=QUERY','Find a block, transaction, mainnet address or asset name/ID.'],
      ['blocks?limit=10&start=HEIGHT','Recent blocks, descending height. next_start continues the page.'],
      ['blocks/HEIGHT_OR_HASH?limit=25&offset=0','Block details with paginated transaction IDs.'],
      ['transactions?block=HEIGHT&limit=25&offset=0','Transaction summaries from one block (latest by default).'],
      ['transactions/TXID','Decoded transaction, RTM outputs, asset outputs and fees when inputs can be resolved.'],
      ['addresses/ADDRESS','Confirmed and pending RTM balances in atomic decimal strings.'],
      ['addresses/ADDRESS/history?limit=25&offset=0','Coin transaction history. Subject to upstream ElectrumX limits.'],
      ['addresses/ADDRESS/utxos?limit=25&offset=0','Coin unspent outputs.'],
      ['addresses/ADDRESS/assets?limit=25','Asset balances. Use the returned cursor for continuation.'],
      ['addresses/ADDRESS/assets/ASSET_ID/history?limit=25','Per-address asset history with cursor pagination.'],
      ['addresses/ADDRESS/assets/ASSET_ID/utxos?limit=25','Asset outputs, unique ranges and spendability.'],
      ['assets?limit=25&offset=0','Core asset directory and metadata.'],
      ['assets/ASSET_ID','Core asset metadata.'],
      ['assets/ASSET_ID/holders?limit=25&offset=0','Confirmed holders and their atomic asset balances. Requires assetindex.'],
      ['assets/by-name/ROOT%7CCHILD','Find a root asset or qualified subasset name.'],
      ['wallet/capabilities','Mainnet readiness, supported asset types and HTTP relay activation.'],
      ['wallet/fees?blocks=6','Exact estimated and relay rates in RTM per kB; unavailable estimates are null.'],
      ['wallet/asset-policy','Current special transaction fee and root/subasset authorization rules.'],
      ['wallet/assets/ASSET_ID','Exact indexed metadata, Core display metadata and parent-root context.'],
      ['transactions/TXID/raw','Raw transaction bytes for local verification.'],
      ['transactions/TXID/proof?height=HEIGHT','Merkle proof for a confirmed transaction.']
    ];
    const legacy = [
      ['getblockcount','Plain height; ?json=true returns a JSON envelope.'],
      ['getblockchaininfo','Core synchronization state under the legacy blockcount key.'],
      ['getblock?height=HEIGHT','Verbose block; alternatively supply ?hash=HASH.'],
      ['getblockstats?height=HEIGHT','Block statistics with exact monetary strings.'],
      ['getaddressbalance/ADDRESS?json=true','Exact RTM and atomic balances; plain text without json=true.'],
      ['asset/ASSET_ID','Asset wrapper, name, display fields and Core metadata.'],
      ['gettransaction/TXID','Verbose transaction including raw hex. Also getrawtransaction?txid=TXID.'],
      ['getmininginfo','Core mining information.'], ['getmempoolinfo','Core mempool summary.'],
      ['getrawmempool','Mempool IDs under the reference getmempoolinfo wrapper.'],
      ['getsmartnodecount','Core count under the reference getsamrtnodecount wrapper.'],
      ['getsmartnodes','Core node list; no fabricated geolocation or ISP fields.'],
      ['project','Basic Raptoreum project identity.'],
      ['supply · marketcap · getrichlist','HTTP 501/not_indexed: these require separate statistics and market-data indexes.']
    ];
    return heading('Explorer API','RTM data and client-signed transaction relay · v1') + `<div class="card api-intro"><h3>Build on Raptoreum</h3><p class="muted">Base URL: <code>${e(location.origin)}/api/v1/</code>. Data routes use GET and return JSON. No API key is required. The default limit is 120 requests per minute per connecting IP; overloaded requests return 429 or 503 with Retry-After. Browser CORS/preflight is supported without cookies.</p><p class="muted">Coin and asset atomic quantities are decimal strings (100,000,000 units = 1 RTM or asset unit). Keep strings or use arbitrary-precision arithmetic. Core metadata quantities retain their source units; decimal JSON numbers are strings.</p><p class="muted">Data routes return 503 until Core and the asset index match. Status and capability routes remain available during sync. Invalid inputs return 400, missing data 404 and unavailable backends 502/503. Asset cursors expire on chain/mempool changes or a backend reconnection: restart the first page. Pages accept up to 100 items (25 for blocks and transaction summaries); offsets stop at 100,000. Coin history is limited by ElectrumX response limits.</p><code>GET /api/v1/status</code></div><div class="section-heading"><h2>Data endpoints</h2></div><div class="card">${table(['GET endpoint','Response'],endpoints.map(([path,description])=>[`<code>/api/v1/${e(path)}</code>`,e(description)]))}</div><div class="section-heading"><h2>Non-custodial wallet requests</h2></div><div class="card api-intro"><h3>Your wallet holds the keys and signs locally</h3><p class="muted">Explicit server opt-in is required for the POST routes. Check <code>GET /api/v1/wallet/capabilities</code> for activation and readiness. Root assets, subassets and unique assets use the same signed-transaction relay. The server never signs or unlocks a Core wallet. Use HTTPS/WSS with a trusted certificate for production wallet connections.</p><p><code>POST /api/v1/wallet/decode</code><br><code>POST /api/v1/wallet/broadcast</code></p><p class="muted">Body: <code>{&quot;hex&quot;:&quot;CLIENT_SIGNED_TRANSACTION_HEX&quot;}</code>. Only hex is accepted, bounded to 100,000 raw bytes. Never send a private key or seed. Decode checks syntax only; signatures, spendability and acceptance are not verified by decoding. Core 2.0.3.01 has no acceptance-preview RPC.</p><p class="muted">Core validates broadcasts with its normal fee and consensus protections. Accepted submissions return txid and accepted:true, which does not guarantee confirmation. A lost connection or timeout returns submission_unknown, accepted:null and the calculated txid: check that ID before submitting again. Rejections use HTTP 422; missing inputs/already-known transactions use 409. The server never automatically retries a broadcast.</p></div><div class="section-heading"><h2>Reference explorer aliases</h2></div><div class="card api-intro"><p class="muted">Familiar routes based on the <a href="https://explorer.raptoreum.com/api" target="_blank" rel="noopener noreferrer">Raptoreum explorer API</a>. Monetary amounts stay precise strings; compatibility does not promise identical numeric types. Large legacy responses fail explicitly above 4 MiB or 5,000 mempool/smartnode records.</p></div><div class="card">${table(['GET alias','Response / availability'],legacy.map(([path,description])=>[`<code>/api/${e(path)}</code>`,e(description)]))}</div>`;
  }

  async function render() {
    const turn = ++generation;
    const raw = location.hash.slice(1) || '/', [path, query=''] = raw.split('?');
    const parts = path.split('/').filter(Boolean).map(decodeURIComponent), params = new URLSearchParams(query);
    $('view').innerHTML = '<div class="loading card">Loading mainnet data…</div>';
    try {
      let html;
      if (parts[0] === 'api') html = apiPage();
      else if (!currentStatus?.ready) html = syncView();
      else if (!parts.length) html = await overview();
      else if (parts[0] === 'blocks') html = await blocksPage(params,path);
      else if (parts[0] === 'transactions') html = await transactionsPage(params,path);
      else if (parts[0] === 'network') html = await networkPage();
      else if (parts[0] === 'smartnodes') html = await smartnodesPage(params,path);
      else if (parts[0] === 'search' && parts[1]) {
        const result=await api('search?q='+enc(parts[1]));
        const match=result.matches[0];
        if(!match) throw new Error('No matching record was found.');
        location.hash='/'+(match.type==='transaction' ? 'tx' : match.type)+'/'+enc(match.id);
        return;
      }
      else if (parts[0] === 'block' && parts[1]) html = await blockPage(parts[1],params,path);
      else if (parts[0] === 'tx' && parts[1]) html = await transactionPage(parts[1]);
      else if (parts[0] === 'address' && parts[1]) html = await addressPage(parts[1],params,path,parts[2]==='asset' ? parts[3] : null);
      else if (parts[0] === 'assets') html = await assetsPage(params,path);
      else if (parts[0] === 'asset' && parts[1]) html = await assetPage(parts[1],params,path);
      else throw new Error('This explorer page does not exist.');
      if (turn === generation) {
        $('view').innerHTML = html;
        document.querySelectorAll('nav a').forEach(a=>{
          const active=(a.hash==='#/' && !parts.length) || (a.hash!=='#/' && a.hash==='#/'+parts[0]);
          a.classList.toggle('active',active);
          if(active) a.setAttribute('aria-current','page'); else a.removeAttribute('aria-current');
        });
      }
    } catch (error) {
      if (turn === generation) $('view').innerHTML = `<div class="card error-card"><h2>Data unavailable</h2><p>${e(error.message)}</p><a class="button" href="#/">Back to overview</a></div>`;
    }
  }

  async function refreshStatus(manual=false) {
    clearTimeout(pollTimer);
    if (refreshing || document.hidden) return;
    refreshing = true;
    const wasReady = currentStatus?.ready;
    try {
      updateStatus(await api('status'));
      failures=currentStatus.phase==='core_unavailable' ? failures+1 : 0;
      const dynamic=/^#\/(?:blocks|transactions|network|smartnodes)(?:\?|$)/.test(location.hash);
      if (manual || dynamic || wasReady !== currentStatus.ready || !currentStatus.ready || !location.hash || location.hash === '#/') await render();
    } catch (error) {
      failures++;
      updateStatus({...currentStatus,ready:false, phase:'core_unavailable',core_stale:!!currentStatus?.core});
      await render();
    } finally {
      refreshing = false;
      if(!document.hidden) pollTimer=setTimeout(refreshStatus,refreshDelay(failures));
    }
  }
  $('search').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    try {
      const result = await api('search?q='+enc($('query').value.trim()));
      if (!result.matches.length) throw new Error('No matching record was found.');
      const match = result.matches[0], kind = match.type==='transaction' ? 'tx' : match.type;
      const target='#/'+kind+'/'+enc(match.id);
      if(location.hash===target) await render(); else location.hash=target;
    } catch (error) {
      ++generation;
      $('view').innerHTML = `<div class="card error-card"><h2>Search unavailable</h2><p>${e(error.message)}</p><a class="button" href="#/">Back to overview</a></div>`;
    } finally { button.disabled = false; }
  });
  $('theme').addEventListener('click', () => {
    document.body.classList.toggle('dark');
    try { localStorage.setItem('rtm-theme',document.body.classList.contains('dark') ? 'dark' : 'light'); } catch {}
  });
  try { document.body.classList.toggle('dark',localStorage.getItem('rtm-theme')==='dark'); } catch {}
  window.addEventListener('hashchange',render);
  document.addEventListener('visibilitychange',()=>{
    clearTimeout(pollTimer);
    if(!document.hidden) refreshStatus();
  });
  $('refresh').addEventListener('click',()=>refreshStatus(true));
  $('view').addEventListener('click',async event=>{
    const button=event.target.closest('[data-copy]');
    if(!button) return;
    try {
      if(navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(button.dataset.copy);
      else {
        const input=document.createElement('textarea');input.value=button.dataset.copy;
        input.className='copy-input';document.body.append(input);input.select();
        const copied=document.execCommand('copy');input.remove();
        if(!copied) throw new Error('Copy unavailable');
      }
      button.textContent='Copied';
    } catch { button.textContent='Select text to copy'; }
  });
  refreshStatus();
}
