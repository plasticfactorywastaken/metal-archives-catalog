import test from 'node:test';
import assert from 'node:assert/strict';
import {matches, genres, needsClassification, isUpcoming, csvCell, csvRow, safeDataURL, historicalSnapshotNotice} from '../site/query.mjs';

const band = {entity_type:'band',entity_id:'100',name:'Test Band',genres_json:['Death Metal','Black Metal','Death Metal'],
  primary_genre:'Death Metal',country:'Finland',location:'Helsinki',status:'Active',formed_in:'1992',
  ecommerce_primary_class:'Extreme metal',classification_status:'mapped',last_changed_at:'2026-11-20T10:30:00Z'};
const release = {entity_type:'release',entity_id:'200',name:'Test Release',band_id:'100',band_name:'Test Band',label_name:'Test Label',
  release_year:2027,release_date:'2027-02-01',release_type:'Full-length',is_upcoming:true,genres_json:'["Death Metal","Black Metal"]'};

test('ordered genres retain duplicate values and JSON forms', () => {
  assert.deepEqual(genres(band), ['Death Metal','Black Metal','Death Metal']);
  assert.deepEqual(genres(release), ['Death Metal','Black Metal']);
  assert.equal(matches(band, {genre_position:'3',position_genre:'Death'}), true);
  assert.equal(matches(band, {genre_position:'2',position_genre:'Death'}), false);
  assert.equal(matches(band, {genre_position:'9'}), false);
});

test('all requested record filters compose without matching unknown fields', () => {
  assert.equal(matches(band, {entity_type:'band',name:'TEST',country:'fin',location:'hels',status:'active',formed_from:'1990',formed_to:'1995',primary_genre:'death',any_genre:'black',ecommerce_primary_class:'extreme'}), true);
  for (const filters of [{entity_type:'artist'},{country:'Sweden'},{formed_from:'2000'},{formed_to:'1990'},{release_from:'2020'},{artist:'Unknown'},{label:'Unknown'},{song:'Unknown'}]) assert.equal(matches(band, filters), false);
  assert.equal(matches(band, {band:'100'}), true);
  assert.equal(matches(release, {band:'test',release:'200',label:'label',release_from:'2027',release_to:'2027',release_type:'full',upcoming:'yes'}), true);
  assert.equal(matches(release, {upcoming:'no'}), false);
});

test('linked song and relationship filters use stable IDs and names', () => {
  assert.equal(matches({entity_type:'song',name:'A Song',entity_id:'22',release_id:'9',release_title:'An Album',band_name:'A Band'}, {song:'song',release:'9',band:'band'}), true);
  assert.equal(matches({entity_type:'relationship',source_entity_type:'artist',source_entity_id:'7',target_entity_type:'band',target_entity_id:'11'}, {artist:'7',band:'11'}), true);
  assert.equal(matches({entity_type:'relationship',source_entity_type:'artist',source_entity_id:'7',source_entity_name:'Test Artist',target_entity_type:'band',target_entity_id:'11',target_entity_name:'Test Band'}, {artist:'artist',band:'band'}), true);
  assert.equal(matches({entity_type:'release',related_entities:[{entity_type:'artist',entity_id:'7',name:'Guest Artist'}]}, {artist:'Guest'}), true);
});

test('release and song filters use inherited facets while keeping raw genres empty', () => {
  for (const entity_type of ['release','song']) {
    const record = {entity_type,entity_id:'20',genres_raw:'',genres_json:[],primary_genre:'',
      facet_genres_json:['Death','Black','Death'],facet_primary_genre:'Death',genre_origin:'band',
      facet_band_id:'1',facet_band_name:'Parent Band',facet_release_id:'2',facet_release_title:'Parent Release',
      facet_label_name:'Parent Label',facet_country:'Finland',facet_location:'Helsinki',facet_band_status:'Active',facet_formed_in:1992,
      facet_ecommerce_primary_class:'Extreme metal',facet_classification_status:'approved'};
    assert.equal(matches(record,{primary_genre:'Death',any_genre:'Black',genre_position:'3',position_genre:'Death',ecommerce_primary_class:'Extreme',band:'Parent',country:'Finland',status:'Active',formed_from:'1990'}),true);
    assert.equal(matches(record,{needs_classification:true}),false);
    assert.equal(matches(record,{query:'Extreme metal'}),true);
    assert.equal(matches(record,{primary_genre:'Doom'}),false);
    assert.deepEqual(record.genres_json,[]);
    assert.equal(record.genres_raw,'');
    assert.match(csvRow(record), /"Death","Death · Black · Death","band"/);
  }
  assert.equal(needsClassification({ecommerce_primary_class:'',facet_ecommerce_primary_class:'',facet_classification_status:'overridden'}),true);
  assert.deepEqual(genres({genres_json:['Doom'],facet_genres_json:['Death']}),['Doom']);
});

test('classification and changed-since filters stay conservative', () => {
  assert.equal(needsClassification(band), false);
  assert.equal(needsClassification(release), true);
  assert.equal(needsClassification({...band,classification_status:'needs_review'}), true);
  assert.equal(matches(band, {changed_since:'2026-11-01T00:00:00Z'}), true);
  assert.equal(matches(band, {changed_since:'2026-11-21T00:00:00Z'}), false);
  assert.equal(matches(release, {changed_since:'2026-11-01T00:00:00Z'}), false);
  assert.equal(matches(band, {needs_classification:true}), false);
  assert.equal(matches(band, {query:'Black Metal'}), true);
});

test('upcoming string false is not truthy', () => {
  assert.equal(isUpcoming({is_upcoming:'false'}), false);
  assert.equal(isUpcoming({is_upcoming:'true'}), true);
  assert.equal(isUpcoming({is_upcoming:0}), false);
});

test('historical notice distinguishes publication from observation and live verification', () => {
  const snapshot = {provider:'Kaggle',snapshot_label:'November 2024',published_at:'2024-11-09',observation_date:null,complete:false,dataset_url:'https://www.kaggle.com/datasets/example/test'};
  const pending = historicalSnapshotNotice(snapshot);
  assert.match(pending.title,/November 2024 snapshot/);
  assert.match(pending.title,/published 2024-11-09/);
  assert.match(historicalSnapshotNotice({...snapshot,published_at:'2024-11-09T13:34:11.417Z'}).title,/published 2024-11-09/);
  assert.match(pending.description,/observation date is unknown/);
  assert.match(pending.description,/unverified until individually checked/);
  assert.match(pending.progress,/import is in progress/);
  const finished = historicalSnapshotNotice({...snapshot,complete:true});
  assert.match(finished.progress,/does not establish live catalog completeness/);
  assert.equal(finished.description,pending.description);
  assert.equal(historicalSnapshotNotice(null),null);
  assert.equal(historicalSnapshotNotice({...snapshot,dataset_url:'javascript:alert(1)'}).sourceURL,'');
});

test('snapshot band filters match a joined label without source fabrication', () => {
  const record = {...band,label_id:'44',facet_label_id:'44',facet_label_name:'Synthetic Catalog Label',verification_status:'pending',last_checked:null};
  assert.equal(matches(record,{entity_type:'band',label:'Catalog',country:'Finland',status:'Active',any_genre:'Black'}),true);
  assert.equal(matches(record,{label:'44'}),true);
  assert.equal(record.last_checked,null);
  assert.equal(record.verification_status,'pending');
});

test('CSV export escapes quotes, multiline data, and spreadsheet formulas', () => {
  assert.equal(csvCell('=HYPERLINK("x")'), '"\'=HYPERLINK(""x"")"');
  assert.equal(csvCell(' \t+cmd'), '"\' \t+cmd"');
  assert.equal(csvCell('Name, "quoted"\nnext'), '"Name, ""quoted""\nnext"');
  assert.match(csvRow(band), /"band","100","Test Band"/);
});

test('data requests reject traversal and external URLs', () => {
  assert.equal(safeDataURL('band/00001.json','https://example.com/mirror/data/'),'https://example.com/mirror/data/band/00001.json');
  for (const path of ['../secret','https://other.test/data.json','javascript:alert(1)','/mirror/data-other/x']) assert.throws(() => safeDataURL(path,'https://example.com/mirror/data/'));
});

test('worker scans partitions sequentially, counts all matches, and retains only a page', async () => {
  const messages = [];
  let handler;
  const originalSelf = globalThis.self;
  const originalFetch = globalThis.fetch;
  globalThis.self = {addEventListener: (_event, callback) => { handler = callback; }, postMessage: value => messages.push(value)};
  const payloads = [
    [{...band,entity_id:'1'},{...band,entity_id:'2'}],
    [{...band,entity_id:'3'},{...band,entity_id:'4'}],
  ];
  let requests = 0, active = 0, maximumActive = 0;
  globalThis.fetch = async url => {
    requests++; active++; maximumActive = Math.max(maximumActive,active);
    await new Promise(resolve => setTimeout(resolve, 2));
    active--;
    const index = Number(new URL(url).pathname.match(/(\d)\.json$/)[1]);
    return {ok:true,json:async () => payloads[index]};
  };
  try {
    await import('../site/worker.js');
    await handler({data:{requestId:1,action:'search',manifest:{partitions:[{path:'0.json',entity_type:'band'},{path:'1.json',entity_type:'band'}]},filters:{},baseURL:'https://example.test/data/',page:2,pageSize:2}});
    const result = messages.find(message => message.kind === 'result');
    assert.deepEqual(result.rows.map(row => row.entity_id), ['3','4']);
    assert.equal(result.total,4);
    assert.equal(result.scanned,4);
    assert.equal(requests,2);
    assert.equal(maximumActive,1);
    messages.length = 0;
    globalThis.fetch = async url => url.includes('missing') ? {ok:false,status:404} : {ok:true,json:async () => payloads[0]};
    await handler({data:{requestId:2,action:'search',manifest:{partitions:[{path:'0.json'},{path:'missing.json'}]},filters:{},baseURL:'https://example.test/data/',page:1,pageSize:2}});
    const partial = messages.find(message => message.kind === 'result');
    assert.equal(partial.failures,1);
    assert.equal(partial.total,2);
    assert.equal(partial.rows.length,2);
  } finally {
    globalThis.self = originalSelf;
    globalThis.fetch = originalFetch;
  }
});

test('worker exposes a bounded first page before remaining partitions finish', async () => {
  const messages = [];
  let handler, resumeSecond, announceSecond;
  const originalSelf = globalThis.self, originalFetch = globalThis.fetch;
  const secondStarted = new Promise(resolve => { announceSecond = resolve; });
  const secondReleased = new Promise(resolve => { resumeSecond = resolve; });
  globalThis.self = {addEventListener: (_event, callback) => { handler = callback; }, postMessage: value => messages.push(value)};
  globalThis.fetch = async url => {
    if (url.endsWith('second.json')) { announceSecond(); await secondReleased; }
    return {ok:true,json:async () => Array.from({length:1000},(_,i) => ({...band,entity_id:String(i)}))};
  };
  try {
    await import('../site/worker.js?early-page');
    const running = handler({data:{requestId:11,action:'search',manifest:{partitions:[{path:'first.json'},{path:'second.json'}]},filters:{},baseURL:'https://example.test/data/',page:1,pageSize:25}});
    await secondStarted;
    const early = messages.find(message => message.kind === 'preview');
    assert.equal(early.rows.length,25);
    assert.equal(early.total,1000);
    assert.equal(messages.some(message => message.kind === 'result'),false);
    assert.equal(messages.filter(message => message.kind === 'progress')[0].action,'search');
    resumeSecond();
    await running;
    const final = messages.find(message => message.kind === 'result');
    assert.equal(final.rows.length,25);
    assert.equal(final.total,2000);
    assert.equal(messages.filter(message => message.kind === 'preview').length,1);
  } finally {
    resumeSecond();
    globalThis.self=originalSelf;globalThis.fetch=originalFetch;
  }
});

test('a new worker request aborts an older fetch without stale results or false failures', async () => {
  const messages=[];
  let handler, announceOld, oldAborted=false;
  const originalSelf=globalThis.self,originalFetch=globalThis.fetch;
  const oldStarted=new Promise(resolve => {announceOld=resolve;});
  globalThis.self={addEventListener:(_event,callback)=>{handler=callback;},postMessage:value=>messages.push(value)};
  globalThis.fetch=async (url,{signal})=>{
    if(url.endsWith('old.json')) return new Promise((_resolve,reject)=>{
      announceOld();
      signal.addEventListener('abort',()=>{oldAborted=true;reject(new Error('aborted'));},{once:true});
    });
    return {ok:true,json:async()=>[{...band,entity_id:'new'}]};
  };
  try {
    await import('../site/worker.js?cancellation');
    const oldRun=handler({data:{requestId:21,action:'search',manifest:{partitions:[{path:'old.json'}]},filters:{},baseURL:'https://example.test/data/'}});
    await oldStarted;
    const newRun=handler({data:{requestId:22,action:'search',manifest:{partitions:[{path:'new.json'}]},filters:{},baseURL:'https://example.test/data/'}});
    await Promise.all([oldRun,newRun]);
    assert.equal(oldAborted,true);
    assert.equal(messages.some(message=>message.requestId===21),false);
    const result=messages.find(message=>message.kind==='result');
    assert.equal(result.requestId,22);
    assert.equal(result.failures,0);
    assert.equal(result.rows[0].entity_id,'new');
  } finally {globalThis.self=originalSelf;globalThis.fetch=originalFetch;}
});
