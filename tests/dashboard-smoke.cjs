// Exercise dashboard control flow without a browser or a live dialing service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {
    innerHTML: '', textContent: '', value: '', checked: false, disabled: false,
    classList: {add() {}, remove() {}, toggle() {}},
  });
  return nodes.get(id);
}
for (const [, id] of html.matchAll(/\bid="([\w-]+)"/g)) element('#' + id);
const sample = {
  '/api/integrations': {telephony: {provider:'livekit_sip', credentials_configured:true, outbound_enabled:true}, max_call_seconds:900, models:{llm:{model_available:true},stt:{available:true},tts:{available:true}}},
  '/api/phone-consents': [{id:1, phone:'+12025550123', contact_name:'Test contact', active:true}],
  '/api/agents': [{id:1, name:'Test agent', published_version:1, languages:['English']}],
  '/api/ivr-flows': [], '/api/calls': [],
  '/api/analytics/summary': {agents:1,ivr_flows:0,total_calls:0,completed:0},
};
let dialCount=0;
const context = vm.createContext({
  document: {querySelector: s => {assert(nodes.has(s), 'Unexpected static selector: '+s); return element(s);}, querySelectorAll: () => []},
  localStorage: {getItem:()=>null, setItem(){}, removeItem(){}},
  fetch:async (url, opts) => {
    if(url==='/api/calls/outbound') {dialCount++;return {ok:true,status:202,json:async()=>({id:5,provider:'livekit_sip',status:'queued'})};}
    assert(url in sample, 'Unexpected API call: '+url);
    return {ok:true,status:200,json:async()=>sample[url]};
  }, setTimeout(){}, setInterval(){}, console, FormData, URLSearchParams,
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
(async()=>{
  vm.runInContext("state.token='test';state.role='owner';state.agents=[{id:1,name:'Test agent',published_version:1}]",context);
  await vm.runInContext('loadCallSetup()', context);
  assert.equal(element('#place-call').disabled,false);
  assert.equal(element('#duration-label').textContent,'15 min limit');
  assert(element('#call-readiness').innerHTML.includes('Configuration complete'));
  element('#call-agent').value='1';element('#call-contact').value='1';
  await element('#call-form').onsubmit({preventDefault(){}});
  assert.equal(dialCount,1);
  assert(element('#call-result').textContent.includes('Call #5 queued'));
  sample['/api/integrations'].models.tts.available=false;
  await vm.runInContext('loadCallSetup()',context);
  assert.equal(element('#place-call').disabled,true);
  await element('#call-form').onsubmit({preventDefault(){}});
  assert.equal(dialCount,1,'Missing models must not initiate a call');
  console.log('Dashboard smoke checks passed: call readiness, queued response, unavailable model gating.');
})().catch(e=>{console.error(e);process.exitCode=1});
