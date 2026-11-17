import type { Metadata } from 'next';
import './globals.css';
export const metadata:Metadata={title:'Venture Lens — бизнес-модели под микроскопом',description:'Проверяемые исследования, независимая критика и сравнение бизнес-моделей'};
export default function Layout({children}:{children:React.ReactNode}) { return <html lang="ru"><body>{children}</body></html>; }
