"""Start the development API on the same configured port as Vite's proxy."""
import os
from pathlib import Path
import sys
from dotenv import load_dotenv
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == '__main__':
    load_dotenv(ROOT / '.env')
    port = int(os.getenv('DATA_AGENT_API_PORT', '18001'))
    if not 1 <= port <= 65535:
        raise SystemExit('Invalid DATA_AGENT_API_PORT')
    uvicorn.run('backend.main:app', host='127.0.0.1', port=port)
