'use client';
import {useCallback,useEffect,useRef,useState} from 'react';
export type ResearchEvent={id:number;at:number;stage:string;message:string;model_id:string|null;payload:Record<string,any>};
type State={runId:string;events:ResearchEvent[];total:number;hasOlder:boolean;loadingOlder:boolean;error:string};
const empty:State={runId:'',events:[],total:0,hasOlder:false,loadingOlder:false,error:''};

export default function useEventLog(runId:string,active:boolean){
 const[state,setState]=useState<State>(empty),olderLoader=useRef<()=>Promise<void>>(async()=>{}),activeRef=useRef(active);
 activeRef.current=active;
 useEffect(()=>{
  const controller=new AbortController();let cancelled=false,polling=false,loadingOlder=false,initialized=false,lastPoll=0;
  let items:ResearchEvent[]=[],total=0,before:number|null=null,error='';
  const commit=()=>{if(!cancelled)setState({runId,events:items,total,hasOlder:before!==null,loadingOlder,error});};
  const merge=(rows:ResearchEvent[])=>{items=Array.from(new Map([...items,...rows].map(row=>[row.id,row])).values()).sort((a,b)=>a.id-b.id);};
  async function page(query:string){const response=await fetch(`/api/runs/${runId}/events/page?${query}`,{signal:controller.signal});if(!response.ok)throw Error('Не удалось обновить журнал. Повторите загрузку.');return response.json();}
  async function poll(){if(cancelled||polling||!runId)return;polling=true;lastPoll=Date.now();try{
   if(!initialized){const value=await page('limit=100');if(cancelled)return;items=value.events;total=value.total;before=value.before;initialized=true;error='';commit();}
   else {let more=true;while(more&&!cancelled){const value=await page(`after=${items.at(-1)?.id||0}&limit=200`);if(cancelled)return;merge(value.events);total=Math.max(total,value.total);more=value.has_more;error='';commit();}}
  }catch(cause){if(!cancelled){error=(cause as Error).message;commit();}}finally{polling=false;}}
  olderLoader.current=async()=>{if(cancelled||loadingOlder||before===null)return;loadingOlder=true;error='';commit();try{const value=await page(`before=${before}&limit=100`);if(cancelled)return;merge(value.events);total=Math.max(total,value.total);before=value.before;}catch(cause){if(!cancelled)error=(cause as Error).message;}finally{loadingOlder=false;commit();}};
  commit();void poll();const refresh=()=>{if(document.visibilityState==='visible')void poll();};
  const timer=setInterval(()=>{if(activeRef.current||Date.now()-lastPoll>=15000)refresh();},3000);document.addEventListener('visibilitychange',refresh);
  return()=>{cancelled=true;controller.abort();clearInterval(timer);document.removeEventListener('visibilitychange',refresh);olderLoader.current=async()=>{};};
 },[runId]);
 const loadOlder=useCallback(()=>olderLoader.current(),[]);
 return {...(state.runId===runId?state:empty),loadOlder};
}
