const assert = require('node:assert/strict');
const {formatAtomic, escapeHTML, loadAssetData} = require('../../electrumx/explorer/static/app.js');
assert.equal(formatAtomic('900719925474099312345'), '9,007,199,254,740.99312345');
assert.equal(formatAtomic('-1'), '-0.00000001');
assert.equal(formatAtomic('100000000'), '1');
assert.equal(formatAtomic(null), 'Unavailable');
assert.equal(escapeHTML('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
console.log('UI amount precision and HTML escaping: passed');
(async()=>{
  const result = await loadAssetData('id','',async path=>{
    if(path.includes('/holders')) throw new Error('Index unavailable');
    return {Asset_name:'Preserved metadata'};
  });
  assert.equal(result.data.Asset_name,'Preserved metadata');
  assert.equal(result.holders.error,'Index unavailable');
  console.log('Asset metadata remains available without holder index: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
