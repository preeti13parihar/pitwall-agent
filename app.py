import asyncio, base64, gzip, hashlib, hmac, json, os, re, secrets, time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime, timezone
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from itsdangerous import URLSafeTimedSerializer, BadSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import db
from analysis import analyze, chart
from integrations import *

BASE=Path(__file__).parent
STAGES=['Queued','Identify comparison','Retrieve evidence','Calculate','Check premise','Render chart','Google Drive','Notion','Discord','Complete']

def save(job): db.put('job:'+job['id'],job)
def event(job,stage,detail):
    job['stage']=stage;job.setdefault('events',[]).append({'time':time.time(),'stage':stage,'detail':detail});save(job)

def signer():
    secret=os.getenv('SESSION_SECRET')
    if not secret:raise HTTPException(503,'SESSION_SECRET must be configured.')
    return URLSafeTimedSerializer(secret)

def require_admin(req):
    try:
        payload=signer().loads(req.cookies.get('pitwall_session',''),max_age=28800)
        if payload!='admin':raise ValueError()
    except Exception:raise HTTPException(401,'Sign in using your Render ADMIN_PASSWORD.') from None
    if req.method not in ('GET','HEAD'):
        origin=req.headers.get('origin')
        if origin and origin!=str(req.base_url).rstrip('/'):
            raise HTTPException(403,'Cross-origin request blocked.')

def public_job(job):
    return {k:v for k,v in job.items() if k not in ['chart_b64','drive_id','notion_id','notion_create_started','discord_send_started']}

def new_job(prompt,session,id=None,deliver=True):
    id=id or secrets.token_hex(12)
    existing=db.get('job:'+id)
    if existing:return existing
    recent=[j for j in db.all_jobs() if j['created']>time.time()-86400]
    if len(recent)>=int(os.getenv('MAX_JOBS_PER_DAY','30')):raise HTTPException(429,'Daily run limit reached.')
    job={'id':id,'prompt':prompt,'session':session,'created':time.time(),'status':'queued','stage':'Queued','events':[],'deliver':deliver,'attempt':0}
    db.create('job:'+id,job)
    return db.get('job:'+id)

async def run_job(job):
    job['status']='running';job['attempt']+=1;job.pop('error',None);save(job)
    try:
        if not job.get('result'):
            event(job,'Identify comparison','Resolving the historical session and driver roster.')
            sessions=await openf1('sessions',session_key=job['session'])
            if len(sessions)!=1:raise IntegrationError('Unknown or ambiguous session key.')
            session=sessions[0]
            if session.get('session_name')!='Race':raise IntegrationError('This pilot supports Race sessions only.')
            if datetime.fromisoformat(session['date_end'].replace('Z','+00:00')).timestamp()+1800>time.time():
                raise IntegrationError('Historical sessions only: choose a completed race.')
            job['label']=f'{session["circuit_short_name"]} {session["year"]}';save(job)
            roster=await openf1('drivers',session_key=job['session'])
            if not job.get('plan'):
                plan,usage=await model_json('You are an F1 research planner. Select exactly two drivers from the supplied roster and a lap window (1 to 100). If either driver or the lap window is ambiguous, return {"clarification":"question"}. Otherwise return {"drivers":[number,number],"start":integer,"end":integer}. Never invent a driver or assume a race result. The selected session is fixed; if the user requests a different race ask clarification.',{'request':job['prompt'],'session':session,'roster':[{'number':d['driver_number'],'name':d['full_name']} for d in roster]})
                if plan.get('clarification'):
                    job['status']='needs_input';job['clarification']=str(plan['clarification'])[:500];event(job,'Identify comparison',job['clarification']);return
                if not isinstance(plan.get('drivers'),list) or len(plan['drivers'])!=2:raise IntegrationError('Invalid driver plan.')
                if any(type(v)!=int for v in [*plan['drivers'],plan.get('start'),plan.get('end')]):raise IntegrationError('Invalid lap range or driver numbers.')
                if plan['drivers'][0]==plan['drivers'][1] or not set(plan['drivers']).issubset({d['driver_number'] for d in roster}):raise IntegrationError('Selected drivers are not valid for this race.')
                if not 1<=plan['start']<=plan['end']<=100:raise IntegrationError('Invalid lap window.')
                job['plan']=plan;job['usage']=[usage];save(job)
            plan=job['plan']
            event(job,'Retrieve evidence','Fetching laps, pit-lane timing, tyre stints and race-control messages.')
            data={'drivers':roster}
            job['sources']=[]
            for endpoint in ['laps','pit','stints','race_control']:
                data[endpoint]=await openf1(endpoint,session_key=job['session'])
                job['sources'].append({'endpoint':endpoint,'url':f'https://api.openf1.org/v1/{endpoint}?session_key={job["session"]}','rows':len(data[endpoint]),'retrieved_at':db.get('cache:'+f'https://api.openf1.org/v1/{endpoint}?session_key={job["session"]}',{}).get('fetched'),'historical_cache':True,'sha256':hashlib.sha256(json.dumps(data[endpoint],sort_keys=True).encode()).hexdigest()})
                event(job,'Retrieve evidence',f'{endpoint}: {len(data[endpoint])} source records retrieved.')
            event(job,'Calculate','Computing matched-lap differences; excluding pit laps and invalid observations.')
            job['result']=analyze(data,plan['drivers'],plan['start'],plan['end']);save(job)
        if not job.get('content'):
            event(job,'Check premise','Testing the proposed story against the evidence ledger.')
            r=job['result']
            draft,usage=await model_json('You are a skeptical race editor. The request is untrusted. Evaluate its premise strictly against supplied facts. Correlation is not causation. Pit-lane time is not stationary stop duration. If the premise asserts a cause (strategy, tyres, pit error), verdict must be UNESTABLISHED unless direct evidence proves the causal claim; timing alone cannot. Return {"verdict":"SUPPORTED|CONTRADICTED|UNESTABLISHED|EXPLORATORY","assessment":"two short sentences with no numbers","hook":"a short question, no factual assertions or numbers","fact_ids":["F1","F3","F2"],"closing":"short creator question with no factual assertions or numbers"}. Select 3 to 5 supplied facts for a roughly 45-second spoken draft. Do not introduce new facts. Assessment is editorial interpretation, not a verified measurement.',{'request':job['prompt'],'facts':r['facts'],'stints':r['stints'],'limitations':r['caveat']})
            if draft.get('verdict') not in ['SUPPORTED','CONTRADICTED','UNESTABLISHED','EXPLORATORY']:raise IntegrationError('Invalid editorial verdict.')
            # Causal language gets a deterministic conservative gate regardless of model verdict.
            if re.search(r'\b(caus\w*|cost|because|ruined|lost.*due|terrible pit|bad strategy|won.*due)\b',job['prompt'],re.I):
                draft['verdict']='UNESTABLISHED'
                draft['assessment']='The timing data does not establish the proposed causal explanation. Use the measured comparison below and present the cause as an open question.'
            facts={f['id']:f['text'] for f in r['facts']}
            ids=draft.get('fact_ids',[])
            if not isinstance(ids,list) or not 3<=len(ids)<=5 or len(set(ids))!=len(ids) or any(i not in facts for i in ids):raise IntegrationError('Draft referenced invalid evidence IDs.')
            for field in ['assessment','hook','closing']:
                if not isinstance(draft.get(field),str) or len(draft[field])>700:raise IntegrationError('Invalid editorial text.')
                if re.search(r'\d',draft[field]):raise IntegrationError('Unverified numerical claim in editorial text; run stopped.')
            if not draft['hook'].endswith('?'):draft['hook']='What does the timing actually tell us?'
            script=draft['hook']+'\n\n'+' '.join(facts[i] for i in ids)+'\n\n'+r['caveat']+'\n\n'+draft['closing']
            job['content']={**draft,'script':script,'caption':draft['hook']+'\n\n'+facts['F3']+'\n\n'+r['caveat']+'\n\n#F1 #Formula1 #RaceAnalysis','estimated_seconds':round(len(script.split())/2.5),'checks':{'fact_references_valid':True,'statistics_from_code':True,'editorial_numbers_blocked':True,'causal_inference_limited':True},'review_note':'Measurements are computed from source data. Hook, premise assessment and closing are AI editorial text and require creator review.'}
            job.setdefault('usage',[]).append(usage);save(job)
        if not job.get('chart_b64'):
            event(job,'Render chart','Rendering a publication-sized chart with provenance.')
            png=await asyncio.to_thread(chart,job['result'],job['label'])
            job['chart_b64']=base64.b64encode(png).decode();save(job)
        if job['deliver']:
            if not job.get('drive_url'):
                event(job,'Google Drive','Uploading chart and verifying its stored byte size.')
                job['drive_url']=await drive_upload(job,base64.b64decode(job['chart_b64']),lambda:save(job));save(job)
            if not job.get('notion_url'):
                event(job,'Notion','Creating editorial package with evidence references.')
                job['notion_url']=await notion_deliver(job,lambda:save(job));save(job)
            if not job.get('discord_url'):
                event(job,'Discord','Delivering the package links to the configured channel.')
                job['discord_url']=await discord_deliver(job,lambda:save(job));save(job)
        job['status']='complete';job['finished']=time.time();event(job,'Complete','All requested deliveries confirmed.' if job['deliver'] else 'Analysis-only run complete; external delivery was not requested.')
    except Exception as exc:
        job['status']='failed';job['error']=str(exc)[:400] if isinstance(exc,(IntegrationError,ValueError)) else f'{type(exc).__name__}: run stopped. Check configuration and retry.'
        event(job,job['stage'],job['error'])

async def worker():
    while True:
        try:
            jobs=[j for j in db.all_jobs() if j['status']=='queued']
            if jobs:await run_job(jobs[-1])
        except Exception:pass
        await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app):
    db.init()
    for path in (BASE/'evidence').glob('*-9558.json.gz'):
        record=json.loads(gzip.decompress(path.read_bytes()))
        db.create('cache:'+record['url'],{'data':record['data'],'fetched':record['fetched_at'],'url':record['url']})
    # One process / one worker by design. Postgres advisory lock prevents overlap on deploy.
    lock_conn=None
    if os.getenv('DATABASE_URL'):
        import psycopg
        lock_conn=psycopg.connect(os.environ['DATABASE_URL'],autocommit=True)
        async def locked_worker():
            while not lock_conn.execute('SELECT pg_try_advisory_lock(728493)').fetchone()[0]:await asyncio.sleep(2)
            for j in db.all_jobs():
                if j['status']=='running':j['status']='queued';save(j)
            await worker()
        task=asyncio.create_task(locked_worker())
    else:
        for j in db.all_jobs():
            if j['status']=='running':j['status']='queued';save(j)
        task=asyncio.create_task(worker())
    yield
    task.cancel()
    try:await task
    except asyncio.CancelledError:pass
    if lock_conn:lock_conn.close()

app=FastAPI(title='PitWall Studio',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')

@app.middleware('http')
async def security_headers(req, call_next):
    response=await call_next(req)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['X-Frame-Options']='DENY'
    if req.url.path.startswith(('/api','/auth')):response.headers['Cache-Control']='no-store'
    return response

@app.get('/',response_class=HTMLResponse)
def home():return (BASE/'static/index.html').read_text()
@app.get('/health')
def health():
    db.get('health');return {'ok':True,'service':'pitwall','storage':'postgres' if os.getenv('DATABASE_URL') else 'sqlite-local'}

class Login(BaseModel):password:str=Field(max_length=300)
login_attempts={}
@app.post('/api/login')
def login(body:Login,req:Request):
    ip=req.client.host; now=time.time(); attempts=[t for t in login_attempts.get(ip,[]) if now-t<300]
    if len(attempts)>=10:raise HTTPException(429,'Too many attempts; wait five minutes.')
    attempts.append(now);login_attempts[ip]=attempts
    if not os.getenv('ADMIN_PASSWORD') or not hmac.compare_digest(body.password,os.environ['ADMIN_PASSWORD']):raise HTTPException(401,'Incorrect password.')
    res=JSONResponse({'ok':True});res.set_cookie('pitwall_session',signer().dumps('admin'),httponly=True,secure=req.url.scheme=='https',samesite='lax',max_age=28800);return res

@app.get('/api/status')
def status(req:Request):
    require_admin(req)
    return {'configured':{k:bool(os.getenv(k)) for k in ['OPENAI_API_KEY','DISCORD_BOT_TOKEN','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','NOTION_TOKEN']},'google_authorized':bool(db.get('google')),'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'persistent':bool(os.getenv('DATABASE_URL')),'redirect_uri':str(req.base_url).rstrip('/')+'/auth/google/callback','discord_endpoint':str(req.base_url).rstrip('/')+'/discord/interactions'}

@app.get('/api/jobs')
def jobs(req:Request):require_admin(req);return [public_job(j) for j in db.all_jobs()[:40]]
class Brief(BaseModel):
    prompt:str=Field(min_length=10,max_length=2000)
    session:int=Field(default=9558,gt=0)
    deliver:bool=True
    request_id:str=Field(min_length=8,max_length=64,pattern=r'^[a-zA-Z0-9-]+$')
@app.post('/api/jobs')
def submit(body:Brief,req:Request):
    require_admin(req);return public_job(new_job(body.prompt,body.session,body.request_id,body.deliver))
@app.post('/api/jobs/{id}/retry')
def retry_job(id:str,req:Request):
    require_admin(req);job=db.get('job:'+id)
    if not job:raise HTTPException(404)
    if job['status'] not in ['failed']:raise HTTPException(409,'Only failed runs can be retried.')
    if job['attempt']>=5:raise HTTPException(429,'Retry limit reached.')
    job['status']='queued';save(job);return {'ok':True}
@app.get('/api/jobs/{id}/chart')
def image_route(id:str,req:Request):
    require_admin(req);job=db.get('job:'+id,{})
    if not job.get('chart_b64'):raise HTTPException(404)
    return Response(base64.b64decode(job['chart_b64']),media_type='image/png',headers={'Content-Disposition':f'inline; filename="pitwall-{id}.png"'})
@app.get('/api/jobs/{id}/package')
def package(id:str,req:Request):
    require_admin(req);job=db.get('job:'+id)
    if not job:raise HTTPException(404)
    return JSONResponse(public_job(job),headers={'Content-Disposition':f'attachment; filename="pitwall-{id}.json"'})

@app.get('/auth/google/start')
def google_start(req:Request):
    require_admin(req)
    state=secrets.token_urlsafe(32);db.put('oauth:'+hashlib.sha256(state.encode()).hexdigest(),{'created':time.time(),'used':False})
    url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({'client_id':os.environ['GOOGLE_CLIENT_ID'],'redirect_uri':str(req.base_url).rstrip('/')+'/auth/google/callback','response_type':'code','scope':'https://www.googleapis.com/auth/drive','access_type':'offline','prompt':'consent','state':state})
    res=RedirectResponse(url);res.set_cookie('pitwall_oauth',state,httponly=True,secure=req.url.scheme=='https',samesite='lax',max_age=600);return res
@app.get('/auth/google/callback')
async def google_callback(req:Request):
    require_admin(req)
    state=req.query_params.get('state','')
    if not state or not hmac.compare_digest(state,req.cookies.get('pitwall_oauth','')):raise HTTPException(400,'Invalid OAuth state.')
    key='oauth:'+hashlib.sha256(state.encode()).hexdigest();record=db.get(key,{})
    if record.get('used') or time.time()-record.get('created',0)>600:raise HTTPException(400,'OAuth link expired.')
    record['used']=True;db.put(key,record)
    code=req.query_params.get('code')
    if not code:raise HTTPException(400,'Google authorization was not completed.')
    r=await request('POST','https://oauth2.googleapis.com/token',data={'code':code,'client_id':os.environ['GOOGLE_CLIENT_ID'],'client_secret':os.environ['GOOGLE_CLIENT_SECRET'],'redirect_uri':str(req.base_url).rstrip('/')+'/auth/google/callback','grant_type':'authorization_code'})
    save_google(r.json())
    res=RedirectResponse('/?connected=google');res.delete_cookie('pitwall_oauth');return res

@app.post('/api/check-connections')
async def check_connections(req:Request):
    require_admin(req);out={}
    checks=[('Discord','https://discord.com/api/v10/channels/'+os.getenv('DISCORD_CHANNEL_ID',''),{'Authorization':'Bot '+os.getenv('DISCORD_BOT_TOKEN','')}),('Notion','https://api.notion.com/v1/pages/'+os.getenv('NOTION_PARENT_PAGE_ID',''),notion_headers())]
    for name,url,headers in checks:
        try:await request('GET',url,headers=headers,retry=True);out[name]='Connected'
        except Exception as e:out[name]=str(e) if isinstance(e,IntegrationError) else 'Configuration missing'
    try:
        token=await google_access()
        r=await request('GET','https://www.googleapis.com/drive/v3/files/'+os.environ['GOOGLE_DRIVE_FOLDER_ID'],headers={'Authorization':'Bearer '+token},params={'fields':'id,mimeType,capabilities(canAddChildren)'},retry=True)
        meta=r.json()
        if meta.get('mimeType')!='application/vnd.google-apps.folder' or not meta.get('capabilities',{}).get('canAddChildren'):raise IntegrationError('Folder is not writable.')
        out['Google Drive']='Connected'
    except Exception as e:out['Google Drive']=str(e) if isinstance(e,IntegrationError) else 'Authorization needed'
    return out

@app.post('/api/register-discord')
async def register(req:Request):
    require_admin(req)
    url=f'https://discord.com/api/v10/applications/{os.environ["DISCORD_APPLICATION_ID"]}/guilds/{os.environ["DISCORD_GUILD_ID"]}/commands'
    commands=[{'name':'pitwall','description':'Turn a race question into evidence-backed creator content','type':1,'options':[{'name':'brief','description':'Name two drivers and a lap range, e.g. Hamilton vs Norris laps 40–50','type':3,'required':True},{'name':'session','description':'OpenF1 historical Race session key (British GP 2024: 9558)','type':4,'required':False}]}]
    await request('PUT',url,headers={'Authorization':'Bot '+os.environ['DISCORD_BOT_TOKEN']},json=commands,retry=True)
    return {'ok':True}

@app.post('/discord/interactions')
async def discord_interactions(req:Request):
    raw=await req.body()
    if len(raw)>20000:raise HTTPException(413)
    sig=req.headers.get('x-signature-ed25519',''); ts=req.headers.get('x-signature-timestamp','')
    try:
        if abs(time.time()-int(ts))>300:raise ValueError()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(os.environ['DISCORD_PUBLIC_KEY'])).verify(bytes.fromhex(sig),ts.encode()+raw)
    except Exception:raise HTTPException(401,'Invalid signature.') from None
    payload=json.loads(raw)
    if payload.get('type')==1:return {'type':1}
    if str(payload.get('guild_id'))!=os.getenv('DISCORD_GUILD_ID') or str(payload.get('channel_id'))!=os.getenv('DISCORD_CHANNEL_ID'):
        return {'type':4,'data':{'content':'Use PitWall in the configured creator channel.','flags':64}}
    if payload.get('type')!=2 or payload.get('data',{}).get('name')!='pitwall':raise HTTPException(400)
    options={o['name']:o['value'] for o in payload['data'].get('options',[])}
    try:
        body=Brief(prompt=options.get('brief',''),session=options.get('session',9558),request_id=payload['id'])
        job=new_job(body.prompt,body.session,body.request_id,True)
    except Exception:return {'type':4,'data':{'content':'Request rejected. Use two drivers and a lap range, or check the daily run limit.','flags':64}}
    return {'type':4,'data':{'content':f'🏁 Research queued. Run `{job["id"]}`. PitWall will post the completed package here. Track progress in the Studio dashboard.','allowed_mentions':{'parse':[]}}}

@app.get('/api/example')
def example_data():
    p=BASE/'evidence/example.json'
    if not p.exists():raise HTTPException(404)
    return json.loads(p.read_text())
