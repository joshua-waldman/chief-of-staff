import argparse
import logging
import json
from pathlib import Path
import uvicorn

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    # Record the trial process before application imports/migrations so the
    # detached updater can stop it even when startup never reaches /healthz.
    from cos.config import ROOT, DATA
    from cos.update_worker import process_identity
    trial=ROOT/'.cos-update-trial'
    if trial.exists():
        identity_file=Path(json.loads(trial.read_text(encoding='utf-8'))['identity_file'])
        if identity_file.name!='trial-process.json' or not identity_file.resolve().is_relative_to((DATA/'updates').resolve()):
            raise RuntimeError('Invalid update trial identity path.')
        identity_file.write_text(json.dumps(process_identity()),encoding='utf-8')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    uvicorn.run('cos.server:app',host='127.0.0.1',port=args.port,access_log=False)
