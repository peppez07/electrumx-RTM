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
const {progressLabel, refreshDelay} = require('../../electrumx/explorer/static/app.js');
assert.equal(progressLabel({}), 'Unavailable');
assert.equal(progressLabel({verificationprogress:0}), '0.000%');
assert.equal(progressLabel({verificationprogress:0.327498}), '32.750%');
assert.equal(refreshDelay(0), 60000);
assert.equal(refreshDelay(4), 240000);
assert.equal(progressLabel({verificationprogress:'0.327498'}), '32.750%');

const {formatBytes} = require("../../electrumx/explorer/static/app.js");
assert.equal(formatBytes(1024), "1 KiB");
assert.equal(formatBytes(0), "0 B");
assert.equal(formatBytes(null), "Unavailable");

{
  const {indexProgress} = require('../../electrumx/explorer/static/app.js');
  const syncing = indexProgress({electrumx: {initial_sync: true, indexed_height: 231995, target_height: 1461952}});
  assert.equal(syncing.percent.toFixed(2), '15.87');
  assert.equal(syncing.label, '15.87% indexed · 231,995 of 1,461,952 blocks');
  assert.equal(indexProgress({electrumx: {ready: true, indexed_height: 5}}), null);
  assert.equal(indexProgress({}), null);
  console.log('UI index progress: passed');
}

{
  const {coreReady} = require('../../electrumx/explorer/static/app.js');
  assert.equal(coreReady({ready: false, core_ready: true}), true);
  assert.equal(coreReady({ready: true}), true);
  assert.equal(coreReady({ready: false, core_ready: false}), false);
  assert.equal(coreReady(null), false);
  console.log('UI core readiness: passed');
}
