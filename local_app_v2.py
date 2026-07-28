from __future__ import annotations
import asyncio, json, os, queue, re, shutil, sqlite3, subprocess, threading, time, uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request as UrlRequest, urlopen
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
import uvicorn
from real_media_pipeline import process_video

ROOT=Path(__file__).resolve().parent
DATA_ROOT=Path(os.getenv('VIDEO_LOCALIZER_DATA_DIR',str(ROOT))).resolve()
WORK=DATA_ROOT/'local_jobs'; DOWNLOAD=DATA_ROOT/'downloads'; OUTPUT=DATA_ROOT/'output'
DATA_ROOT.mkdir(parents=True,exist_ok=True); WORK.mkdir(parents=True,exist_ok=True); DOWNLOAD.mkdir(parents=True,exist_ok=True); OUTPUT.mkdir(parents=True,exist_ok=True)
PANEL=ROOT/'control_panel_v3.html'
app=FastAPI(title='Video Localizer Real Workflow',version='0.4.0')
FONT_CATALOG=[
 {'id':'aptos','name':'Aptos','family':'Aptos','style':'现代无衬线，屏幕阅读清晰'},
 {'id':'segoe-ui','name':'Segoe UI','family':'Segoe UI','style':'Windows 原生界面字体'},
 {'id':'noto-sans','name':'Noto Sans','family':'Noto Sans','style':'多语言覆盖稳定'},
 {'id':'source-han-sans','name':'思源黑体','family':'Source Han Sans SC','style':'中文与马来语字幕清晰'},
]
app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:8790','http://localhost:8790'],allow_methods=['*'],allow_headers=['*'])
DB_PATH=DATA_ROOT/'local_jobs.sqlite3'
jobs:dict[str,dict[str,Any]]={}; subscribers:set[queue.Queue]=set(); lock=threading.RLock(); stop_all_event=threading.Event(); processing_lock=threading.RLock()

def _db_init():
 with sqlite3.connect(DB_PATH) as conn:
  conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)')
  conn.commit()

def persist_job(job_id: str):
 job=jobs.get(job_id)
 if not job: return
 payload=json.dumps(job,ensure_ascii=False)
 with sqlite3.connect(DB_PATH,timeout=30) as conn:
  conn.execute('INSERT INTO jobs(id,payload,updated_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',(job_id,payload,time.time()))
  conn.commit()

def persist_all():
 for job_id in list(jobs): persist_job(job_id)

def load_jobs():
 _db_init()
 with sqlite3.connect(DB_PATH) as conn:
  rows=conn.execute('SELECT id,payload FROM jobs ORDER BY updated_at ASC').fetchall()
 for job_id,payload in rows:
  try:
   job=json.loads(payload)
   if job.get('status') in ('queued','processing','downloading','running'):
    job.update({'status':'interrupted','error':'服务重启时任务被中断；原始输入、进度快照和已生成文件已保留','recovered_after_restart':True,'interrupted_at':time.strftime('%Y-%m-%d %H:%M:%S')})
   jobs[job_id]=job
   if job.get('recovered_after_restart'):
    persist_job(job_id)
  except (json.JSONDecodeError,TypeError):
   continue

def migrate_legacy_json():
 candidates=[ROOT/'douyin_batch_success.json',ROOT/'panel_real_job_final.json',ROOT/'panel_real_job_cache_result.txt',ROOT/'panel_real_job_result.txt']
 for path in candidates:
  if not path.exists() or path.suffix.lower() != '.json': continue
  try:
   payload=json.loads(path.read_text(encoding='utf-8-sig'))
   if isinstance(payload,dict) and payload.get('id') and payload['id'] not in jobs:
    jobs[payload['id']]=payload
    persist_job(payload['id'])
  except (OSError,UnicodeError,json.JSONDecodeError):
   continue
_db_init()
load_jobs()
migrate_legacy_json()
VOICE_CATALOG=[
 {'id':'en-us-jenny','name':'Jenny','language':'English','locale':'en-US','gender':'女声','style':'自然温暖 · 讲解/短视频'},
 {'id':'en-us-guy','name':'Guy','language':'English','locale':'en-US','gender':'男声','style':'清晰沉稳 · 新闻/教程'},
 {'id':'en-gb-sonia','name':'Sonia','language':'English','locale':'en-GB','gender':'女声','style':'英式纪录片'},
 {'id':'ms-my-osman','name':'Osman','language':'Bahasa Melayu','locale':'ms-MY','gender':'男声','style':'自然广告'},
 {'id':'ms-my-yasmin','name':'Yasmin','language':'Bahasa Melayu','locale':'ms-MY','gender':'女声','style':'柔和短视频'},
 {'id':'zh-cn-xiaoxiao','name':'Xiaoxiao','language':'中文','locale':'zh-CN','gender':'女声','style':'活泼短视频'},
 {'id':'zh-cn-yunxi','name':'Yunxi','language':'中文','locale':'zh-CN','gender':'男声','style':'纪录片解说'},
 {'id':'ja-jp-nanami','name':'Nanami','language':'日本語','locale':'ja-JP','gender':'女声','style':'自然清晰'},
 {'id':'ko-kr-sunhi','name':'SunHi','language':'한국어','locale':'ko-KR','gender':'女声','style':'柔和综艺'},
 {'id':'es-mx-dalia','name':'Dalia','language':'Español','locale':'es-MX','gender':'女声','style':'热情短视频'},
 {'id':'fr-fr-denise','name':'Denise','language':'Français','locale':'fr-FR','gender':'女声','style':'优雅纪录片'},
 {'id':'de-de-conrad','name':'Conrad','language':'Deutsch','locale':'de-DE','gender':'男声','style':'专业教程'},
]

def emit(event:dict[str,Any]):
 payload=json.dumps(event,ensure_ascii=False)
 with lock:
  for s in list(subscribers):
   try:s.put_nowait(payload)
   except Exception:subscribers.discard(s)

def log(job_id,stage,message,progress,**extra):
 progress=float(max(0,min(100,float(progress))))
 if progress.is_integer(): progress=int(progress)
 now=time.strftime('%Y-%m-%d %H:%M:%S')
 event={'job_id':job_id,'stage':stage,'message':message,'progress':progress,'time':time.strftime('%H:%M:%S'),**extra}
 job=jobs[job_id]
 job['progress']=progress
 job['current_stage']=stage
 job['current_message']=message
 job['updated_at']=now
 job.setdefault('logs',[]).append(event)
 # 防止超长任务无限膨胀，同时完整保留最近实时进度。
 if len(job['logs'])>2000: job['logs']=job['logs'][-2000:]
 persist_job(job_id); emit(event)

def analyze_voice_emotion(job_id):
 segments=[]
 for i,(start,end,gender,emotion) in enumerate([(0.88,3.18,'女声','calm'),(3.18,5.14,'女声','surprised'),(6.10,8.14,'男声','excited'),(8.14,10.34,'女声','happy'),(40.72,43.30,'男声','angry'),(68.0,71.56,'男声','angry'),(82.62,85.92,'女声','excited'),(88.5,93.24,'女声','happy')]):
  duration=end-start; raw=duration*1.12
  speed=min(1.18,max(1.0,raw/(duration+0.25)))
  segments.append({'index':i+1,'speaker_id':'speaker_1' if gender=='女声' else 'speaker_2','gender':gender,'gender_confidence':0.84 if gender=='女声' else 0.79,'emotion':emotion,'emotion_confidence':0.71,'intensity':0.72,'energy':0.68,'pitch':'high' if gender=='女声' else 'low','speech_rate':1.0,'original_start':start,'original_end':end,'original_duration':round(duration,3),'tts_raw_duration':round(raw,3),'silence_before_available':0.25,'silence_after_available':0.35,'natural_compression_ratio':0.93,'borrowed_before':0.0,'borrowed_after':round(max(0,raw*0.93-duration),3),'final_speed_ratio':round(speed,3),'audio_window_start':start,'first_character_start':start,'final_start':start,'final_end':round(start+duration,3),'overlong_strategy':'情绪保持 + 自然压缩 + 借用句间静音','subtitle_wrap':'按语义换行'})
 return segments

def clean_douyin_text(raw: str) -> list[str]:
    urls = re.findall(r'https?://[^\s]+', raw or '')
    cleaned=[]
    for value in urls:
        value=value.strip(' \\t\\r\\n，。！？；,.;!?）)]}')
        if 'douyin.com' in value or 'iesdouyin.com' in value:
            cleaned.append(value)
    return list(dict.fromkeys(cleaned))

def repair_mojibake(value: str) -> str:
    if not isinstance(value, str): return value
    markers=sum(value.count(x) for x in ('Ã','Â','æ','ç','å','é','è','å'))
    if markers < 1: return value
    try:
        repaired=value.encode('latin1').decode('utf-8')
        return repaired if sum(repaired.count(x) for x in ('Ã','Â','æ','ç','å','é','è')) < markers else value
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value

def safe_title(title: str, fallback: str) -> str:
    value=re.sub(r'[<>:"/\\\\|?*\\x00-\\x1f]', '_', (title or '').strip())
    value=re.sub(r'\\s+', ' ', value).strip(' .')
    return (value[:120] or fallback)

def parse_douyin(url: str) -> dict[str, Any]:
    endpoint='https://api.xcboke.cn/api/dy?url='+quote(url, safe='')
    response=subprocess.run(['curl.exe','-L','--fail','--silent','--show-error','--max-time','40','-A','Mozilla/5.0',endpoint],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=50)
    if response.returncode:
        raise RuntimeError(response.stderr.decode('utf-8','replace') or '抖音解析请求失败')
    payload=json.loads(response.stdout.decode('utf-8'))
    if payload.get('code') != 200 or not payload.get('data',{}).get('url'):
        raise RuntimeError(payload.get('msg') or '抖音链接解析失败')
    data=payload['data']
    return {'source_url':url,'title':repair_mojibake(data.get('title') or 'douyin_video'),'video_url':data['url'],'cover':data.get('cover',''),'author':repair_mojibake(data.get('author',''))}

def download_douyin(item: dict[str,Any], index: int) -> Path:
    title=safe_title(item.get('title'), f'douyin_{index:03d}')
    target=DOWNLOAD/(title+'.mp4')
    if target.exists() and target.stat().st_size>0:
        return target
    temp=DOWNLOAD/(title+'.part.mp4')
    try:
        subprocess.run(['curl.exe','-L','--fail','--silent','--show-error','--retry','2','--connect-timeout','20','--max-time','600','-A','Mozilla/5.0','-o',str(temp),item['video_url']],check=True,timeout=660)
        temp.replace(target)
        return target
    finally:
        temp.unlink(missing_ok=True)

def process_job(job_id, parent_batch_id=None, item_index=1, item_count=1):
 j=jobs[job_id]
 try:
  with processing_lock:
   if stop_all_event.is_set():
    j.update({'status':'stopped','error':'已停止，未开始处理'}); persist_job(job_id); return
   j['status']='processing'; persist_job(job_id)
   def pipeline_log(stage, message, progress, **extra):
    if stop_all_event.is_set():
     raise RuntimeError('已请求停止全部任务')
    j['progress']=progress
    log(job_id, stage, message, progress, **extra)
    if parent_batch_id and parent_batch_id in jobs:
     parent_progress=round((((item_index-1)+(float(progress)/100.0))/max(1,item_count))*100,1)
     log(parent_batch_id,stage,f'第 {item_index}/{item_count} 条：{message}',parent_progress,child_job_id=job_id,child_progress=progress,**extra)
   result=process_video(j['stored_path'], str(OUTPUT), j['target_language'], j['source_language'], j['max_speed'], pipeline_log, mode=j['mode'], font_name=j['font_name'])
   j['segments']=result['segments']
   speaker_map={}
   for row in j['segments']:
    sid=row['speaker_id']; speaker_map[sid]={'speaker_id':sid,'gender':row['gender'],'confidence':0.80,'voice_mode':j['voice_mode']}
   j['speakers']=list(speaker_map.values())
   j.update({'status':'success','progress':100,'output_path':result['output_path'],'subtitle_path':result['subtitle_path'],'completed_at':time.strftime('%Y-%m-%d %H:%M:%S'),'duration_seconds':result['duration_seconds'],'asr_language':result['language'],'mode':result.get('mode',j['mode']),'subtitle_layout':result.get('subtitle_layout'),'width':result.get('width'),'height':result.get('height'),'output_dir':str(OUTPUT)}); persist_job(job_id)
 except Exception as e:
  j.update({'status':'failed','error':str(e)}); persist_job(job_id); log(job_id,'失败',str(e),j.get('progress',0))
 
@app.get('/',response_class=HTMLResponse)
def index():return PANEL.read_text(encoding='utf-8')
@app.get('/output/{filename}')
def output_file(filename: str):
 path=(OUTPUT/Path(filename).name).resolve()
 if not path.exists() or path.parent != OUTPUT.resolve():
  return {'error':'not found'}
 return FileResponse(path)

@app.get('/api/health')
def health():return {'status':'ok','workflow':'video-selection-first','sse':True,'voice_count':len(VOICE_CATALOG),'font_count':len(FONT_CATALOG),'output_dir':str(OUTPUT),'download_dir':str(DOWNLOAD),'default_mode':'subtitle_only','default_target_language':'Bahasa Melayu','douyin_api':'https://api.xcboke.cn/api/dy?url='}
@app.get('/api/voices')
def voices():return VOICE_CATALOG
@app.get('/api/fonts')
def fonts():return FONT_CATALOG
@app.post('/api/open-output')
def open_output():
 subprocess.Popen(['explorer.exe', str(OUTPUT)])
 return {'ok':True,'path':str(OUTPUT)}

@app.post('/api/open-downloads')
def open_downloads():
 subprocess.Popen(['explorer.exe', str(DOWNLOAD)])
 return {'ok':True,'path':str(DOWNLOAD)}

@app.post('/api/douyin/parse')
async def douyin_parse(request: Request):
    body=await request.json()
    raw=str(body.get('text',''))
    urls=clean_douyin_text(raw)
    if not urls: return {'items':[],'error':'未识别到抖音链接'}
    items=[]
    for index,url in enumerate(urls,1):
        try:
            item=parse_douyin(url); item['index']=index; items.append(item)
        except Exception as exc:
            items.append({'index':index,'source_url':url,'error':str(exc)})
    return {'items':items,'count':len(items)}

def process_douyin_batch(batch_id: str, items: list[dict[str,Any]], options: dict[str,Any]):
    batch=jobs[batch_id]
    batch.update({'status':'processing','progress':0,'current_stage':'下载','current_message':f'准备按顺序处理 {len(items)} 条视频'})
    persist_job(batch_id)
    for index,item in enumerate(items,1):
        if stop_all_event.is_set():
            batch.update({'status':'stopped','error':'已停止全部任务','progress':round((index-1)/len(items)*100,1)}); persist_job(batch_id); return
        child_id='VL-'+uuid.uuid4().hex[:8].upper()
        try:
            parent_progress=round((index-1)/len(items)*100,1)
            log(batch_id,'解析下载',f'开始处理第 {index}/{len(items)} 条：{item.get("title", "未命名视频")}',parent_progress)
            child={'id':child_id,'status':'downloading','progress':0,'input_name':safe_title(item.get('title'), f'douyin_{index:03d}')+'.mp4','stored_path':'','target_language':options['target_language'],'source_language':options['source_language'],'mode':'subtitle_only','font_name':options['font_name'],'voice_mode':'按说话人自动匹配','max_speed':options['max_speed'],'logs':[],'source_type':'douyin','source_url':item['source_url'],'title':item['title'],'download_path':''}
            jobs[child_id]=child
            batch.setdefault('items',[]).append(child)
            persist_job(child_id); persist_job(batch_id)
            path=download_douyin(item,index)
            child.update({'status':'queued','input_name':path.name,'stored_path':str(path),'download_path':str(path)})
            persist_job(child_id)
            log(batch_id,'下载完成',f'已下载到本地：{path.name}',round((index-1)/len(items)*100,1))
            process_job(child_id, batch_id, index, len(items))
            batch['items']=[jobs.get(x.get('id'),x) for x in batch.get('items',[])]; persist_job(batch_id)
            if jobs[child_id].get('status')!='success':
                log(batch_id,'失败',jobs[child_id].get('error','视频处理失败'),round(index/len(items)*100,1))
            else:
                log(batch_id,'完成',f'第 {index}/{len(items)} 条完成：{Path(jobs[child_id]["output_path"]).name}',round(index/len(items)*100,1))
        except Exception as exc:
            failed={'id':child_id,'status':'failed','progress':0,'title':item.get('title'),'source_url':item.get('source_url'),'error':str(exc),'logs':[]}
            jobs[child_id]=failed
            batch['items']=[x for x in batch.get('items',[]) if x.get('id')!=child_id]+[failed]
            persist_job(child_id); persist_job(batch_id)
            log(batch_id,'失败',f'第 {index}/{len(items)} 条失败：{str(exc)}',round(index/len(items)*100,1))
    if any(x.get('status')=='failed' for x in batch.get('items',[])):
        batch.update({'status':'failed','progress':100,'error':'批次中存在失败子任务','completed_at':time.strftime('%Y-%m-%d %H:%M:%S')})
        persist_job(batch_id)
    else:
        batch.update({'status':'success','progress':100,'completed_at':time.strftime('%Y-%m-%d %H:%M:%S')})
        persist_job(batch_id)

@app.post('/api/stop-all')
def stop_all():
    stop_all_event.set()
    stopped=0
    for job in jobs.values():
        if job.get('status') in ('queued','processing'):
            job['stop_requested']=True; persist_job(job['id']); stopped+=1
    emit({'stage':'停止','progress':0,'message':f'已请求停止全部任务，共 {stopped} 项'})
    return {'ok':True,'stopped':stopped}
@app.post('/api/douyin/jobs')
async def create_douyin_jobs(request: Request):
    global stop_all_event
    body=await request.json()
    raw=str(body.get('text',''))
    urls=clean_douyin_text(raw)
    if not urls: return {'error':'未识别到抖音链接'}
    parsed=[]
    errors=[]
    for index,url in enumerate(urls,1):
        try:
            item=parse_douyin(url); item['index']=index; parsed.append(item)
        except Exception as exc:
            errors.append({'source_url':url,'error':str(exc)})
    if not parsed: return {'error':'所有抖音链接解析失败','errors':errors}
    stop_all_event.clear()
    batch_id='VDB-'+uuid.uuid4().hex[:8].upper()
    options={'target_language':body.get('target_language','Bahasa Melayu'),'source_language':body.get('source_language','auto'),'font_name':body.get('font_name','Aptos'),'max_speed':float(body.get('max_speed',1.18))}
    jobs[batch_id]={'id':batch_id,'status':'queued','progress':0,'input_name':f'抖音批量任务（{len(parsed)}条）','source_type':'douyin_batch','items':[],'parsed_items':parsed,'errors':errors,'download_dir':str(DOWNLOAD),'output_dir':str(OUTPUT),'target_language':options['target_language'],'mode':'subtitle_only','created_at':time.strftime('%Y-%m-%d %H:%M:%S'),'logs':[]}
    persist_job(batch_id)
    threading.Thread(target=process_douyin_batch,args=(batch_id,parsed,options),daemon=True).start()
    emit({'job_id':batch_id,'stage':'队列','progress':0,'message':f'已解析 {len(parsed)} 条抖音链接，按顺序处理'})
    return jobs[batch_id]

@app.get('/api/jobs')
def list_jobs():
 return sorted(jobs.values(),key=lambda item:item.get('updated_at') or item.get('completed_at') or item.get('created_at') or '',reverse=True)
@app.get('/api/jobs/{job_id}')
def get_job(job_id):return jobs.get(job_id,{'error':'not found'})
@app.post('/api/jobs')
async def create_job(file:UploadFile=File(...),target_language:str=Form('Bahasa Melayu'),source_language:str=Form('auto'),mode:str=Form('subtitle_only'),font_name:str=Form('Aptos'),voice_mode:str=Form('按说话人自动匹配'),preserve_background:bool=Form(True),bilingual_subtitles:bool=Form(True),max_speed:float=Form(1.18)):
 stop_all_event.clear()
 job_id='VL-'+uuid.uuid4().hex[:8].upper(); safe=Path(file.filename or 'input.mp4').name; folder=WORK/job_id; folder.mkdir(); stored=folder/safe
 with stored.open('wb') as out:
  while chunk:=await file.read(1024*1024):out.write(chunk)
 jobs[job_id]={'id':job_id,'status':'queued','progress':0,'input_name':safe,'stored_path':str(stored),'target_language':target_language,'source_language':source_language,'mode':mode if mode in ('subtitle_only','dubbed') else 'subtitle_only','font_name':font_name or 'Aptos','voice_mode':voice_mode,'preserve_background':preserve_background,'bilingual_subtitles':bilingual_subtitles,'max_speed':max_speed,'created_at':time.strftime('%Y-%m-%d %H:%M:%S'),'logs':[]}
 persist_job(job_id)
 threading.Thread(target=process_job,args=(job_id,),daemon=True).start(); emit({'job_id':job_id,'stage':'队列','progress':0,'message':f'原视频已加入处理队列：{safe}'})
 return jobs[job_id]
@app.get('/api/logs/stream')
async def stream(request:Request):
 q=queue.Queue(); subscribers.add(q)
 async def gen():
  try:
   yield 'data: '+json.dumps({'stage':'连接','progress':0,'message':'实时日志流已连接'},ensure_ascii=False)+'\n\n'
   while not await request.is_disconnected():
    try:
     payload = await asyncio.to_thread(q.get, True, 1)
     yield 'data: '+payload+'\n\n'
    except queue.Empty:
     yield ': heartbeat\n\n'
  finally:subscribers.discard(q)
 return StreamingResponse(gen(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=int(os.getenv('VIDEO_LOCALIZER_PORT','8790')))
