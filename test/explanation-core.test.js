const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../toxicity-core.js');
const originalFetch = global.fetch;
test.afterEach(() => { global.fetch = originalFetch; });
function packet() {
  return { contractVersion: 'aidd.research.explanation.api.v1', status: 'partial', structureId: 'a'.repeat(64),
    modelPrediction: { provenance:{modelVersion:'unit-fixture-only',weightsSha256:'b'.repeat(64)}, endpoints: ['herg','dili','ames','clintox'].map(id => ({id,score:.2})) },
    attribution: {status:'unavailable',atoms:[]}, evidence:{status:'insufficient-evidence',results:[]},
    report:{language:{status:'not-requested'},sections:[],claims:[],sources:[]},provenance:{researchOnly:true,humanRiskProbability:false} };
}
test('explanation uses separate same-origin API with actual weights identity and explicit optional language', async () => {
  let received; global.fetch = async (url, options) => { received={url,...options}; return {ok:true,json:async()=>packet()}; };
  await core.explainSmiles('CCO',{endpoint:'ames',expectedWeightsSha256:'b'.repeat(64),includeLanguage:false});
  const body=JSON.parse(received.body); assert.equal(received.url,'/api/research/explain'); assert.equal(body.model,'v2');
  assert.equal(body.endpoint,'ames'); assert.equal(body.expectedWeightsSha256,'b'.repeat(64)); assert.equal(body.includeLanguage,false);
  assert.ok(!('modelPrediction' in body));
});
test('explanation does not accept invalid score, duplicate endpoint or nonresearch claim', async () => {
  for (const mutate of [p=>p.modelPrediction.endpoints[0].score=true,p=>p.modelPrediction.endpoints[1].id='herg',p=>p.provenance.humanRiskProbability=true,
    p=>p.attribution.atoms=[{contribution:NaN}],p=>p.contractVersion='wrong',
    p=>delete p.report.language,p=>delete p.modelPrediction.provenance,p=>p.report.sections=[{title:'invalid fixture',text:'missing source fields'}]]) {
    const p=packet(); mutate(p); global.fetch=async()=>({ok:true,json:async()=>p});
    await assert.rejects(core.explainSmiles('CCO'),{code:'INVALID_EXPLANATION'});
  }
});
test('explanation abort reaches network without synthetic report fallback', async () => {
  const c=new AbortController(); global.fetch=(_,o)=>new Promise((r,j)=>o.signal.addEventListener('abort',()=>j(new DOMException('cancel','AbortError')),{once:true}));
  const pending=core.explainSmiles('CCO',{signal:c.signal}); c.abort(); await assert.rejects(pending,{name:'AbortError'});
});
