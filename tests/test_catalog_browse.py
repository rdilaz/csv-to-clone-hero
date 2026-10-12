"""Whole-catalog browsing and independent, owned ZIP selections."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.server.downloads import _validated_selections, selection_snapshot, build_archive
from tests.conftest import make_chart, FakeSource, valid_sng_bytes
from tests.test_catalog import write_snapshot
from tests.test_server_web import app, client, config, spotify_client_id, _sign_in


def _seed(client, app, config, count=251, *, sign_in=True):
    app.extensions['jobs'].shutdown()
    if sign_in:
        _sign_in(client)
    rows = []
    for index in range(count):
        chart = make_chart(f'Song {index:04}', 'Catalog Artist', chart_id=index + 1)
        chart.source = 'enchor'
        chart.source_metadata = {'preview_start_ms': 1200}
        rows.append(chart)
    config.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    write_snapshot(config.catalog_path, rows)
    app.extensions['jobs'].catalog_browser.request()
    app.extensions['jobs'].catalog_browser.prepare()
    return rows


def _pick(client, item, token, *, action='select', revision=None):
    history = client.get('/api/selection/history').get_json()
    return client.post('/api/catalog/selection', json={
        'catalog_key': item['catalog_key'], 'snapshot_token': token,
        'selection_revision': history['selection_revision'] if revision is None else revision,
        'exact_generation_id': int(history['exact_generation_id'] or 0), 'action': action})


def test_anonymous_full_catalog_pages_search_shuffle_and_real_package_metadata(client, app, config):
    _seed(client, app, config, sign_in=False)
    first = client.get('/api/catalog?limit=100').get_json()
    assert first['total_charts'] == first['total_songs'] == 251
    assert len(first['items']) == 100 and first['has_more']
    second = client.get('/api/catalog?limit=100&offset=100').get_json()
    third = client.get('/api/catalog?limit=100&offset=200').get_json()
    keys = [row['catalog_key'] for page in (first, second, third) for row in page['items']]
    assert len(keys) == len(set(keys)) == 251 and not third['has_more']
    item = first['items'][0]
    assert item['preview']['url'] == 'https://files.enchor.us/' + item['md5'] + '.sng'
    assert item['preview']['availability'] == 'unverified-until-open'
    assert item['preview_start_ms'] == 1200 and item['has_video_background'] is False
    assert 'spotify_id' not in item
    assert client.get('/api/catalog/enchor/1').get_json()['item']['catalog_key'] == item['catalog_key']
    assert client.get('/api/catalog?q=Song%200250').get_json()['total_filtered_charts'] == 1
    shuffle = client.get('/api/catalog?order=shuffle&seed=fixed&limit=100').get_json()
    assert shuffle['items'] == client.get('/api/catalog?order=shuffle&seed=fixed&limit=100').get_json()['items']
    assert shuffle['items'] != first['items']
    assert client.post('/api/catalog/selection', json={}).status_code == 401
    assert client.get('/api/catalog/selections').status_code == 401


def test_cold_catalog_read_queues_local_only_worker_work_and_never_live_fallback(client, app, config, monkeypatch):
    app.extensions['jobs'].shutdown()
    app.extensions['jobs'].source_factory = lambda _: pytest.fail('catalog browse must not crawl providers')
    cold = client.get('/api/catalog')
    assert cold.status_code == 202 and cold.get_json()['status'] == 'queued'
    monkeypatch.setattr('csv_to_clonehero.server.catalog_browse.load_enchor_catalog', lambda _: SimpleNamespace(available=False))
    app.extensions['jobs'].catalog_browser.prepare()
    assert client.get('/api/catalog').status_code == 503
    assert not app.extensions['jobs'].catalog_browser.pending()


def test_catalog_add_remove_multi_undo_and_download_before_any_music_import(client, app, config):
    _seed(client, app, config, count=2)
    browse = client.get('/api/catalog').get_json()
    assert browse['exact_generation_id'] == 0
    first = _pick(client, browse['items'][0], browse['snapshot_token']).get_json()
    second = _pick(client, browse['items'][1], browse['snapshot_token']).get_json()
    assert second['summary']['catalog'] == second['catalog_summary'] == {'songs': 2, 'charts': 2}
    assert second['undo']['available']
    def undo(receipt):
        return client.post('/api/selection/undo', json={'operation_id': receipt['undo']['operation_id'],
            'selection_revision': receipt['selection_revision']})
    restored = undo(second).get_json()
    assert restored['charts'] == 1 and restored['undo']['operation_id'] == first['undo']['operation_id']
    assert undo(restored).get_json()['charts'] == 0
    selected = _pick(client, browse['items'][0], browse['snapshot_token']).get_json()
    assert client.get('/api/journey').get_json()['catalog_summary']['charts'] == 1
    assert client.get('/api/catalog/selections').get_json()['items'][0]['selected']
    db = Database(config.database_path)
    workspace = db.get_workspace(Database.workspace_key_for_user('user-1'))['id']
    assert not db.get_workspace_library_track_ids(workspace)
    assert selection_snapshot(db, workspace)['generation_id'] == 0
    assert _validated_selections(db, workspace, config=config)[0]['result_tier'] == 'CATALOG_CHOICE'
    db.close()
    assert client.post('/api/download', json={}).status_code == 202


def test_catalog_noop_preserves_existing_custom_library_choice_and_artist_expansion(client, app, config):
    rows = _seed(client, app, config, count=2)
    from tests.conftest import make_track
    db = Database(config.database_path)
    workspace = client.get('/api/journey').get_json()['workspace_id']
    track = db.upsert_spotify_tracks([make_track(rows[0].name, rows[0].artist)])[0]
    db.replace_workspace_library_tracks(workspace, [track])
    generation = db.create_discovery_scan(workspace, scan_key='catalog-test', kind='exact', status='complete')
    task = db.upsert_discovery_task(workspace, scan_id=generation, source='enchor', query_key='song', status='complete')
    candidate_id = db.upsert_workspace_candidate(workspace, track.track_key, rows[0], score=99, eligibility='RECOMMENDED', task_id=task)
    db.select_workspace_candidate(workspace, track.track_key, rows[0].db_id, force=True, metadata={'custom': 'preserve me'})
    before = db._public_selection_rows(workspace)
    db.close()
    browse = client.get('/api/catalog').get_json()
    repeated = _pick(client, browse['items'][0], browse['snapshot_token']).get_json()
    assert repeated['changed'] == 0 and repeated['already_selected'] and repeated['selected_in_scope'] == 'exact'
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()
    added = _pick(client, browse['items'][1], browse['snapshot_token']).get_json()
    assert added['summary']['catalog']['charts'] == 1
    db = Database(config.database_path)
    db.clear_workspace_expansion_candidates(workspace)
    assert db.public_selection_receipt(workspace)['catalog_summary']['charts'] == 1
    db.close()


def test_catalog_alias_clear_removes_owned_package_undo_restores_and_canonical_clear_works_unavailable(client, app, config):
    rows = _seed(client, app, config, count=2)
    rows[1].md5 = rows[0].md5
    write_snapshot(config.catalog_path, rows)
    browser = app.extensions['jobs'].catalog_browser
    browser.request();browser.prepare()
    browse = client.get('/api/catalog').get_json()
    added = _pick(client, browse['items'][0], browse['snapshot_token']).get_json()
    canonical = added['selected']['catalog'][0]
    assert canonical['catalog_key'] == 'enchor:1'
    assert canonical['source'] == 'enchor' and canonical['chart_id'] == 1 and canonical['md5'] == rows[0].md5.lower()
    alias = client.get('/api/catalog/enchor/2').get_json()['item']
    assert alias['selected'] and alias['selected_in_scope'] == 'catalog'
    assert alias['selected_catalog_key'] == 'enchor:1'
    repeated = _pick(client, alias, browse['snapshot_token']).get_json()
    assert repeated['changed'] == 0 and repeated['selection_revision'] == added['selection_revision']
    assert repeated['undo']['operation_id'] == added['undo']['operation_id']

    db = Database(config.database_path)
    other = db.upsert_user(spotify_user_id='other')
    signed = db.create_session(int(other['id']), expires_at=9999999999)
    db.close()
    other_client = app.test_client()
    with other_client.session_transaction() as value:
        value['sid'] = signed['session_id']
    _pick(other_client, browse['items'][0], browse['snapshot_token'])

    removed = _pick(client, alias, browse['snapshot_token'], action='clear').get_json()
    assert removed['changed'] == 1 and removed['catalog_summary']['charts'] == 0
    assert other_client.get('/api/catalog/selections').get_json()['total'] == 1
    restored = client.post('/api/selection/undo', json={
        'operation_id': removed['undo']['operation_id'], 'selection_revision': removed['selection_revision']})
    assert restored.status_code == 200
    assert restored.get_json()['selected']['catalog'][0]['catalog_key'] == 'enchor:1'
    assert client.get('/api/catalog/enchor/2').get_json()['item']['selected_catalog_key'] == 'enchor:1'

    config.catalog_path.unlink()
    browser.request();browser.prepare()
    assert client.get('/api/catalog').status_code == 503
    removed = _pick(client, {'catalog_key': alias['selected_catalog_key']}, None, action='clear').get_json()
    assert removed['changed'] == 1 and removed['catalog_summary']['charts'] == 0
    assert other_client.get('/api/catalog/selections').get_json()['total'] == 1


@pytest.mark.parametrize('kind', ['exact', 'expansion'])
def test_catalog_alias_clear_never_removes_library_or_expansion_choices(client, app, config, kind):
    rows = _seed(client, app, config, count=2)
    rows[1].md5 = rows[0].md5
    write_snapshot(config.catalog_path, rows)
    app.extensions['jobs'].catalog_browser.request();app.extensions['jobs'].catalog_browser.prepare()
    from tests.conftest import make_track
    db = Database(config.database_path)
    workspace = client.get('/api/journey').get_json()['workspace_id']
    track = db.upsert_spotify_tracks([make_track(rows[0].name, rows[0].artist)])[0]
    db.replace_workspace_library_tracks(workspace, [track])
    generation = db.create_discovery_scan(workspace, scan_key='catalog-alias-test', kind='exact', status='complete')
    task = db.upsert_discovery_task(workspace, scan_id=generation, source='enchor', query_key='song', status='complete')
    db.upsert_workspace_candidate(workspace, track.track_key if kind == 'exact' else None, rows[0],
        kind=kind, artist_norm=rows[0].artist_norm, score=99, eligibility='RECOMMENDED', task_id=task)
    db.select_workspace_candidate(workspace, track.track_key if kind == 'exact' else None, rows[0].db_id,
        kind=kind, artist_norm=rows[0].artist_norm, force=True, metadata={'custom': 'preserve me'})
    before = db._public_selection_rows(workspace)
    db.close()
    detail = client.get('/api/catalog/enchor/2').get_json()
    alias = detail['item']
    assert alias['selected'] and alias['selected_in_scope'] == kind and alias['selected_catalog_key'] is None
    result = _pick(client, alias, detail['snapshot_token'], action='clear').get_json()
    assert result['changed'] == 0 and result['charts'] == 1
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()


@pytest.mark.parametrize('block', ['stale_revision', 'snapshot', 'busy', 'artifact', 'modchart'])
def test_catalog_add_rejects_stale_busy_and_hardblocked_choices(client, app, config, block):
    rows = _seed(client, app, config, count=1)
    browse = client.get('/api/catalog').get_json()
    db = Database(config.database_path)
    user = db.upsert_user(spotify_user_id='user-1')
    if block == 'busy':
        db.create_job(int(user['id']), 'download', workspace_key=Database.workspace_key_for_user('user-1'))
    if block == 'artifact':
        db.record_chart_artifact_validation('enchor', 1, artifact_ref=rows[0].md5, state='REJECTED')
    db.close()
    if block == 'modchart':
        rows[0].modchart = True
        write_snapshot(config.catalog_path, rows)
        app.extensions['jobs'].catalog_browser.request()
        app.extensions['jobs'].catalog_browser.prepare()
        browse = client.get('/api/catalog').get_json()
    result = _pick(client, browse['items'][0], 'stale' if block == 'snapshot' else browse['snapshot_token'], revision=-1 if block == 'stale_revision' else None)
    assert result.status_code == (400 if block in {'artifact', 'modchart'} else 409), result.get_json()
    assert client.get('/api/selection/history').get_json()['charts'] == 0


def test_catalog_export_uses_real_note_validation_and_rechecks_snapshot_rows(client, app, config, tmp_path):
    rows = _seed(client, app, config, count=1)
    payload = valid_sng_bytes('Song 0000')
    rows[0].md5 = hashlib.md5(payload).hexdigest()
    write_snapshot(config.catalog_path, rows)
    app.extensions['jobs'].catalog_browser.request();app.extensions['jobs'].catalog_browser.prepare()
    browse = client.get('/api/catalog').get_json()
    _pick(client, browse['items'][0], browse['snapshot_token'])
    db = Database(config.database_path)
    workspace = db.get_workspace(Database.workspace_key_for_user('user-1'))['id']
    source = FakeSource(files={rows[0].md5: payload});source.name = 'enchor'
    archive, summary = build_archive(config, db, workspace, [source], tmp_path/'d')
    assert archive.is_file() and summary['published'] == 1
    rows[0].md5 = 'f' * 32
    write_snapshot(config.catalog_path, rows)
    with pytest.raises(Exception, match='No downloadable'):
        _validated_selections(db, workspace, config=config)
    db.close()


def test_catalog_owner_isolation_and_rejected_artifact_cannot_return_through_undo(client, app, config):
    _seed(client, app, config, count=1)
    browse = client.get('/api/catalog').get_json()
    added = _pick(client, browse['items'][0], browse['snapshot_token']).get_json()
    db = Database(config.database_path)
    other = db.upsert_user(spotify_user_id='other')
    session = db.create_session(int(other['id']), expires_at=9999999999)
    db.close()
    other_client = app.test_client()
    with other_client.session_transaction() as value:
        value['sid'] = session['session_id']
    assert other_client.get('/api/catalog/selections').get_json()['items'] == []
    assert other_client.get('/api/catalog').get_json()['items'][0]['selected'] is False
    assert other_client.post('/api/selection/undo', json={'operation_id': added['undo']['operation_id'],
        'selection_revision': added['selection_revision']}).status_code == 409
    removed = _pick(client, browse['items'][0], browse['snapshot_token'], action='clear').get_json()
    db = Database(config.database_path)
    db.record_chart_artifact_validation('enchor', 1, state='REJECTED')
    db.close()
    assert client.post('/api/selection/undo', json={'operation_id': removed['undo']['operation_id'],
        'selection_revision': removed['selection_revision']}).status_code == 409
    assert client.get('/api/catalog/selections').get_json()['items'] == []


def test_catalog_mutation_enforces_same_origin_request_header(client, app, config):
    _seed(client, app, config, count=1)
    browse = client.get('/api/catalog').get_json()
    payload = {'catalog_key': browse['items'][0]['catalog_key'], 'snapshot_token': browse['snapshot_token'],
               'selection_revision': browse['selection_revision'], 'exact_generation_id': 0, 'action': 'select'}
    config.server.enforce_secret_key = True
    config.server.base_url = 'https://riff.example'
    assert client.post('/api/catalog/selection', json=payload).status_code == 403
    assert client.post('/api/catalog/selection', json=payload,
        headers={'X-Riff-Request': '1', 'Origin': 'https://another.example'}).status_code == 403
    assert client.post('/api/catalog/selection', json=payload,
        headers={'X-Riff-Request': '1', 'Origin': config.public_base_url}).status_code == 200


@pytest.mark.parametrize('path', ['/about', '/privacy', '/terms', '/dmca'])
def test_information_pages_preserve_optional_session_owner_without_exposing_credentials(client, path):
    guest = client.get(path)
    assert guest.status_code == 200
    assert 'data-authenticated="false"' in guest.get_data(as_text=True)
    assert 'data-riff-owner="guest"' in guest.get_data(as_text=True)
    _sign_in(client)
    signed_in = client.get(path).get_data(as_text=True)
    assert 'data-authenticated="true"' in signed_in
    assert 'data-riff-owner="user-1"' in signed_in
    assert 'encrypted_refresh_token' not in signed_in


def test_catalog_index_has_no_150000_entry_cap(config, monkeypatch):
    from csv_to_clonehero.server.catalog_browse import CatalogBrowser
    entries = tuple(SimpleNamespace(source='enchor',chart_id=index,name=f'Song {index}',artist='Artist',charter='',album='') for index in range(150123))
    monkeypatch.setattr('csv_to_clonehero.server.catalog_browse.load_enchor_catalog', lambda _: SimpleNamespace(available=True,sha256='a'*64,charts=lambda:entries))
    browser = CatalogBrowser(config)
    browser.request();browser.prepare()
    assert browser.readiness()['total_charts'] == browser.readiness()['total_songs'] == 150123
    page, total = browser.page(offset=150100, limit=100, q='', order='source', seed='0')
    assert total == 150123 and len(page) == 23 and page[-1].chart_id == 150122
