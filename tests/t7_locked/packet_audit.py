"""Strict official-schema capture audit; intentionally malformed replies stay explicit."""
import json
from pathlib import Path
from .schema_check import Schemas,SchemaError

NAMES={'initialize':'Initialize','thread/start':'ThreadStart','thread/resume':'ThreadResume','thread/read':'ThreadRead','thread/list':'ThreadList','thread/items/list':'ThreadItemsList','thread/turns/list':'ThreadTurnsList','thread/backgroundTerminals/list':'ThreadBackgroundTerminalsList','turn/start':'TurnStart','turn/steer':'TurnSteer'}

def audit_capture(path):
    records=[json.loads(line) for line in Path(path).read_text().splitlines()];schemas=Schemas(Path(__file__).with_name('schemas'));requests={};negative={};typed=0;expected_negative=0;tokens={}
    assert not any(r['kind'] in ['schema-failure','handler-failure'] for r in records),'OWN_PACKET_SCHEMA_OR_HANDLER_FAILURE'
    for record in records:
        kind=record['kind'];payload=record.get('payload')
        if kind=='client-request':
            if 'id' not in payload:schemas.validate('ClientNotification.json',payload);typed+=1;continue
            schemas.validate('JSONRPCRequest.json',payload);schemas.validate('ClientRequest.json',payload);method=payload['method'];params=payload.get('params',{});key=(record['connection_id'],payload['id']);requests[key]=payload
            schemas.validate(('v1/' if method=='initialize' else 'v2/')+NAMES[method]+'Params.json',params);typed+=3
            if params.get('cursor'):
                scope=(method,params.get('threadId'),params.get('turnId'),params.get('sortDirection') or ('desc' if method in ['thread/turns/list','thread/list'] else 'asc'))
                assert params['cursor'] in tokens,'OWN_UNISSUED_OPAQUE_CURSOR'
                assert tokens[params['cursor']]==scope,'OWN_CURSOR_SCOPE_OR_DIRECTION_CHANGED'
        elif kind=='intentional-after-acceptance-reply-fault':negative[(record['connection_id'],record['request_id'])]=record['mode']
        elif kind=='server-response':
            key=(record['connection_id'],payload['id']);request=requests[key];method=request['method'];assert method==record['method']
            if 'error' in payload:schemas.validate('JSONRPCError.json',payload);typed+=1;continue
            schemas.validate('JSONRPCResponse.json',payload);typed+=1
            name=('v1/' if method=='initialize' else 'v2/')+NAMES[method]+'Response.json'
            if negative.get(key) in ['malformed-history','malformed-result']:
                try:schemas.validate(name,payload['result'])
                except SchemaError:expected_negative+=1
                else:raise AssertionError('OWN_NEGATIVE_FAULT_ACCIDENTALLY_SCHEMA_VALID')
            else:schemas.validate(name,payload['result']);typed+=1
            params=request.get('params',{});result=payload['result']
            if isinstance(result,dict) and result.get('nextCursor'):
                tokens[result['nextCursor']]=(method,params.get('threadId'),params.get('turnId'),params.get('sortDirection') or ('desc' if method in ['thread/turns/list','thread/list'] else 'asc'))
        elif kind in ['server-notification','server-notification-dropped']:
            schemas.validate('JSONRPCNotification.json',payload);schemas.validate('ServerNotification.json',payload);typed+=2
        elif kind=='handler-cleanup':assert record['all_handlers_finished'],'OWN_NATIVE_HANDLER_NOT_QUIESCENT'
    return {'typed_schema_validations':typed,'explicit_expected_negative_packets':expected_negative,'opaque_tokens':len(tokens),'native_handler_cleanup_count':sum(r['kind']=='handler-cleanup' for r in records)}
