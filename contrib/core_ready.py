#!/usr/bin/env python3
'''Dependency-free systemd condition: start an indexer only after mainnet sync.'''

import argparse
import base64
import json
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit


def ready(info):
    return bool(isinstance(info,dict) and info.get('chain') == 'main'
        and info.get('initialblockdownload') is False
        and type(info.get('blocks')) is int and type(info.get('headers')) is int
        and info['blocks'] >= info['headers'] >= 0)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def transport(url,headers,body,timeout):
    request=urllib.request.Request(url,data=json.dumps(body).encode(),headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=timeout) as response:
        return json.loads(response.read(1048576))


def check(config,rpc=transport):
    try:
        parsed=urlsplit(config['url'])
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost','::1')
                or parsed.username or parsed.password):
            raise ValueError()
        auth=base64.b64encode((config['username']+':'+config['password']).encode()).decode()
        body={'jsonrpc':'1.0','id':'core-ready','method':'getblockchaininfo','params':[]}
        reply=rpc(config['url'],{'Authorization':'Basic '+auth,'Content-Type':'application/json'},body,10)
        info=reply.get('result')
        if not isinstance(info,dict):
            raise ValueError()
        is_ready=ready(info)
        return {'ready':is_ready, 'phase':'ready' if is_ready else
                ('core_syncing' if info.get('chain') == 'main' else 'wrong_network'),
                'blocks':info.get('blocks'),'headers':info.get('headers')}
    except Exception:
        return {'ready':False,'phase':'core_unavailable'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    args=parser.parse_args(argv)
    try:
        status=check(json.loads(Path(args.config).read_text()))
    except Exception:
        status={'ready':False,'phase':'configuration_error'}
    print(json.dumps(status),flush=True)
    return 0 if status['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
