"use strict";
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const $=id=>document.getElementById(id);
let data,selected,childFilter="all",visibleMonth,busy=false,generation=0,scrollPending=false;
const months=new Map(),eventEdits=new Map();
let editingEvent,events=[],savingEvent=false,editorLoad=0;
const fmtMonth=new Intl.DateTimeFormat("en-US",{month:"long",year:"numeric",timeZone:"UTC"});
const fmtDay=new Intl.DateTimeFormat("en-US",{weekday:"long",month:"long",day:"numeric",year:"numeric",timeZone:"UTC"});
const utc=value=>new Date(value+"T12:00:00Z");
const element=(tag,text,className)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
function shiftMonth(month,offset){const d=utc(month+"-01");d.setUTCMonth(d.getUTCMonth()+offset);return d.toISOString().slice(0,7);}
function parentColor(id){return data.parent_colors[id]||"var(--empty)";}
function swatch(id){const s=element("span",undefined,"swatch");s.style.background=parentColor(id);return s;}
function name(id){return data.parents[id]||"Unassigned";}
function timestamp(value){return new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:data.timezone}).format(new Date(value));}
function updateNavigation(month){visibleMonth=month;$("month-title").textContent=fmtMonth.format(utc(month+"-01"));$("month").value=month;}
function error(message){$("status").className="error";$("status").textContent=message;}
async function api(path,body={}){
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","Authorization":"tma "+tg.initData},body:JSON.stringify(body)});
  const result=await response.json();if(!response.ok)throw new Error(result.error||"Calendar unavailable. Please try again.");return result;
}
async function fetchMonth(month){
  const result=await api("/api/calendar",month?{month}:{});
  for(const event of eventEdits.values()){const newer=result.days.flatMap(day=>day.events).find(item=>item.id===event.id&&item.revision>event.revision);if(newer)eventEdits.delete(event.id);else applyEvent(result,event);}
  return result;
}
function renderFilters(){
  if(!data)return;
  $("timezone").textContent=data.timezone.replaceAll("_"," ");
  $("filters").replaceChildren(...[["all","Both children"],...Object.entries(data.children)].map(([id,label])=>{
    const b=element("button",label);b.setAttribute("aria-pressed",String(id===childFilter));b.onclick=()=>{
      childFilter=id;renderFilters();for(const [key,value] of months){const old=document.querySelector(`[data-month="${key}"]`);old.replaceWith(renderMonth(value));}$("menu").close();
    };return b;
  }));
}
function renderMonth(value){
  const card=element("section",undefined,"calendar-card");card.dataset.month=value.month;card.setAttribute("aria-label",fmtMonth.format(utc(value.month+"-01")));
  const heading=element("div",undefined,"calendar-heading");heading.append(element("h2",fmtMonth.format(utc(value.month+"-01"))));card.append(heading);
  const legend=element("div",undefined,"legend");for(const [id,label] of Object.entries(value.parents)){const el=element("span");el.append(swatch(id),document.createTextNode(label));legend.append(el);}card.append(legend);
  const weekdays=element("div",undefined,"weekdays");weekdays.setAttribute("aria-hidden","true");for(const label of ["M","T","W","T","F","S","S"])weekdays.append(element("span",label));card.append(weekdays);
  const grid=element("div",undefined,"grid"),children=childFilter==="all"?Object.keys(value.children):[childFilter];
  for(const day of value.days){
    const b=element("button",undefined,"day"+(day.date.startsWith(value.month)?"":" outside")+(day.date===value.today?" current":"")+(day.events.length?" special":""));
    b.setAttribute("aria-pressed",String(day.date===selected));b.setAttribute("aria-label",fmtDay.format(utc(day.date))+". "+children.map(c=>value.children[c]+": "+name(day.before[c])+" before handover, "+name(day.after[c])+" after handover").join(". ")+(day.events.length?". "+day.events.map(e=>e.title||"Special event").join(", "):""));
    b.append(element("span",String(Number(day.date.slice(-2))),"number"));
    if(day.events.length){const marker=element("span",undefined,"special-marker");marker.style.background="linear-gradient(90deg,"+day.events.map((e,i)=>`${e.color} ${i/day.events.length*100}% ${(i+1)/day.events.length*100}%`).join(",")+")";b.append(marker);}
    const shared=children.every(c=>day.before[c]===day.before[children[0]]&&day.after[c]===day.after[children[0]]);
    for(const c of shared?[children[0]]:children){const band=element("span",undefined,"band");band.style.background=`linear-gradient(90deg,${parentColor(day.before[c])} 50%,${parentColor(day.after[c])} 50%)`;b.append(band);}
    b.onclick=()=>{data=value;selected=day.date;document.querySelectorAll(".day[aria-pressed=true]").forEach(el=>el.setAttribute("aria-pressed","false"));b.setAttribute("aria-pressed","true");renderDetail();renderTotals();$("day-dialog").showModal();tg?.HapticFeedback?.selectionChanged();};grid.append(b);
  }
  card.append(grid);return card;
}
async function load(month){
  if(!tg?.initData){error("Open this calendar using the Calendar button in your private Telegram bot chat.");return;}
  eventEdits.clear();const version=++generation;busy=true;$("status").className="";$("status").textContent="Loading calendar…";$("retry").hidden=true;
  try{
    const value=await fetchMonth(month);if(version!==generation)return;
    data=value;renderPalette();months.clear();months.set(value.month,value);selected=undefined;$("months").replaceChildren(renderMonth(value));updateNavigation(value.month);renderFilters();$("app").hidden=false;
    $("status").textContent=value.handover_confirmed?"":"Setup pending: handover times are examples until confirmed.";window.scrollTo({top:0});
  }catch(exc){if(version===generation)error(exc instanceof TypeError?"Cannot connect to your calendar. Please try Refresh in the menu.":exc.message);}
  finally{if(version===generation){busy=false;observer.unobserve($("load-more"));observer.observe($("load-more"));}}
}
async function appendNext(){
  if(busy||!months.size)return;
  const target=shiftMonth([...months.keys()].at(-1),1);if(target>"2099-12"){$("load-more").textContent="End of supported calendar";return;}
  const version=generation;busy=true;$("load-more").textContent="Loading next month…";$("retry").hidden=true;
  try{const value=await fetchMonth(target);if(version!==generation)return;months.set(value.month,value);$("months").append(renderMonth(value));$("load-more").textContent="Scroll down for the next month";}
  catch(exc){if(version===generation){$("load-more").textContent=exc instanceof TypeError?"Cannot load the next month. Please try again.":exc.message;$("retry").hidden=false;}}
  finally{if(version===generation){busy=false;if($("retry").hidden){observer.unobserve($("load-more"));observer.observe($("load-more"));}}}
}
const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)&&$("retry").hidden)appendNext();},{rootMargin:"150px"});
window.addEventListener("scroll",()=>{if(scrollPending)return;scrollPending=true;requestAnimationFrame(()=>{scrollPending=false;const cards=[...document.querySelectorAll(".calendar-card")];let current=cards[0];for(const card of cards){if(card.getBoundingClientRect().top<=110)current=card;else break;}if(current)updateNavigation(current.dataset.month);});},{passive:true});
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
  for(const event of day.events)box.append(eventCard(event));
}
function renderTotals() {
  const month=data.month;
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

function eventCard(event){
  const card=element("div",undefined,"event-card");card.style.borderLeftColor=event.color;
  const heading=element("div",undefined,"event-card-heading");heading.append(element("strong",event.title||"Special event"));
  const edit=element("button","Edit","text-button");edit.setAttribute("aria-label","Edit "+(event.title||"special event"));edit.onclick=()=>openEventEditor(event.id);heading.append(edit);card.append(heading);
  card.append(element("p",event.start+" through "+event.end,"muted"));
  if(event.description)card.append(element("p",event.description,"event-description"));return card;
}
function renderEventList(){
  $("specials-list").replaceChildren(...events.map(eventCard));$("specials-status").textContent=events.length?"":"No special events yet. Add one with /special in the bot.";
}
async function showSpecials(){
  $("menu").close();$("specials-dialog").showModal();$("specials-list").replaceChildren();$("specials-status").textContent="Loading events…";
  try{events=(await api("/api/specials/list")).events;renderEventList();}
  catch(exc){$("specials-status").textContent=exc instanceof TypeError?"Cannot load events. Close and try again.":exc.message;}
}
async function openEventEditor(id){
  if(savingEvent)return;
  const request=++editorLoad;editingEvent=null;$("event-form").reset();$("event-error").textContent="Loading event…";$("event-fields").disabled=true;$("save-event").disabled=true;$("event-editor").showModal();
  try{
    const fresh=(await api("/api/specials/list")).events.find(e=>e.id===id);
    if(!$("event-editor").open||request!==editorLoad)return;
    if(!fresh)throw new Error("This event is no longer available.");
    editingEvent=fresh;$("event-title").value=fresh.title;$("event-start").value=fresh.start;$("event-end").value=fresh.end;$("event-description").value=fresh.description;$("event-color").value=fresh.color;
    $("event-error").textContent="";$("event-fields").disabled=false;$("save-event").disabled=false;updateEventPreview();
  }catch(exc){if($("event-editor").open&&request===editorLoad)$("event-error").textContent=exc instanceof TypeError?"Cannot load this event. Close and try again.":exc.message;}
}
function updateEventPreview(){
  const color=$("event-color").value;$("event-preview").style.borderColor=color;$("event-preview").textContent=$("event-title").value.trim()||"Special event";
  for(const b of $("event-palette").children)b.setAttribute("aria-pressed",String(b.dataset.color===color));
}
function applyEvent(value,event){
  for(const day of value.days){day.events=day.events.filter(e=>e.id!==event.id);if(event.start<=day.date&&day.date<=event.end)day.events.push(event);day.events.sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end)||a.id-b.id);}
}
async function saveEvent(e){
  e.preventDefault();if(!editingEvent||savingEvent)return;
  const payload={id:editingEvent.id,revision:editingEvent.revision,title:$("event-title").value,start:$("event-start").value,end:$("event-end").value,description:$("event-description").value,color:$("event-color").value};
  if(payload.end<payload.start){$("event-error").textContent="End date must be on or after start date.";return;}
  savingEvent=true;$("event-fields").disabled=true;$("save-event").disabled=true;$("close-editor").disabled=true;$("cancel-editor").disabled=true;$("save-event").textContent="Saving…";$("event-error").textContent="";
  try{
    const event=(await api("/api/specials/update",payload)).event;eventEdits.set(event.id,event);
    for(const [key,value] of months){applyEvent(value,event);document.querySelector(`[data-month="${key}"]`).replaceWith(renderMonth(value));}
    if(data&&!months.has(data.month))applyEvent(data,event);
    events=events.map(item=>item.id===event.id?event:item).sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end)||a.id-b.id);
    if($("specials-dialog").open)renderEventList();if($("day-dialog").open)renderDetail();$("event-editor").close();tg?.HapticFeedback?.notificationOccurred("success");
  }catch(exc){$("event-error").textContent=exc instanceof TypeError?"Connection lost. Try saving again; duplicate saves are safe.":exc.message;}
  finally{savingEvent=false;$("event-fields").disabled=false;$("save-event").disabled=false;$("close-editor").disabled=false;$("cancel-editor").disabled=false;$("save-event").textContent="Save changes";}
}
function renderPalette(){
  $("event-color").replaceChildren(...data.palette.map(item=>{const option=element("option",item.name);option.value=item.color;return option;}));
  $("event-palette").replaceChildren(...data.palette.map(item=>{
    const b=element("button");b.type="button";b.dataset.color=item.color;b.style.background=item.color;b.setAttribute("aria-label",item.name);b.title=item.name;
    b.onclick=()=>{$("event-color").value=item.color;updateEventPreview();};return b;
  }));
}
$("specials-button").onclick=showSpecials;$("close-specials").onclick=()=>$("specials-dialog").close();
$("close-editor").onclick=$("cancel-editor").onclick=()=>{if(!savingEvent)$("event-editor").close();};
$("event-editor").addEventListener("cancel",e=>{if(savingEvent)e.preventDefault();});
$("event-form").onsubmit=saveEvent;$("event-title").oninput=$("event-color").oninput=updateEventPreview;

$("previous").onclick=()=>{if(visibleMonth&&visibleMonth>"1901-01")load(shiftMonth(visibleMonth,-1));};
$("next").onclick=()=>{if(visibleMonth&&visibleMonth<"2099-12")load(shiftMonth(visibleMonth,1));};
$("month").onchange=e=>{if(e.target.value)load(e.target.value);};
$("menu-button").onclick=()=>$("menu").showModal();$("close-menu").onclick=()=>$("menu").close();$("close-day").onclick=()=>$("day-dialog").close();
$("refresh").onclick=()=>{$("menu").close();load(visibleMonth);};$("today").onclick=()=>{$("menu").close();load();};
$("retry").onclick=appendNext;$("totals-period").onchange=renderTotals;
load();
