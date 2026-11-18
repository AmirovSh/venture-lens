'use client';
import {useEffect,useRef} from 'react';
export default function Modal({children,onClose,wide=false,label,className=''}:{children:React.ReactNode;onClose:()=>void;wide?:boolean;label:string;className?:string}){
 const ref=useRef<HTMLDivElement>(null);
 useEffect(()=>{const previous=document.activeElement as HTMLElement;const overflow=document.body.style.overflow;document.body.style.overflow='hidden';(ref.current?.querySelector<HTMLElement>('input,select,textarea,button')||ref.current)?.focus();const handler=(e:KeyboardEvent)=>{if(e.key==='Escape')onClose();if(e.key==='Tab'){const items=ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input,select,textarea,[tabindex="0"]');if(!items?.length)return;const first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){last.focus();e.preventDefault();}else if(!e.shiftKey&&document.activeElement===last){first.focus();e.preventDefault();}}};document.addEventListener('keydown',handler);return()=>{document.body.style.overflow=overflow;document.removeEventListener('keydown',handler);previous?.focus();};},[onClose]);
 return <div className="overlay" onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}><div className={'modal '+(wide?'wide ':'')+className} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={ref}>{children}</div></div>;
}
