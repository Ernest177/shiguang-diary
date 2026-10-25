"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {value:"", dataset:{}, listeners:{}, checked:true, innerHTML:"", hidden:true,
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelectorAll:()=>[], classList:{add(){},remove(){}}});
  return nodes.get(selector);
}
const context = vm.createContext({console, Intl, Date, Set, Map, URLSearchParams, URL:{revokeObjectURL(){}},
  crypto:{randomUUID:()=>"synthetic-id"},
  localStorage:{getItem:()=>null,setItem(_key,value){context.savedDraft = JSON.parse(value);}},
  document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}}});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../web/media.js"),"utf8"),context);
const source = fs.readFileSync(path.join(__dirname,"../web/app.js"),"utf8");
vm.runInContext(source.slice(0,source.lastIndexOf("\ninit();")),context);
const run = text => vm.runInContext(text,context);
assert.equal(run('codepointOffset("甲🌿乙", 3)'),2);
assert.equal(run('shiftedMedia([{position:2}], "甲🌿乙", "开头甲🌿乙")[0].position'),4);
assert.equal(run('shiftedMedia([{position:2}], "甲🌿乙", "甲🌿新句乙")[0].position'),2,"typing at the anchor adds text after the photo");
assert.equal(run('shiftedMedia([{position:3}], "甲🌿乙", "甲")[0].position'),1);
context.attachments = [{id:"a".repeat(32),name:'<script>照片</script>.png',mime:"image/png",position:2},
  {id:"b".repeat(32),name:"视频.mp4",mime:"video/mp4",position:4}];
let html = run('mixedMediaHTML("甲🌿中间末尾", attachments)');
assert.ok(html.indexOf("甲🌿") < html.indexOf('<figure'));
assert.ok(html.indexOf("中间") > html.indexOf('</figure>'));
assert.ok(html.indexOf("末尾") > html.indexOf('</video>'));
assert.match(html,/&lt;script&gt;照片&lt;\/script&gt;/);
assert.ok(!html.includes("<script>"));
assert.match(html,/<video .*controls playsinline preload="metadata"/);
assert.match(html,/download=1/);
assert.match(run('mixedMediaHTML("",attachments.map(a=>({...a,position:0})))'),/<figure/);
assert.equal(run('mediaURL("../../settings")'),"");
assert.equal(run('mediaDisplayName({name:"密室合影_01.jpg",extension:".jpg"}, "2026-10-25")'),"密室合影_01_2026-10-25.jpg");
assert.equal(run('mediaDisplayName({name:"毕业_2020-07-01.jpg",extension:".jpg"}, "2026-10-25")'),"毕业_2020-07-01_2026-10-25.jpg");
assert.equal(run('mediaDisplayName({name:"合影_2026-10-25.jpg",extension:".jpg"}, "2026-10-25")'),"合影_2026-10-25.jpg");
assert.equal(run('mediaDisplayName({name:"CON.照片<原图>.jpg",extension:".jpg"}, "2026-10-25")'),"_CON.照片_原图__2026-10-25.jpg");
assert.equal(run('mediaDisplayName({name:"原名.jpg",extension:".jpg"}, "2026-02-30")'),"原名.jpg");
node("#entry-body").value = "甲🌿乙";
run('setComposerMedia([{...attachments[0],position:2,localURL:"blob:synthetic",file:{bytes:1}}]); draftWrite();');
assert.equal(context.savedDraft.attachments.length,1);
assert.ok(!("localURL" in context.savedDraft.attachments[0]));
assert.ok(!("file" in context.savedDraft.attachments[0]));
run('initMedia(); globalThis.pendingItem = composerMedia[0];');
node("#entry-body").value = "新增甲🌿乙";
node("#entry-body").listeners.input();
assert.equal(context.pendingItem.position,4);
assert.equal(run('composerMedia[0] === pendingItem'),true,"in-flight uploads retain the object used for their response");
assert.equal(context.savedDraft.attachments[0].position,4);
node("#entry-day").value = "2026-10-20";
node("#entry-day").listeners.change();
assert.match(node("#media-items").innerHTML,/照片.*_2026-10-20.png/);
node("#entry-day").value = "2026-10-23";
node("#entry-day").listeners.change();
assert.match(node("#media-items").innerHTML,/照片.*_2026-10-23.png/);
assert.ok(!node("#media-items").innerHTML.includes("_2026-10-20_2026-10-23"));
context.linked = {id:"c".repeat(32), name:"合影.png", extension:".png",mime:"image/png",position:0,entry_id:"entry-id",media_revision:2,journal_day:"2026-10-23"};
html = run('mediaFigure(linked)');
assert.match(html,/合影_2026-10-23.png/);
assert.match(html,/download=1&amp;entry=entry-id&amp;revision=2/);
run('mediaUploading = true; renderComposerMedia();');
assert.equal(node("#save-entry").disabled,true);
run('mediaUploading = false; state.saving = false; setComposerMedia();');
assert.equal(node("#media-tools").hidden,true);
assert.equal(node("#save-entry").disabled,false);
assert.match(source,/link.href = "\/api\/media\/archive"/,"large portable exports stream as native downloads");
assert.ok(fs.readFileSync(path.join(__dirname,"../web/index.html"),"utf8").includes('src="/media.js"'));
console.log("Media flow, Unicode anchors, draft privacy, escaping, upload edits and streamed export contracts passed.");
