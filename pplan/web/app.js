"use strict";
const tg=window.Telegram?.WebApp;tg?.ready();tg?.expand();
const $=id=>document.getElementById(id);
let data,selected,childFilter="all",visibleMonth,busy=false,generation=0,scrollPending=false;
const months=new Map();
const fmtMonth=new Intl.DateTimeFormat("en-US",{month:"long",year:"numeric",timeZone:"UTC"});
const fmtDay=new Intl.DateTimeFormat("en-US",{weekday:"long",month:"long",day:"numeric",year:"numeric",timeZone:"UTC"});
const utc=value=>new Date(value+"T12:00:00Z");
const element=(tag,text,className)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
function shiftMonth(month,offset){const d=utc(month+"-01");d.setUTCMonth(d.getUTCMonth()+offset);return d.toISOString().slice(0,7);}
function parentColor(id){const i=Object.keys(data.parents).indexOf(id);return i<0?"var(--empty)":`var(--p${i})`;}
function swatch(id){const s=element("span",undefined,"swatch");s.style.background=parentColor(id);return s;}
function name(id){return data.parents[id]||"Unassigned";}
function timestamp(value){return new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit",timeZone:data.timezone}).format(new Date(value));}
function updateNavigation(month){visibleMonth=month;$("month-title").textContent=fmtMonth.format(utc(month+"-01"));$("month").value=month;}
function error(message){$("status").className="error";$("status").textContent=message;}
async function fetchMonth(month){
  const response=await fetch("/api/calendar",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"tma "+tg.initData},body:JSON.stringify(month?{month}:{})});
  const result=await response.json();if(!response.ok)throw new Error(result.error||"Calendar unavailable. Please try again.");return result;
}
function renderFilters(){
  if(!data)return;
  $("timezone").textContent=data.timezone.replaceAll("_"," ")+" · View only";
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
    b.setAttribute("aria-pressed",String(day.date===selected));b.setAttribute("aria-label",fmtDay.format(utc(day.date))+". "+children.map(c=>value.children[c]+": "+name(day.before[c])+" before handover, "+name(day.after[c])+" after handover").join(". ")+(day.events.length?". "+day.events.map(e=>e.title).join(", "):""));
    b.append(element("span",String(Number(day.date.slice(-2))),"number"));
    const shared=children.every(c=>day.before[c]===day.before[children[0]]&&day.after[c]===day.after[children[0]]);
    for(const c of shared?[children[0]]:children){const band=element("span",undefined,"band");band.style.background=`linear-gradient(90deg,${parentColor(day.before[c])} 50%,${parentColor(day.after[c])} 50%)`;b.append(band);}
    b.onclick=()=>{data=value;selected=day.date;document.querySelectorAll(".day[aria-pressed=true]").forEach(el=>el.setAttribute("aria-pressed","false"));b.setAttribute("aria-pressed","true");renderDetail();renderTotals();$("day-dialog").showModal();tg?.HapticFeedback?.selectionChanged();};grid.append(b);
  }
  card.append(grid);return card;
}
async function load(month){
  if(!tg?.initData){error("Open this calendar using the Calendar button in your private Telegram bot chat.");return;}
  const version=++generation;busy=true;$("status").className="";$("status").textContent="Loading calendar…";$("retry").hidden=true;
  try{
    const value=await fetchMonth(month);if(version!==generation)return;
    data=value;months.clear();months.set(value.month,value);selected=undefined;$("months").replaceChildren(renderMonth(value));updateNavigation(value.month);renderFilters();$("app").hidden=false;
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
  for(const event of day.events)box.append(element("div",event.title,"event"));
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

$("previous").onclick=()=>{if(visibleMonth&&visibleMonth>"1901-01")load(shiftMonth(visibleMonth,-1));};
$("next").onclick=()=>{if(visibleMonth&&visibleMonth<"2099-12")load(shiftMonth(visibleMonth,1));};
$("month").onchange=e=>{if(e.target.value)load(e.target.value);};
$("menu-button").onclick=()=>$("menu").showModal();$("close-menu").onclick=()=>$("menu").close();$("close-day").onclick=()=>$("day-dialog").close();
$("refresh").onclick=()=>{$("menu").close();load(visibleMonth);};$("today").onclick=()=>{$("menu").close();load();};
$("retry").onclick=appendNext;$("totals-period").onchange=renderTotals;
load();
