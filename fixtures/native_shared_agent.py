"""Synthetic ACP agent for real queue-owner integration, never a model substitute."""
import json,sys,pathlib,os,re
trace=pathlib.Path(sys.argv[1]);mode=sys.argv[2];pending=None
skill_reads=[]; skill_results=[]; skill_entries=[]
if mode=='auth':
    auth_ok=os.environ.get('CALLCLI_FIXTURE_AUTH')=='SYNTHETIC_AUTH_ONLY'
    with trace.open('a')as f:f.write(json.dumps({'synthetic_auth_seen':auth_ok})+'\n')
    if not auth_ok:raise RuntimeError('Synthetic auth fixture unavailable')

def record(direction,message):
    out={'direction':direction}
    for key in ['id','method']:
        if key in message:out[key]=message[key]
    if 'error'in message:out['error']={'code':message['error'].get('code')}
    if isinstance(message.get('result'),dict)and 'stopReason'in message['result']:out['result']={'stopReason':message['result']['stopReason']}
    update=message.get('params',{}).get('update',{})
    if update:
        out['sessionUpdate']=update.get('sessionUpdate')
        if 'messageId' in update:out['messageId']=update['messageId']
    with trace.open('a')as f:f.write(json.dumps(out)+'\n')

def send(message):
    record('inbound',message)
    print(json.dumps(message),flush=True)

def answer(i,result):send({'jsonrpc':'2.0','id':i,'result':result})
def text(value, message_id=None):
    update={'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':value}}
    if message_id is not None:update['messageId']=message_id
    send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':update}})

for line in sys.stdin:
    m=json.loads(line);record('outbound',m);method=m.get('method');i=m.get('id')
    if method=='initialize':
        answer(i,{'protocolVersion':1,'agentCapabilities':{'loadSession':True},'agentInfo':{'name':'synthetic-acp-fixture','version':'1'},'authMethods':[]})
    elif method=='session/new':answer(i,{'sessionId':'fixture-session'})
    elif method in ['session/load','session/resume','authenticate']:answer(i,{})
    elif method=='session/prompt':
        text('CALLCLI_STAGE synthetic protocol fixture started\n')
        if mode=='auth-fail':
            text('SYNTHETIC_PARTIAL_RESULT\n')
            send({'jsonrpc':'2.0','id':i,'error':{'code':-32000,'message':'PRIVATE_SYNTHETIC_ERROR',
                  'data':{'authRequired':True,'private':'PRIVATE_SYNTHETIC_DATA'}}})
        elif mode=='fail':send({'jsonrpc':'2.0','id':i,'error':{'code':-32000,'message':'Synthetic main prompt failure'}})
        elif mode=='block':pending=i
        elif mode=='drop':sys.exit(0)
        elif mode=='intervention':
            prompt='\n'.join(part.get('text','') for part in m['params']['prompt'])
            if 'INITIAL_BLOCK' in prompt:
                text('SYNTHETIC_PARTIAL_RESULT\n')
                pending=i
            elif prompt.endswith('SYNTHETIC_REVISION'):
                text('SYNTHETIC_TARGET_RESULT revision received\n')
                answer(i,{'stopReason':'end_turn'})
            else:
                send({'jsonrpc':'2.0','id':i,'error':{'code':-32000,'message':'Synthetic revision missing'}})
        elif mode=='questions':
            prompt='\n'.join(part.get('text','') for part in m['params']['prompt'])
            if 'INITIAL_QUESTION' in prompt or 'REASK_QUESTION' in prompt:
                text('SYNTHETIC_PARTIAL_RESULT\nCALLCLI_QUES', 'message-a')
                text('TION Q1 Which format?', 'message-a')
                send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'tool_call','toolCallId':'question-tool','kind':'other','status':'pending'}}})
                send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'tool_call_update','toolCallId':'question-tool','status':'completed'}}})
                text('CALLCLI_QUESTION Q1 Which format?', 'message-b')
            elif 'INDEPENDENT_QUESTIONS' in prompt:
                text('EARLY_RESULT\nCALLCLI_QUESTION Q1 Same?\nCALLCLI_QUESTION Q2 Same?\n'
                     'CALLCLI_QUESTION Q1 Format, JSON or CSV?\nLATE_RESULT\n', 'message-c')
                for suffix in 'ABA':
                    text('CALLCLI_QUESTION Q3 '+'x'*7997, 'message-long')
                    text(suffix+'\n', 'message-long')
            else:
                text('SYNTHETIC_APPLIED '+prompt+'\n', 'message-d')
            answer(i,{'stopReason':'end_turn'})
        elif mode=='skills':
            prompt='\n'.join(part.get('text','') for part in m['params']['prompt'])
            skill_entries=[json.loads(line) for line in prompt.splitlines() if line.startswith('{"id": "skill-')]
            skill_results=[]
            for entry in skill_entries:
                skill_file=pathlib.Path(entry['skill_file'])
                text_content=skill_file.read_text()
                value=(skill_file.parent/'references/value.txt').read_text()
                skill_results.append(value)
                send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'tool_call','toolCallId':entry['id'],'title':'read_skill_resources','kind':'read','status':'completed'}}})
                text('CALLCLI_SKILL '+entry['id']+' adopted Read SKILL.md and reference\n')
            text('RESOURCE_VALUES '+json.dumps(skill_results)+'\n')
            answer(i,{'stopReason':'end_turn'});pending=None
        else:
            pending=i
            send({'jsonrpc':'2.0','id':i,'method':'fs/read_text_file','params':{'sessionId':'fixture-session','path':str(trace.parent/'deliberately-absent-fixture.txt')}})
    elif method=='session/cancel' and pending is not None:
        answer(pending,{'stopReason':'cancelled'});pending=None
    elif method is None and i==pending and ('error'in m or 'result'in m):
        if 'error'not in m:raise RuntimeError('Expected real missing-read error from native client')
        text('RECOVERED_SYNTHETIC_READ_ERROR\n');answer(pending,{'stopReason':'end_turn'});pending=None
    elif i is not None:send({'jsonrpc':'2.0','id':i,'error':{'code':-32601,'message':'Fixture method unsupported'}})
