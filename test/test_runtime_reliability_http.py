"""The standalone acceptance must remain isolated when launched elsewhere."""
import json
import subprocess
import sys
from pathlib import Path


def test_standalone_runtime_reliability_from_empty_directory(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_runtime_reliability.py'
    process = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True,
                             text=True, timeout=90, encoding='utf-8')
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(process.stdout)
    assert report['total'] >= 28 and report['passed'] == report['total']
    assert report['data'] == 'isolated synthetic SQLite'
    assert report['llm_calls'] == report['external_service_calls'] == 0
