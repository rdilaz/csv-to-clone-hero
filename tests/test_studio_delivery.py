import gzip
from pathlib import Path
from flask import Flask, jsonify
from csv_to_clonehero.server.http_delivery import ASSET_VERSION, configure_asset_delivery, _gzip_asset


def test_static_compression_and_versioned_cache_keep_private_responses_separate(tmp_path):
    app = Flask(__name__, static_folder=str(tmp_path), static_url_path='/static')
    configure_asset_delivery(app)
    data = b"const studio = 'music';\n" * 400
    (tmp_path / 'studio.js').write_bytes(data)
    app.add_url_rule('/api/private', view_func=lambda: jsonify({'library': 'owner'}))
    client = app.test_client()
    result = client.get('/static/studio.js?v=' + ASSET_VERSION, headers={'Accept-Encoding': 'gzip'})
    assert result.headers['Content-Encoding'] == 'gzip'
    assert 'immutable' in result.headers['Cache-Control']
    assert 'Accept-Encoding' in result.headers['Vary']
    assert gzip.decompress(result.data) == data
    assert result.headers['ETag'].startswith('W/')
    cached = client.get('/static/studio.js?v=' + ASSET_VERSION, headers={'Accept-Encoding': 'gzip', 'If-None-Match': result.headers['ETag']})
    assert cached.status_code == 304
    identity = client.get('/static/studio.js', headers={'Accept-Encoding': 'gzip;q=0'})
    assert identity.data == data and 'Content-Encoding' not in identity.headers
    assert 'immutable' not in identity.headers.get('Cache-Control', '')
    private = client.get('/api/private', headers={'Accept-Encoding': 'gzip'})
    assert 'Content-Encoding' not in private.headers
    assert 'immutable' not in private.headers.get('Cache-Control', '')
    ranged = client.get('/static/studio.js', headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-19'})
    assert ranged.status_code == 206 and ranged.data == data[:20]
    assert 'Content-Encoding' not in ranged.headers


def test_static_cache_refreshes_when_asset_changes(tmp_path):
    app = Flask(__name__, static_folder=str(tmp_path), static_url_path='/static')
    configure_asset_delivery(app)
    path = tmp_path / 'studio.css'
    first = b'body{color:lime}\n' * 400
    second = b'body{color:violet}\n' * 400
    path.write_bytes(first)
    client = app.test_client()
    assert gzip.decompress(client.get('/static/studio.css', headers={'Accept-Encoding': 'gzip'}).data) == first
    path.write_bytes(second)
    assert gzip.decompress(client.get('/static/studio.css', headers={'Accept-Encoding': 'gzip'}).data) == second
    _gzip_asset.cache_clear()
