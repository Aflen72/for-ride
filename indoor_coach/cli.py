"""Newline delimited JSON interface; stdout contains protocol output only."""
import argparse
import json
import sys
from pathlib import Path
from .service import Service, TOOLS
from .icu import ICUError

def main():
    parser=argparse.ArgumentParser(description='Local indoor cycling agent tools (JSONL, not MCP)')
    parser.add_argument('--db',default=str(Path.home()/'.indoor-coach'/'coach.sqlite3'))
    parser.add_argument('--list-tools',action='store_true')
    args=parser.parse_args()
    if args.list_tools:
        print(json.dumps({'protocol':'indoor-coach-jsonl/1','tools':TOOLS,'external_write_enabled':False},ensure_ascii=False)); return
    service=Service(args.db)
    try:
        for line in sys.stdin:
            request_id=None
            try:
                if len(line)>2_000_000: raise ValueError('request too large')
                req=json.loads(line)
                if not isinstance(req,dict): raise ValueError('request must be an object')
                request_id=req.get('id')
                if request_id is not None and not isinstance(request_id,(str,int)): raise ValueError('id must be string or integer')
                result=service.call(req['tool'],req.get('arguments',{}))
                response={'id':request_id,'result':result}
            except ICUError as exc:
                response={'id':request_id,'error':exc.as_dict()}
            except (ValueError,KeyError,TypeError) as exc:
                # Validation messages never include input payloads or credentials.
                response={'id':request_id,'error':{'code':'invalid_request','message':'Invalid request or unsupported tool. Check documented schema.'}}
            except Exception:
                response={'id':request_id,'error':{'code':'operation_failed','message':'Operation failed; check local configuration/network. Secret details are not logged.'}}
            print(json.dumps(response,ensure_ascii=False,allow_nan=False),flush=True)
    finally: service.store.close()

if __name__=='__main__': main()
