import asyncio, copy, json, os, time
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
import db,app as service
from analysis import analyze,CAVEAT
from integrations import IntegrationError

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('SQLITE_PATH',str(tmp_path/'test.db'))
    monkeypatch.setenv('ADMIN_PASSWORD','test-password')
    monkeypatch.setenv('SESSION_SECRET','test-secret-for-local-evaluations')
    monkeypatch.setenv('DISCORD_GUILD_ID','guild')
    monkeypatch.setenv('DISCORD_CHANNEL_ID','channel')
    db.init()

def dataset():
    return {'drivers':[{'driver_number':44,'full_name':'Lewis Hamilton'},{'driver_number':4,'full_name':'Lando Norris'}], 'laps':[{'driver_number':d,'lap_number':n,'lap_duration':100+(d==4)*2,'is_pit_out_lap':False} for d in [44,4] for n in range(2,9)],'pit':[],'stints':[],'race_control':[]}

def test_known_numeric_ground_truth():
    r=analyze(dataset(),[44,4],2,8)
    assert r['mean_delta']==-2
    assert sum(x['delta'] for x in r['rows'])==-14
    assert r['faster']=='Lewis Hamilton'
    assert 'not the race gap' in r['facts'][3]['text']

def test_pit_entry_and_following_lap_excluded():
    d=dataset();d['pit']=[{'driver_number':44,'lap_number':4,'pit_duration':23.123}]
    r=analyze(d,[44,4],2,8)
    assert [x['lap'] for x in r['excluded']]==[4,5]
    assert 'not stationary' in r['facts'][-1]['text']

def test_missing_lap_never_imputed():
    d=dataset();d['laps']=[x for x in d['laps'] if not(x['driver_number']==4 and x['lap_number']==3)]
    r=analyze(d,[44,4],2,8)
    assert 3 not in [x['lap'] for x in r['rows']]

def test_null_and_nan_excluded():
    d=dataset();d['laps'][0]['lap_duration']=None;d['laps'][1]['lap_duration']=float('nan')
    r=analyze(d,[44,4],2,8)
    assert len(r['rows'])==5

def test_duplicate_source_lap_excluded():
    d=dataset();d['laps'].append(copy.deepcopy(d['laps'][0]))
    r=analyze(d,[44,4],2,8)
    assert r['excluded'][0]['reason']=='Duplicate source lap'

def test_insufficient_evidence_stops():
    with pytest.raises(ValueError,match='Insufficient'):analyze(dataset(),[44,4],2,3)

@pytest.mark.parametrize('drivers,start,end',[([44,44],2,8),([44,99],2,8),([44,4],8,2),([44,4],0,8)])
def test_invalid_plan_stops(drivers,start,end):
    with pytest.raises(ValueError):analyze(dataset(),drivers,start,end)

def test_caution_event_overlap_excluded():
    d=dataset();d['laps'][0]['date_start']='2024-01-01T12:00:00+00:00'
    d['race_control']=[{'date':'2024-01-01T12:00:30+00:00','flag':'YELLOW'}]
    r=analyze(d,[44,4],2,8)
    assert 2 not in [x['lap'] for x in r['rows']]

def test_duplicate_job_creates_one_record():
    one=service.new_job('Hamilton vs Norris laps 2 to 8',9558,'same-request')
    two=service.new_job('Different payload',9558,'same-request')
    assert one==two and len(db.all_jobs())==1

def test_unauthenticated_cannot_submit_or_read():
    client=TestClient(service.app)
    assert client.get('/api/jobs').status_code==401
    assert client.post('/api/jobs',json={'prompt':'Hamilton vs Norris laps 2–8','session':9558,'request_id':'test-request'}).status_code==401

def test_invalid_discord_signature_rejected():
    client=TestClient(service.app)
    assert client.post('/discord/interactions',json={'type':1}).status_code==401

def test_valid_discord_ping(monkeypatch):
    private=Ed25519PrivateKey.generate()
    monkeypatch.setenv('DISCORD_PUBLIC_KEY',private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex())
    body=b'{"type":1}';ts=str(int(time.time()));signature=private.sign(ts.encode()+body).hex()
    client=TestClient(service.app)
    res=client.post('/discord/interactions',content=body,headers={'x-signature-ed25519':signature,'x-signature-timestamp':ts})
    assert res.json()=={'type':1}

def test_valid_command_wrong_channel_rejected(monkeypatch):
    private=Ed25519PrivateKey.generate();monkeypatch.setenv('DISCORD_PUBLIC_KEY',private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex())
    body=json.dumps({'type':2,'guild_id':'guild','channel_id':'other'}).encode();ts=str(int(time.time()))
    res=TestClient(service.app).post('/discord/interactions',content=body,headers={'x-signature-ed25519':private.sign(ts.encode()+body).hex(),'x-signature-timestamp':ts})
    assert 'configured' in res.json()['data']['content']
    assert not db.all_jobs()

def prepared_job():
    job=service.new_job('Hamilton versus Norris, laps 2 to 8',9558,'workflow-test')
    job.update({'result':analyze(dataset(),[44,4],2,8),'label':'Test race','sources':[],'content':{'verdict':'EXPLORATORY','assessment':'Test interpretation','script':'Test script','caption':'Test caption'},'chart_b64':'cG5n'})
    service.save(job);return job

def test_partial_delivery_resumes_without_duplicate_drive(monkeypatch):
    calls=[]
    async def drive(*args):calls.append('drive');return 'https://drive.google.com/example'
    async def fail(*args):calls.append('notion-fail');raise IntegrationError('Injected Notion failure')
    async def notion(*args):calls.append('notion');return 'https://notion.so/example'
    async def discord(*args):calls.append('discord');return 'https://discord.com/example'
    monkeypatch.setattr(service,'drive_upload',drive);monkeypatch.setattr(service,'notion_deliver',fail);monkeypatch.setattr(service,'discord_deliver',discord)
    job=prepared_job();asyncio.run(service.run_job(job))
    assert job['status']=='failed' and calls==['drive','notion-fail']
    # Simulated restart: reload only persisted state.
    restored=db.get('job:'+job['id']);monkeypatch.setattr(service,'notion_deliver',notion)
    asyncio.run(service.run_job(restored))
    assert restored['status']=='complete'
    assert calls==['drive','notion-fail','notion','discord']

def test_no_false_complete_on_discord_failure(monkeypatch):
    async def drive(*args):return 'https://drive.google.com/example'
    async def notion(*args):return 'https://notion.so/example'
    async def discord(*args):raise IntegrationError('Injected Discord failure')
    monkeypatch.setattr(service,'drive_upload',drive);monkeypatch.setattr(service,'notion_deliver',notion);monkeypatch.setattr(service,'discord_deliver',discord)
    job=prepared_job();asyncio.run(service.run_job(job))
    assert job['status']=='failed' and not job.get('discord_url')

def test_unknown_fact_id_blocks_delivery(monkeypatch):
    async def model(*args):return {'verdict':'SUPPORTED','assessment':'No numbers here','hook':'What happened?','fact_ids':['F1','F2','F99'],'closing':'What do you think?'},{}
    monkeypatch.setattr(service,'model_json',model)
    job=prepared_job();job.pop('content');asyncio.run(service.run_job(job))
    assert job['status']=='failed' and 'evidence IDs' in job['error'] and not job.get('drive_url')

def test_causal_premise_overrides_model_support(monkeypatch):
    async def model(*args):return {'verdict':'SUPPORTED','assessment':'The pit stop caused it','hook':'What happened?','fact_ids':['F1','F2','F3'],'closing':'What do you think?'},{}
    monkeypatch.setattr(service,'model_json',model)
    job=prepared_job();job.pop('content');job['prompt']='A terrible pit stop cost Norris the win';job['deliver']=False
    asyncio.run(service.run_job(job))
    assert job['status']=='complete' and job['content']['verdict']=='UNESTABLISHED'

def test_numeric_editorial_hallucination_blocked(monkeypatch):
    async def model(*args):return {'verdict':'SUPPORTED','assessment':'He lost 99 seconds','hook':'What happened?','fact_ids':['F1','F2','F3'],'closing':'What do you think?'},{}
    monkeypatch.setattr(service,'model_json',model)
    job=prepared_job();job.pop('content');asyncio.run(service.run_job(job))
    assert job['status']=='failed' and 'numerical' in job['error']

def test_google_tokens_encrypted_at_rest():
    from integrations import save_google,google_tokens
    save_google({'refresh_token':'private-refresh-value'})
    assert 'private-refresh-value' not in json.dumps(db.get('google'))
    assert google_tokens()['refresh_token']=='private-refresh-value'

def test_expired_oauth_state_rejected():
    client=TestClient(service.app)
    client.post('/api/login',json={'password':'test-password'})
    assert client.get('/auth/google/callback?state=unknown&code=fake').status_code==400

def test_daily_limit(monkeypatch):
    monkeypatch.setenv('MAX_JOBS_PER_DAY','1');service.new_job('First valid prompt',9558,'first-request')
    with pytest.raises(service.HTTPException) as e:service.new_job('Second valid prompt',9558,'second-request')
    assert e.value.status_code==429
