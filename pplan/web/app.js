"use strict";
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const $ = id => document.getElementById(id);
let data, month, selected, childFilter = "all", loading = false;
const fmtMonth = new Intl.DateTimeFormat("en-US", {month:"long",year:"numeric",timeZone:"UTC"});
const fmtDay = new Intl.DateTimeFormat("en-US", {weekday:"long",month:"long",day:"numeric",year:"numeric",timeZone:"UTC"});
const utc = value => new Date(value + "T12:00:00Z");
const element = (tag, text, className) => {const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
function parentColor(id) {const i=Object.keys(data.parents).indexOf(id);return i<0?"var(--empty)":`var(--p${i})`;}
function swatch(id) {const s=element("span",undefined,"swatch");s.style.background=parentColor(id);return s;}
function name(id) {return data.parents[id] || "Unassigned";}
function timestamp(value) {return new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:data.timezone}).format(new Date(value));}
async function load(target) {
  if(loading)return;
  if(!tg?.initData){$("status").className="error";$("status").textContent="Open this calendar using the Calendar button in your private Telegram bot chat.";return;}
  loading=true;document.body.classList.add("loading");$("status").className="";$("status").textContent="Updating calendar…";
  try {
    const response=await fetch("/api/calendar",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"tma "+tg.initData},body:JSON.stringify(target?{month:target}:{})});
    const result=await response.json();if(!response.ok)throw new Error(result.error || "Calendar unavailable. Please try again.");
    data=result;month=result.month;
    if(!selected?.startsWith(month))selected=data.today.startsWith(month)?data.today:month+"-01";
    $("status").textContent=data.handover_confirmed?"":"Setup pending: handover times are examples until confirmed.";
    $("app").hidden=false;render();
  } catch(error) {$("status").className="error";$("status").textContent=error instanceof TypeError?"Cannot connect to your calendar. Please try Refresh in a moment.":error.message;}
  finally{loading=false;document.body.classList.remove("loading");}
}
function render() {
  $("timezone").textContent=data.timezone.replaceAll("_"," ");$("month-title").textContent=fmtMonth.format(utc(month+"-01"));$("month").value=month;
  $("legend").replaceChildren(...Object.entries(data.parents).map(([id,label])=>{const el=element("span");el.append(swatch(id),document.createTextNode(label));return el;}));
  $("filters").replaceChildren(...[["all","Both children"],...Object.entries(data.children)].map(([id,label])=>{const b=element("button",label);b.setAttribute("aria-pressed",String(id===childFilter));b.onclick=()=>{childFilter=id;render();};return b;}));
  const children=childFilter==="all"?Object.keys(data.children):[childFilter];
  $("grid").replaceChildren(...data.days.map(day=>{
    const b=element("button",undefined,"day"+(day.date.startsWith(month)?"":" outside")+(day.date===data.today?" current":"")+(day.events.length?" special":""));
    b.setAttribute("aria-pressed",String(day.date===selected));
    b.setAttribute("aria-label",fmtDay.format(utc(day.date))+". "+children.map(c=>data.children[c]+": "+name(day.before[c])+" before handover, "+name(day.after[c])+" after handover").join(". ")+(day.events.length?". "+day.events.map(e=>e.title).join(", "):""));
    b.append(element("span",String(Number(day.date.slice(-2))),"number"));
    const shared=children.every(c=>day.before[c]===day.before[children[0]]&&day.after[c]===day.after[children[0]]);
    for(const c of shared?[children[0]]:children){const band=element("span",undefined,"band");band.style.background=`linear-gradient(90deg,${parentColor(day.before[c])} 50%,${parentColor(day.after[c])} 50%)`;b.append(band);}
    b.onclick=()=>{selected=day.date;render();tg?.HapticFeedback?.selectionChanged();};return b;
  }));
  renderDetail();renderTotals();
}
function renderDetail() {
  const day=data.days.find(d=>d.date===selected);if(!day)return;
  const box=$("detail");box.replaceChildren(element("h2",fmtDay.format(utc(selected))),element("p","Handover at "+timestamp(day.handover)+" · "+data.timezone,"muted"));
  for(const [id,label] of Object.entries(data.children)) {
    if(childFilter!=="all"&&childFilter!==id)continue;
    const row=element("div",undefined,"child-detail");row.append(element("p",label,"child-name"));
    const before=day.before[id],after=day.after[id];
    if(before!==after){const line=element("div",undefined,"assignment");const text=element("div",name(before));text.append(element("small","Before "+timestamp(day.handover)));line.append(swatch(before),text);row.append(line);}
    const line=element("div",undefined,"assignment");const text=element("div",name(after));
    text.append(element("small",after?"From "+timestamp(day.handover)+" until "+timestamp(day.until):"This date is before the plan begins."));line.append(swatch(after),text);row.append(line);box.append(row);
  }
  for(const event of day.events)box.append(element("div",event.title,"event"));
}
function renderTotals() {
  const year=$("totals-period").value==="year",totals=year?data.year_totals:data.totals;
  const end=data.days.filter(d=>d.date.startsWith(month)).at(-1).date;
  $("totals-range").textContent=(year?month.slice(0,4)+"-01-01":month+"-01")+" through "+end+" · after-handover nights";
  $("totals").replaceChildren(...Object.entries(data.children).filter(([id])=>childFilter==="all"||childFilter===id).map(([id,label])=>{
    const row=element("div",undefined,"night-row");row.append(element("div",label,"child-name"));const sum=Object.values(totals[id]).reduce((a,b)=>a+b,0);
    const bar=element("div",undefined,"bar"),labels=element("div",undefined,"night-labels");
    for(const parent of Object.keys(data.parents)){const span=element("span");span.style.width=(sum?totals[id][parent]/sum*100:0)+"%";span.style.background=parentColor(parent);bar.append(span);labels.append(element("span",data.parents[parent]+" · "+totals[id][parent]));}
    row.append(bar,labels);return row;
  }));
}
function move(offset){if(!month)return;const d=utc(month+"-01");d.setUTCMonth(d.getUTCMonth()+offset);const target=d.toISOString().slice(0,7);if(target>="1901-01"&&target<="2099-12")load(target);}
$("previous").onclick=()=>move(-1);$("next").onclick=()=>move(1);$("refresh").onclick=()=>load(month);
$("month").onchange=e=>load(e.target.value);$("today").onclick=()=>{selected=data.today;load(data.today.slice(0,7));};$("totals-period").onchange=renderTotals;
load();
