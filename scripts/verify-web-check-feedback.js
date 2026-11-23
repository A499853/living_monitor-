"use strict";
// Snapshot-driven UI status: never invent recovery from a browser timer.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const source=fs.readFileSync(path.join(__dirname,"../app/livewatch/web/app.js"),"utf8");
function section(start,end){const offset=source.indexOf(start);assert.ok(offset>=0,start);return source.slice(offset,source.indexOf(end,offset));}
const context={S:{pendingChecks:new Map()},Date,liveLabels:{live:"已开播",waiting:"等待开播",disabled:"未监控",recovering:"恢复中…",unknown:"状态未知"},dateTime:value=>value||"—"};
vm.createContext(context);
vm.runInContext(section("function roomCheckState(","function filtered("),context);
vm.runInContext(section("function groupOf(","function setText("),context);
vm.runInContext(section("function friendlyRoomReason(","function openRoomStatus("),context);
const base={id:"room",monitor_enabled:true,live_status:"live",room_status:"live",last_confirmed_status:"live",last_success_at:"2026-11-22T19:00:00+08:00"};
let snapshot={...base,check_state:"checking",checking:true,check_started_at:"2000-01-01T00:00:00Z"};
assert.equal(context.roomCheckState(snapshot),"checking","browser must not infer recovery from elapsed time");
assert.equal(context.roomCheckFeedback(snapshot),"检测中…");assert.equal(context.visibleStatus(snapshot),"live");assert.equal(context.roomCheckActive(snapshot),true);
snapshot={...base,check_state:"recovering",monitor_recovering:true,checking:false};
assert.equal(context.visibleStatus(snapshot),"recovering","recovery replaces cached live status");assert.equal(context.statusLabel(snapshot),"恢复中…");assert.equal(context.groupOf(snapshot),"recovering","recovering room leaves live group");
assert.equal(context.roomCheckFeedback(snapshot),"等待自动重试");assert.match(context.friendlyRoomReason(snapshot),/最近确认结果：已开播/);
assert.equal(context.roomCheckActive(snapshot),false,"recovery backoff must not show a checking spinner");
for(const count of [1,2,4,8,1]){snapshot.timeout_retry_yield_count=count;snapshot.timeout_retry_remaining=count;snapshot.next_check_seconds=999;assert.match(context.timeoutRecoveryNote(snapshot),new RegExp("本次先让其他房间检查 "+count+" 次，还剩 "+count+" 次"));assert.ok(!context.timeoutRecoveryNote(snapshot).includes("秒"),"yield opportunities must not become a time countdown");snapshot.timeout_retry_remaining=0;assert.match(context.timeoutRecoveryNote(snapshot),/正在重新排队/);assert.equal(context.roomCheckActive(snapshot),false);}
snapshot.checking=true;assert.equal(context.roomCheckFeedback(snapshot),"正在重新检测");assert.equal(context.visibleStatus(snapshot),"recovering");assert.equal(context.roomCheckActive(snapshot),true);assert.ok(!context.timeoutRecoveryNote(snapshot).includes("还剩"),"active retry must not show stale remaining opportunities");
snapshot={...base,check_state:"idle",checking:false,monitor_recovering:false,room_status:"waiting",last_confirmed_status:"waiting"};
assert.equal(context.visibleStatus(snapshot),"waiting");assert.equal(context.statusLabel(snapshot),"等待开播");assert.equal(context.roomCheckFeedback(snapshot),"","success has no completion badge or flash");assert.equal(context.groupOf(snapshot),"waiting");
snapshot={...base,check_state:"idle",checking:false,monitor_recovering:false,timeout_retry_yield_count:0,timeout_retry_remaining:0};assert.equal(context.visibleStatus(snapshot),"live");assert.equal(context.groupOf(snapshot),"live");assert.equal(context.roomCheckActive(snapshot),false);assert.equal(context.timeoutRecoveryNote(snapshot),"","successful live result clears timeout feedback");
assert.equal(context.roomCheckState({...base,check_state:"idle",checking:true,monitor_recovering:true}),"idle","explicit backend state wins over legacy flags");
assert.equal(context.roomCheckActive({...base,check_state:"recovering",checking:false,is_checking:true}),false,"explicit inactive checking flag wins over stale legacy flag");
assert.equal(context.roomCheckState({...base,is_checking:true}),"checking","older backend checking flag remains supported");
assert.equal(context.visibleStatus({...base,monitor_recovering:true}),"recovering","older backend recovery flag remains supported");
assert.equal(context.visibleStatus({...base,monitor_enabled:false,check_state:"recovering"}),"disabled");
assert.equal(context.roomCheckFeedback({...base,monitor_enabled:false,check_state:"recovering"}),"");
context.S.pendingChecks.set(base.id,{at:Date.now()});assert.equal(context.roomCheckFeedback({...base,check_state:"idle"}),"等待检测","queued manual checks must not pretend to run");
assert.equal(context.roomCheckFeedback({...base,check_state:"checking"}),"检测中…");
assert.ok(!source.includes("timeout_retry_cycles")&&!source.includes("倍正常检查周期")&&!source.includes("倍间隔"),"obsolete time-multiplier policy must not remain");
console.log("PASS: checking feedback, inactive backoff, yield-opportunity details, successful live restoration, backend-only recovery, group movement, disabled rooms, legacy flags, and queued checks");
