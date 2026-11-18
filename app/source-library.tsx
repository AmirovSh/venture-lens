'use client';

import {useMemo,useState} from 'react';
import {BookOpen,ExternalLink,FileText,Filter,X} from 'lucide-react';

export type Source={id:string;url:string;title:string;text?:string;origin?:string;retrieved:number;sha256:string};
type SourceModel={company:string;data:Record<string,any>};
const sourceType=(source:Source)=>source.origin==='search_snippet'?'snippet':'page';
const retrievedDate=(value:number)=>new Date(value*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'short'});
function hostname(url:string){try{return new URL(url).hostname;}catch{return 'Источник';}}

export default function SourceLibrary({sources,models,title}:{sources:Source[];models:SourceModel[];title?:string}){
 const [company,setCompany]=useState(''),[kind,setKind]=useState('');
 const associations=useMemo(()=>{
  const result=new Map<string,Set<string>>();
  for(const model of models){
   const used=[model.data.discovery_source,{id:model.data.candidate?.discovery_source_id},...(model.data.sources||[]),...(model.data.verification_sources||[])];
   for(const source of used){
    if(!source?.id)continue;
    if(!result.has(source.id))result.set(source.id,new Set());
    result.get(source.id)!.add(model.company);
   }
  }
  return result;
 },[models]);
 const companies=useMemo(()=>Array.from(new Set(models.map(model=>model.company))).sort((a,b)=>a.localeCompare(b)),[models]);
 const filtered=sources.filter(source=>(!company||associations.get(source.id)?.has(company))&&(!kind||sourceType(source)===kind));
 const active=Boolean(company||kind);
 const reset=()=>{setCompany('');setKind('');};
 return <section className="results-panel" aria-label="Библиотека источников">
  <div className="section-title"><div><h2>Библиотека доказательств <span>{sources.length}</span></h2><p>{title||'Сначала запустите исследование'}</p></div></div>
  {!sources.length?<div className="empty-state"><BookOpen size={30}/><h3>Источники появятся во время исследования</h3><p>Каждая страница сохраняется с датой получения и контрольной суммой.</p></div>:<>
   <div className="source-filters" role="group" aria-label="Фильтры источников">
    <label>Компания-аналог<select value={company} onChange={event=>setCompany(event.target.value)}><option value="">Все компании</option>{companies.map(name=><option key={name} value={name}>{name}</option>)}</select></label>
    <label>Тип источника<select value={kind} onChange={event=>setKind(event.target.value)}><option value="">Все типы</option><option value="page">Текст страницы</option><option value="snippet">Поисковый фрагмент</option></select></label>
    <button className="button secondary compact" disabled={!active} onClick={reset}><X size={14}/>Сбросить фильтры</button>
    <div className="source-filter-summary" role="status" aria-live="polite"><Filter size={14}/><span>Показано <strong>{filtered.length}</strong> из {sources.length} источников</span></div>
    <p className="source-filter-help">Фильтр компании показывает материалы её карточки и использованные общие источники поиска.</p>
   </div>
   {!filtered.length?<div className="empty-state"><Filter size={28}/><h3>Нет источников по выбранным фильтрам</h3><p>Выберите другую компанию или тип источника либо сбросьте фильтры.</p><button className="button secondary compact" onClick={reset}>Показать все источники</button></div>:<div className="source-grid">{filtered.map(source=>{
    const linked=Array.from(associations.get(source.id)||[]).sort((a,b)=>a.localeCompare(b));
    return <article className="source-card" key={source.id}><span className="source-domain"><FileText size={15}/>{hostname(source.url)}</span><h3><a href={/^https?:\/\//i.test(source.url)?source.url:'#'} target="_blank" rel="noreferrer">{source.title}<ExternalLink size={13}/></a></h3><span className="source-companies" title={linked.join(', ')}>{linked.length>2?`Используется для ${linked.length} компаний`:linked.length?linked.join(' · '):'Общий поиск'}</span><p>{source.text?.slice(0,220)}…</p><div className="source-foot"><span>{sourceType(source)==='snippet'?'Поисковый фрагмент':'Текст страницы'} · {retrievedDate(source.retrieved)}</span><span title={source.sha256}>#{source.sha256.slice(0,8)}</span></div></article>;
   })}</div>}
  </>}
 </section>;
}
