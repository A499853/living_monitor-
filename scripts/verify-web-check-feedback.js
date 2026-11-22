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
assert.equal(context.roomCheckFeedback(snapshot),"检测中…");assert.equal(context.visibleStatus(snapshot),"live");
snapshot={...base,check_state:"recovering",monitor_recovering:true,checking:false};
assert.equal(context.visibleStatus(snapshot),"recovering","recovery replaces cached live status");assert.equal(context.statusLabel(snapshot),"恢复中…");assert.equal(context.groupOf(snapshot),"recovering","recovering room leaves live group");
assert.equal(context.roomCheckFeedback(snapshot),"等待自动重试");assert.match(context.friendlyRoomReason(snapshot),/最近确认结果：已开播/);
snapshot.checking=true;assert.equal(context.roomCheckFeedback(snapshot),"正在重新检测");assert.equal(context.visibleStatus(snapshot),"recovering");
snapshot={...base,check_state:"idle",checking:false,monitor_recovering:false,room_status:"waiting",last_confirmed_status:"waiting"};
assert.equal(context.visibleStatus(snapshot),"waiting");assert.equal(context.statusLabel(snapshot),"等待开播");assert.equal(context.roomCheckFeedback(snapshot),"","success has no completion badge or flash");assert.equal(context.groupOf(snapshot),"waiting");
assert.equal(context.roomCheckState({...base,check_state:"idle",checking:true,monitor_recovering:true}),"idle","explicit backend state wins over legacy flags");
assert.equal(context.roomCheckState({...base,is_checking:true}),"checking","older backend checking flag remains supported");
assert.equal(context.visibleStatus({...base,monitor_recovering:true}),"recovering","older backend recovery flag remains supported");
assert.equal(context.visibleStatus({...base,monitor_enabled:false,check_state:"recovering"}),"disabled");
assert.equal(context.roomCheckFeedback({...base,monitor_enabled:false,check_state:"recovering"}),"");
context.S.pendingChecks.set(base.id,{at:Date.now()});assert.equal(context.roomCheckFeedback({...base,check_state:"idle"}),"等待检测","queued manual checks must not pretend to run");
assert.equal(context.roomCheckFeedback({...base,check_state:"checking"}),"检测中…");
console.log("PASS: checking feedback, backend-only recovery, group movement, success without completion notice, disabled rooms, legacy flags, and queued checks");
