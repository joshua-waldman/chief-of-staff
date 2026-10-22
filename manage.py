import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from cos.config import ROOT,DATA,settings,gateway_key
from cos.db import Store

parser=argparse.ArgumentParser()
sub=parser.add_subparsers(dest='command',required=True)
sub.add_parser('doctor')
sub.add_parser('backup')
move=sub.add_parser('relocate')
move.add_argument('--old-root',required=True)
move.add_argument('--new-root',required=True)
args=parser.parse_args()

if args.command=='doctor':
    from cos.integrations import auth_status,skill_inventory,mcp_inventory
    import shutil
    print(json.dumps({'gateway_configured':bool(gateway_key()),'microsoft_authenticated':auth_status()['authenticated'],'docker':bool(shutil.which('docker')),'skills':[s['name'] for s in skill_inventory()],'mcp':[m['name'] for m in mcp_inventory()],'projects':{k:Path(v).exists() for k,v in settings()['projects'].items()}},indent=2))
elif args.command=='backup':
    target=DATA/'backups'/datetime.now().strftime('%Y%m%d-%H%M%S')
    target.mkdir(parents=True)
    with sqlite3.connect(DATA/'chief.sqlite3') as source,sqlite3.connect(target/'chief.sqlite3') as dest:
        source.backup(dest)
    print('Consistent database backup:',target/'chief.sqlite3')
    print('Copy the complete data/jobs directory with this backup to preserve artifacts.')
elif args.command=='relocate':
    old=Path(args.old_root).resolve()
    new=Path(args.new_root).resolve()
    if new!=ROOT.resolve():
        raise SystemExit('New root must match this application location.')
    store=Store()
    count=0
    for row in store.rows('SELECT id,path FROM files'):
        p=Path(row['path'])
        if old in p.parents:
            target=new/p.relative_to(old)
            store.write('UPDATE files SET path=? WHERE id=?',(str(target),row['id']))
            count+=1
    # Session histories contain old attachment/workspace paths; relocate only app-root substrings.
    for job in store.rows('SELECT id,messages FROM jobs'):
        value=json.loads(job['messages'])
        encoded=json.dumps(value).replace(json.dumps(str(old))[1:-1],json.dumps(str(new))[1:-1])
        store.write('UPDATE jobs SET messages=? WHERE id=?',(encoded,job['id']))
    print('Relocated file references:',count)
