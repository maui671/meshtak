
#!/usr/bin/env python3
import json
import os
import uvicorn

CONFIG_JSON = '/etc/meshtak/config.json'

host = '0.0.0.0'
port = 9443
ssl_certfile = None
ssl_keyfile = None
if os.path.exists(CONFIG_JSON):
    with open(CONFIG_JSON, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    web = cfg.get('web', {})
    host = web.get('host', host) or host
    port = int(web.get('port', port) or port)
    ssl_certfile = web.get('tls_cert') or None
    ssl_keyfile = web.get('tls_key') or None
kwargs = {
    'app': 'src.api.server:create_app',
    'factory': True,
    'host': host,
    'port': port,
    'log_level': 'info',
}
if ssl_certfile and ssl_keyfile and os.path.exists(ssl_certfile) and os.path.exists(ssl_keyfile):
    kwargs['ssl_certfile'] = ssl_certfile
    kwargs['ssl_keyfile'] = ssl_keyfile
uvicorn.run(**kwargs)
