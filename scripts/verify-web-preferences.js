"use strict";
// Offline checks for source choice and recording UI requests; no live service.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const source=fs.readFileSync(path.join(__dirname,"../app/livewatch/web/app.js"),"utf8");
const html=fs.readFileSync(path.join(__dirname,"../app/livewatch/web/index.html"),"utf8");
const timeoutInput=html.match(/<input\b[^>]*\bid="statusRequestTimeout"[^>]*>/)?.[0];assert.ok(timeoutInput);
assert.match(timeoutInput,/min="2"/);assert.match(timeoutInput,/max="60"/);assert.match(timeoutInput,/value="4"/);
assert.ok(html.includes("最多同时检查的房间数")&&html.includes("所有房间每秒最多启动检查数"));
for(const id of ["recordUntilOffline","recordNoSegments"]){const input=html.match(new RegExp('<input\\b[^>]*\\bid="'+id+'"[^>]*>'))?.[0];assert.ok(input,id);assert.match(input,/checked/);}
for(const id of ["recordDurationMinutes","recordSegmentMinutes"]){const input=html.match(new RegExp('<input\\b[^>]*\\bid="'+id+'"[^>]*>'))?.[0];assert.ok(input,id);assert.match(input,/min="1"/);assert.match(input,/max="1440"/);assert.match(input,/disabled/);}

function section(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
const calls=[],nodes=new Map(),rooms=[{id:"live",nickname:"直播中",live_status:"live",last_confirmed_status:"live",record_enabled:false},{id:"waiting",live_status:"waiting",last_confirmed_status:"waiting",record_enabled:false}];
function node(id){if(!nodes.has(id))nodes.set(id,{id,value:"",textContent:"",style:{},options:[],disabled:false,open:false,listeners:{},addEventListener(name,callback){this.listeners[name]=callback;},videoWidth:0,videoHeight:0,replaceChildren(){this.options=[];},append(child){this.options.push(child);},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}});return nodes.get(id);}
const context={console,Set,Map,Math,window:{innerWidth:1280,innerHeight:900},document:{querySelectorAll:()=>[]},S:{rooms,streams:[],settings:{recording_directory:"D:\\Archive"},rowBusy:new Set()},$:node,el:(_tag,_cls,text)=>({value:"",textContent:text}),roomById:id=>rooms.find(room=>room.id===id),visibleStatus:room=>room.live_status,setText:(target,text)=>target.textContent=text,error:()=>{},toast:()=>{},renderRooms:()=>{},refresh:async()=>{},api:async(...args)=>{calls.push(args);return {};},recordingPreference:()=>""};
vm.createContext(context);
vm.runInContext(section("function updateRecordingControls()","function settings()"),context);
vm.runInContext(section("const qualityChoices=","function streamOptions()"),context);
vm.runInContext(section("async function changeRoomSwitch(","async function checkRoom("),context);
vm.runInContext(section("function fitPlayer()","async function stopPlayback("),context);
const make=(quality,protocol="flv")=>({quality,protocol,url:quality+"."+protocol});
const choose=(streams,quality,protocol="flv")=>context.preferredStream(streams,quality,protocol);
const actual=[make("origin"),make("ultra"),make("high"),make("origin","hls"),make("blue","hls")];
assert.equal(choose(actual,"below_origin_1").quality,"ultra","skip qualities the platform does not provide");
assert.equal(choose(actual,"below_origin_2").quality,"high");
assert.equal(choose(actual,"blue").quality,"ultra","explicit missing quality falls to nearest lower grade");
assert.equal(choose([make("origin")],"below_origin_2").quality,"origin","one-quality streams remain usable");
assert.equal(choose([make("high","hls")],"below_origin_1").protocol,"hls","fallback when FLV is unavailable");
assert.equal(choose([],"below_origin_1"),null);
const player=node("playerDialog"),video=node("liveVideo");player.open=true;video.videoWidth=1080;video.videoHeight=1920;context.fitPlayer();
assert.ok(parseFloat(player.style.width)<450,"portrait player starts narrow");assert.equal(parseFloat(video.style.height),640);
video.videoWidth=1920;video.videoHeight=1080;context.fitPlayer();assert.ok(parseFloat(player.style.width)>900,"landscape follows source aspect");
context.window.innerWidth=375;context.window.innerHeight=667;context.fitPlayer();assert.ok(parseFloat(player.style.width)<=343);assert.ok(parseFloat(video.style.height)<=507);
(async()=>{
 await context.changeRoomSwitch("live","record_enabled");assert.equal(calls.length,0,"enabling asks for options before changing the room");assert.equal(node("recordOptionsDialog").open,true);assert.match(node("recordOptionsPolicy").textContent,/录到下播 · 不分段/);
 node("recordOptionsDialog").close();await node("recordOptionsForm").onsubmit({preventDefault(){}});assert.equal(calls.length,0,"cancelled dialog must not persist or enable recording");await context.changeRoomSwitch("live","record_enabled");
 node("recordQuality").value="high";node("recordProtocol").value="flv";node("recordDirectory").value=" E:\\Live recordings ";
 await node("recordOptionsForm").onsubmit({preventDefault(){}});
 assert.equal(calls.length,1);assert.equal(calls[0][0],"/api/rooms/live");assert.equal(calls[0][1],"PUT");assert.deepEqual(JSON.parse(JSON.stringify(calls[0][2])),{record_quality:"high",record_protocol:"flv",record_directory:"E:\\Live recordings",record_enabled:true,monitor_enabled:true});
 calls.length=0;context.openRecordingOptions({action:"record_live"});node("recordQuality").value="";node("recordProtocol").value="";node("recordDirectory").value="";
 await node("recordOptionsForm").onsubmit({preventDefault(){}});assert.equal(calls.length,2);assert.equal(calls[0][0],"/api/rooms/live");assert.equal(calls[1][0],"/api/bulk");assert.deepEqual(JSON.parse(JSON.stringify(calls[1][2])),{action:"record_live",ids:["live"]});
 let directoryCallback;context.window.chrome={webview:{addEventListener(_name,callback){directoryCallback=callback;}}};
 vm.runInContext(section('if(window.chrome&&window.chrome.webview)window.chrome.webview.addEventListener', 'document.addEventListener("visibilitychange"'),context);
 node("recordDirectory").value="keep";directoryCallback({data:{event:"recording-directory-selected",request_id:"recordDirectory",path:"",cancelled:true}});assert.equal(node("recordDirectory").value,"keep");
 directoryCallback({data:{event:"recording-directory-selected",request_id:"recordDirectory",path:"F:\\Video",cancelled:false}});assert.equal(node("recordDirectory").value,"F:\\Video");
 node("roomInput").value="room-identity";directoryCallback({data:{event:"recording-directory-selected",request_id:"roomInput",path:"wrong-target",cancelled:false}});assert.equal(node("roomInput").value,"room-identity","directory callback cannot write unrelated inputs");
 context.updateMonitorStrategyHint=()=>{};context.updateAccessModeHint=()=>{};context.updateDiskStatus=()=>{};
 vm.runInContext(section("function settings()","async function bulk("),context);
 rooms[0].monitor_enabled=false;const beforeRooms=JSON.stringify(rooms);
 for(const seconds of [20,60]){calls.length=0;context.settings();node("statusRequestTimeout").value=String(seconds);await node("settingsForm").onsubmit({preventDefault(){},submitter:{disabled:false}});assert.equal(calls.length,1);assert.equal(calls[0][0],"/api/settings");assert.equal(calls[0][1],"PUT");assert.equal(calls[0][2].status_request_timeout,seconds,"custom timeout must survive form submission");assert.equal(JSON.stringify(rooms),beforeRooms,"saving settings cannot enable disabled rooms");}
 calls.length=0;context.S.settings.record_segment_seconds=1800;context.settings();
 assert.equal(node("recordUntilOffline").checked,true);assert.equal(node("recordNoSegments").checked,true,"old half-hour setting must not enable segmentation");assert.equal(node("recordDurationMinutes").disabled,true);assert.equal(node("recordSegmentMinutes").disabled,true);
 await node("settingsForm").onsubmit({preventDefault(){},submitter:{disabled:false}});assert.equal(calls[0][2].record_duration_enabled,false);assert.equal(calls[0][2].record_segment_enabled,false);assert.equal(calls[0][2].record_segment_seconds,1800);
 context.settings();node("recordDurationEnabled").checked=true;node("recordSegmentEnabled").checked=true;node("recordDurationEnabled").listeners.change();assert.equal(node("recordDurationMinutes").disabled,false);assert.equal(node("recordSegmentMinutes").disabled,false);
 node("recordDurationMinutes").value="45";node("recordSegmentMinutes").value="15";calls.length=0;await node("settingsForm").onsubmit({preventDefault(){},submitter:{disabled:false}});
 assert.equal(calls[0][2].record_duration_enabled,true);assert.equal(calls[0][2].record_duration_seconds,2700,"duration minutes convert to seconds");assert.equal(calls[0][2].record_segment_enabled,true);assert.equal(calls[0][2].record_segment_seconds,900,"segment minutes convert to seconds");
 Object.assign(context.S.settings,calls[0][2]);context.settings();assert.equal(node("recordDurationEnabled").checked,true);assert.equal(node("recordSegmentEnabled").checked,true);assert.equal(node("recordDurationMinutes").value,45);assert.equal(node("recordSegmentMinutes").value,15);assert.match(context.recordingPolicySummary(),/指定时长 45 分钟.*每 15 分钟分段/);
 node("recordDurationEnabled").checked=false;node("recordSegmentEnabled").checked=false;node("recordUntilOffline").listeners.change();node("recordDurationMinutes").value="";node("recordSegmentMinutes").value="";calls.length=0;await node("settingsForm").onsubmit({preventDefault(){},submitter:{disabled:false}});
 assert.equal(node("recordDurationMinutes").disabled,true);assert.equal(node("recordSegmentMinutes").disabled,true);assert.equal(calls[0][2].record_duration_seconds,2700,"inactive empty inputs preserve saved values");assert.equal(calls[0][2].record_segment_seconds,900);assert.equal(calls[0][2].record_duration_enabled,false);assert.equal(calls[0][2].record_segment_enabled,false);assert.equal(JSON.stringify(rooms),beforeRooms);
 console.log("PASS: source defaults, quality fallback, adaptive player, recording requests, cancellation, directory isolation, custom timeouts, continuous recording defaults, optional duration/segments, minute conversion, disabled input preservation, and disabled-room preservation");
})().catch(error=>{console.error(error);process.exitCode=1;});
