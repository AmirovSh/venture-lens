"""Build a narrated, captioned walkthrough from real CUA screenshots.

Inputs: .platform/video/screens, voice-NN.mp3 and Edge sentence timings.
No browser automation or synthetic application results are generated here.
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
import imageio_ffmpeg

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.platform/video'
OUTPUT=ROOT/'deliverables'
FFMPEG=imageio_ffmpeg.get_ffmpeg_exe()
SCENES=[('overview','От идеи к первой оплате'),('overview','Честный результат исследования'),
        ('new-research','Настройте условия запуска'),('card-overview','Одинаковая структура карточек'),
        ('evidence','Паспорт каждого утверждения'),('risks','Критика и независимая проверка'),
        ('audit','Изоляция видна в аудите'),('comparison','Сравните модели по одним правилам'),
        ('plan','От гипотезы к эксперименту'),('settings','Сохранение работы и границы надёжности'),
        ('overview','Что развивать дальше')]


def duration(path):
    result=subprocess.run([FFMPEG,'-i',str(path)],capture_output=True,text=True)
    match=re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)',result.stderr)
    if not match:raise RuntimeError('Cannot read media duration: '+path.name)
    h,m,s=map(float,match.groups());return h*3600+m*60+s


def wrap(draw,text,font,width):
    lines=[];line=''
    for word in text.split():
        candidate=(line+' '+word).strip()
        if draw.textlength(candidate,font=font)>width and line:lines.append(line);line=word
        else:line=candidate
    if line:lines.append(line)
    return lines


def main():
    OUTPUT.mkdir(exist_ok=True);frames=STATE/'frames';frames.mkdir(exist_ok=True)
    voices=[STATE/f'voice-{i:02d}.mp3' for i in range(len(SCENES))]
    lengths=[duration(p) for p in voices]
    speed=max(1,sum(lengths)/174)
    font=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',30)
    heading=ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf',35)
    small=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',21)
    concat=[];subtitles=[];timeline=[];cursor=0.;frame_id=0
    for scene,(screen,title) in enumerate(SCENES):
        base=Image.new('RGB',(1920,1080),'#f5f7f2');draw=ImageDraw.Draw(base)
        draw.text((58,24),'venturelens.',font=heading,fill='#24543d')
        draw.text((370,29),title,font=font,fill='#24392d')
        draw.text((1710,32),f'{scene+1:02d} / {len(SCENES):02d}',font=small,fill='#748077')
        if scene==len(SCENES)-1:
            draw.text((150,180),'Полная версия',font=heading,fill='#24543d')
            draw.text((1030,180),'Оставить за рамками теста',font=heading,fill='#24543d')
            left=['Устойчивый поиск и точная юрисдикция','Калибровка на реальных экспериментах','Регулярные внешние копии и мониторинг']
            right=['Регистрация и сложные роли','Kubernetes и расширенный GitOps','Инфраструктура до проверки ценности']
            for i,line in enumerate(left):draw.text((150,270+i*90),'• '+line,font=font,fill='#24392d')
            for i,line in enumerate(right):draw.text((1030,270+i*90),'• '+line,font=font,fill='#24392d')
            draw.text((150,660),'Главное — карточки, доказательства и честные выводы.',font=heading,fill='#24543d')
            draw.text((150,755),'venture-lens.duckdns.org',font=heading,fill='#24543d')
        else:
            screenshot=Image.open(STATE/'screens'/f'{screen}.jpg').convert('RGB')
            screenshot=ImageOps.contain(screenshot,(1800,810),Image.Resampling.LANCZOS)
            base.paste(screenshot,((1920-screenshot.width)//2,90+(810-screenshot.height)//2))
        draw.line((58,925,1862,925),fill='#dbe4d5',width=2)
        draw.line((58,925,58+int(1804*(scene+1)/len(SCENES)),925),fill='#34805b',width=4)
        metadata=[json.loads(line) for line in (STATE/f'voice-{scene:02d}.jsonl').read_text().splitlines() if line.strip()]
        metadata=[item for item in metadata if item['type']=='SentenceBoundary']
        if not metadata:raise RuntimeError('Missing sentence boundaries')
        starts=[0.]+[min(lengths[scene],item['offset']/1e7) for item in metadata[1:]]
        ends=starts[1:]+[lengths[scene]]
        timeline.append({'scene':scene+1,'title':title,'start':round(cursor,3),'duration':round(lengths[scene]/speed,3),'screen':screen})
        for item,start,end in zip(metadata,starts,ends):
            lines=wrap(draw,item['text'],font,1780)
            groups=[lines[i:i+3] for i in range(0,len(lines),3)]
            for group in groups:
                frame=base.copy();canvas=ImageDraw.Draw(frame)
                for j,line in enumerate(group):canvas.text((70,946+j*36),line,font=font,fill='#223b2c')
                span=(end-start)/speed/len(groups)
                filename=f'frame-{frame_id:04d}.jpg';frame.save(frames/filename,quality=94)
                concat+=['file '+filename,f'duration {span:.6f}']
                subtitles.append({'start':cursor,'end':cursor+span,'text':'\n'.join(group)})
                cursor+=span;frame_id+=1
    concat.append('file '+filename)
    (frames/'frames.txt').write_text('\n'.join(concat)+'\n')
    (STATE/'audio.txt').write_text('\n'.join("file '"+p.name+"'" for p in voices)+'\n')
    subprocess.run([FFMPEG,'-y','-f','concat','-safe','0','-i','audio.txt','-af',f'atempo={speed:.8f}',
                    '-c:a','aac','-b:a','160k','audio.m4a'],cwd=STATE,check=True,capture_output=True)
    video=OUTPUT/'venture-lens-demo.mp4'
    subprocess.run([FFMPEG,'-y','-f','concat','-safe','0','-i','frames.txt','-i',str(STATE/'audio.m4a'),
                    '-r','24','-c:v','libx264','-preset','medium','-crf','20','-threads','2','-pix_fmt','yuv420p',
                    '-c:a','copy','-shortest','-movflags','+faststart',str(video)],cwd=frames,check=True,capture_output=True)
    actual=duration(video)
    assert actual<=180 and actual>=60
    def stamp(value):
        milliseconds=round(value*1000);s,ms=divmod(milliseconds,1000);m,s=divmod(s,60);h,m=divmod(m,60)
        return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'
    (OUTPUT/'venture-lens-demo.srt').write_text('\n\n'.join(f"{i+1}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}" for i,s in enumerate(subtitles))+'\n',encoding='utf-8')
    proof={'file':video.name,'duration_seconds':actual,'resolution':'1920x1080','fps':24,'audio':'Russian synthetic narration; silent closing card',
           'captions':'burned in; SRT also supplied','scenes':timeline,'bytes':video.stat().st_size,
           'sha256':hashlib.file_digest(video.open('rb'),'sha256').hexdigest(),'source':'real current service screenshots; edited walkthrough'}
    (OUTPUT/'video-verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in proof.items() if k!='scenes'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
