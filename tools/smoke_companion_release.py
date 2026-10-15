"""Verify a built companion ZIP in an isolated extracted sandbox (no installs)."""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


CHILD_CHECK = r'''
import hashlib, io, json, os, sys, zipfile
from dataclasses import asdict
from pathlib import Path
base, runtime = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(base / 'vendor'), str(base / 'app' / 'src')]
os.environ['RAPIDFUZZ_IMPLEMENTATION'] = 'python'
from csv_to_clonehero.companion import create_app
from csv_to_clonehero.models import ChartCandidate
runtime.mkdir()
sandbox, library, cache = (runtime / name for name in ('sandbox', 'library', 'export-artifacts'))
for folder in (sandbox, library, cache): folder.mkdir()
stream = io.BytesIO()
with zipfile.ZipFile(stream, 'w') as archive:
 archive.writestr('song.ini', '[song]\nartist = Smoke Artist\nname = Smoke Chart\n')
 archive.writestr('notes.chart', '[Song]\n{\nResolution = 192\n}\n[ExpertSingle]\n{\n0 = N 0 0\n}\n')
payload = stream.getvalue()
chart = ChartCandidate(source='enchor', chart_id=1, md5='0'*31+'1', name='Smoke Chart', artist='Smoke Artist', name_norm='smoke chart', artist_norm='smoke artist', instruments=['guitar'])
key = hashlib.sha256(('enchor\0source-id:' + chart.md5).encode()).hexdigest()
(cache / (key + '.sng')).write_bytes(payload)
(library / 'untouched.sng').write_bytes(payload)
body = (json.dumps({'type':'chart','candidate':asdict(chart)}, sort_keys=True, separators=(',', ':')) + '\n').encode()
header = {'type':'enchor-catalog','format_version':1,'complete':True,'snapshot_id':'synthetic-smoke-only','chart_count':1,'out_of':1,'sha256':hashlib.sha256(body).hexdigest()}
catalog = runtime / 'synthetic-catalog.jsonl'
catalog.write_bytes((json.dumps(header) + '\n').encode()+body)
app = create_app(catalog_path=catalog, sandbox_root=sandbox, library_roots=[library], artifact_roots=[cache])
client = app.test_client()
status = client.get('/api/companion/status').get_json()
assert status['catalog_count'] == 1 and status['cached_catalog_charts'] == 1
assert client.get('/api/companion/catalog?q=smoke').get_json()['filtered'] == 1
assert client.get('/api/companion/catalog?decade=banana').status_code == 400
assert client.get('/api/companion/catalog?offset=-1').status_code == 400
assert client.get('/api/companion/status', environ_overrides={'REMOTE_ADDR':'192.0.2.3'}).status_code == 403
assert client.get('/api/companion/status', base_url='http://public.example').status_code == 403
assert client.post('/api/companion/folder', json={'path':str(sandbox)}, headers={'Origin':'https://foreign.example'}).status_code == 403
assert client.post('/api/companion/folder', data='path=ignored').status_code == 415
assert client.post('/api/companion/folder', json={'path':str(library)}).status_code == 400
assert client.post('/api/companion/folder', json={'path':str(sandbox)}).status_code == 200
install = {'catalog_key':'enchor:1','destination':str(sandbox),'confirm':True}
assert client.post('/api/companion/install', json={**install,'confirm':False}).status_code == 400
assert client.post('/api/companion/install', json=install).status_code == 201
assert client.post('/api/companion/install', json=install).status_code == 409
assert len(list(sandbox.iterdir())) == 1 and (library / 'untouched.sng').read_bytes() == payload
route = '/api/companion/audio/' + chart.md5 + '.sng'
assert client.get(route).status_code == 416
assert client.get(route, headers={'Range':'bytes=0-9'}).data == payload[:10]
assert 'csv_to_clonehero.server.web' not in sys.modules and 'httpx' not in sys.modules
assert 'cryptography' not in sys.modules and 'csv_to_clonehero.sources.enchor' not in sys.modules
import flask, pydantic_core, rapidfuzz
assert all(Path(module.__file__).resolve().is_relative_to(base / 'vendor') for module in (flask,pydantic_core,rapidfuzz))
assert rapidfuzz.fuzz.ratio.__module__ == 'rapidfuzz.fuzz_py'
print(json.dumps({'isolated_imports':True, 'pure_python_rapidfuzz':True, 'synthetic_catalog_search':True, 'sandbox_add_and_duplicate_rejection':True, 'library_preserved':True, 'loopback_origin_and_range_guards':True, 'no_provider_or_public_server_imports':True}))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    archive, work = args.archive.resolve(), args.work.resolve()
    if work.exists():
        raise ValueError("Choose a new smoke-test directory to preserve earlier evidence")
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as stream:
        for member in stream.infolist():
            destination = (work / member.filename).resolve()
            if not destination.is_relative_to(work) or member.filename.startswith(("/", "\\")):
                raise ValueError("Unsafe ZIP member")
        stream.extractall(work)
    package = next(path for path in work.iterdir() if path.is_dir())
    manifest = json.loads((package / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((package / name).read_bytes()).hexdigest() == expected, name
    base_python = sys._base_executable
    checked = subprocess.run([base_python, "-I", "-S", "-B", str(package / "bootstrap.py"), "--check", "--data-dir", str(work / "empty")], check=True, capture_output=True, text=True)
    launcher = subprocess.run(["cmd.exe", "/d", "/c", str(package / "START_COMPANION.cmd"), "--check", "--data-dir", str(work / "launcher-check")],
                              check=True, capture_output=True, text=True, timeout=30)
    child = work / "isolated_check.py"
    child.write_text(CHILD_CHECK, encoding="utf-8")
    validated = subprocess.run([base_python, "-I", "-S", "-B", str(child), str(package), str(work / "fixtures")], check=True, capture_output=True, text=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (work / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen([base_python, "-I", "-S", "-B", str(package / "bootstrap.py"), "--data-dir", str(work / "first-run"), "--port", str(port)], stdout=log, stderr=subprocess.STDOUT)
    checks = {}
    try:
        url = f"http://127.0.0.1:{port}"
        for attempt in range(100):
            if process.poll() is not None:
                raise RuntimeError("Extracted server exited before its first response")
            try:
                with urllib.request.urlopen(url + "/api/companion/status", timeout=2) as response:
                    status = json.load(response)
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.1)
        else:
            raise RuntimeError("Extracted server did not start")
        assert status["catalog_count"] == 0 and status["catalog"]["available"] is False
        assert status["cached_artifacts"] == 0 and status["library"]["roots"] == []
        assert Path(status["sandbox_root"]).is_relative_to(work / "first-run")
        with urllib.request.urlopen(url + "/", timeout=2) as response:
            assert response.status == 200 and b"Riff" in response.read()
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        for asset in ("companion.css", "companion.js", "catalog-audio.js", "catalog.js", "riff-scene.js", "signal-knot.svg"):
            with urllib.request.urlopen(url + "/static/" + asset, timeout=2) as response:
                assert response.status == 200 and response.read()
        checks = {"waitress_loopback_http": True, "empty_first_run_honest": True, "all_six_assets_http_200": True}
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()
    result = {"archive": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "size_bytes": archive.stat().st_size, "manifest_files_verified": len(manifest["files"]),
              "base_interpreter": "CPython 3.12 x64; -I -S -B (no original site-packages)",
              "runtime_check": checked.stdout.strip(), "synthetic_smoke": json.loads(validated.stdout), **checks}
    result["actual_cmd_launcher_check"] = launcher.stdout.strip()
    (work / "SMOKE_RESULT.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
