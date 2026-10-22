import argparse
import logging
from pathlib import Path
import uvicorn

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    uvicorn.run('cos.server:app',host='127.0.0.1',port=args.port,access_log=False)
