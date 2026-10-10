"""Owner-scoped restoration, stale-write rejection and observable cold readiness."""
import json
import os

import pytest

from csv_to_clonehero.database import Database, SCHEMA_VERSION
from csv_to_clonehero.server import safeguards
from tests.test_server_web import app, client, config, spotify_client_id, _sign_in
from tests.test_studio_api import _seed
from tests.test_public_yield_api import _seed_catalog, _wait_job


def _undo(client, receipt):
    return client.post('/api/selection/undo', json={
        'operation_id': receipt['undo']['operation_id'],
        'selection_revision': receipt['selection_revision'],
    })


def test_clear_undo_restores_custom_choices_and_metadata_and_is_single_use(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    db = Database(config.database_path)
    db.select_workspace_candidate(workspace, tracks[0].track_key, ids[1], force=True,
                                  metadata={'custom': 'original alternate choice'})
    db.select_workspace_candidate(workspace, None, extra, kind='expansion', artist_norm='artist', force=True)
    before = db._public_selection_rows(workspace)
    db.close()
    cleared = client.post('/api/selection/bulk', json={'action': 'clear'}).get_json()
    assert cleared['summary']['library']['charts'] == 0
    assert cleared['summary']['expansion']['charts'] == 1
    assert cleared['undo']['available']
    restored = _undo(client, cleared)
    assert restored.status_code == 200
    assert restored.get_json()['summary']['library']['charts'] == 1
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()
    assert _undo(client, cleared).status_code == 409
    assert client.get('/api/selection/history').get_json()['undo']['available'] is False


def test_multi_operation_undo_and_noop_preserve_last_restore_point(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    token = client.get('/api/listening-deck').get_json()['generation_token']
    first = client.post('/api/selection', json={'track_key': tracks[0].track_key, 'candidate_id': ids[0]}).get_json()
    assert client.get('/api/listening-deck').get_json()['generation_token'] == token
    second = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    noop = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    assert noop['changed'] == 0
    assert noop['undo']['operation_id'] == second['undo']['operation_id']
    restored = _undo(client, noop).get_json()
    assert restored['charts'] == 1
    assert restored['undo']['operation_id'] == first['undo']['operation_id']
    assert _undo(client, restored).get_json()['charts'] == 0


def test_oversized_selection_changes_complete_with_honest_preview_and_resume_normal_undo(client, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    saved = client.post('/api/selection/all', json={'scope': 'library', 'policy': 'all_versions'}).get_json()
    assert saved['charts'] == 4
    monkeypatch.setattr('csv_to_clonehero.database.PUBLIC_RESTORE_POINT_BYTES', 64)
    preview = client.get('/api/selection/all-preview?scope=library').get_json()
    assert preview['undo_supported'] is False
    removed = client.post('/api/selection', json={'track_key': tracks[0].track_key, 'candidate_id': ids[0], 'action': 'clear'})
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()['charts'] == 3
    assert not removed.get_json()['undo']['available']
    assert 'size limit' in removed.get_json()['undo']['reason']
    assert _undo(client, saved).status_code == 409
    cleared = client.post('/api/selection/bulk', json={'action': 'clear'})
    assert cleared.status_code == 200 and cleared.get_json()['charts'] == 0
    assert client.get('/api/selection/all-preview?scope=library').get_json()['undo_supported']
    added = client.post('/api/selection', json={'track_key': tracks[0].track_key, 'candidate_id': ids[0]}).get_json()
    assert added['undo']['available'] and not added['undo']['reason']
    assert _undo(client, added).get_json()['charts'] == 0
    monkeypatch.setattr(Database, '_bounded_public_restore_json', staticmethod(lambda _: pytest.fail('routine read serialized a restore snapshot')))
    assert client.get('/api/workspace').status_code == 200
    assert client.get('/api/journey').status_code == 200


def test_oversized_selection_noop_preserves_metadata_and_existing_real_undo(client, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    saved = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    db = Database(config.database_path)
    before = db._public_selection_rows(workspace)
    db.close()
    monkeypatch.setattr('csv_to_clonehero.database.PUBLIC_RESTORE_POINT_BYTES', 64)
    repeated = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    assert repeated['changed'] == 0
    assert repeated['undo']['operation_id'] == saved['undo']['operation_id']
    assert repeated['undo']['available'] and not repeated['undo']['reason']
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()
    assert _undo(client, repeated).get_json()['charts'] == 0


def test_history_bound_and_workspace_revisions_are_independent(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    for index in range(24):
        response = client.post('/api/selection', json={'track_key': tracks[0].track_key,
            'candidate_id': ids[0], 'action': 'select' if index % 2 == 0 else 'clear'})
        assert response.status_code == 200
    data = client.get('/api/selection/history').get_json()
    assert len(data['operations']) == 10
    db = Database(config.database_path)
    other = db.create_workspace(workspace_key='spotify:u:someone-else')
    assert db.public_selection_revision(other) == 0
    db.close()


def test_other_owner_reusing_deleted_selection_ids_cannot_block_or_change_undo(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    client.post('/api/selection/all', json={'scope': 'library', 'policy': 'all_versions'})
    db = Database(config.database_path)
    before = db._public_selection_rows(workspace)
    db.close()
    cleared = client.post('/api/selection/bulk', json={'action': 'clear'}).get_json()
    db = Database(config.database_path)
    other = db.create_workspace(workspace_key=Database.workspace_key_for_user('other'))
    chart = db.get_candidate(ids[0])
    db.upsert_workspace_candidate(other, tracks[0].track_key, chart, score=99, eligibility='RECOMMENDED')
    db.select_workspace_candidate(other, tracks[0].track_key, ids[0], force=True, metadata={'owner': 'other'})
    other_before = db._public_selection_rows(other)
    assert other_before[0]['id'] in {row['id'] for row in before}
    db.close()
    assert _undo(client, cleared).status_code == 200
    db = Database(config.database_path)
    restored = db._public_selection_rows(workspace)
    assert db._public_selection_rows(other) == other_before
    without_ids = lambda rows: sorted((json.dumps({key: value for key, value in row.items() if key != 'id'}, sort_keys=True) for row in rows))
    assert without_ids(restored) == without_ids(before)
    assert db.public_selection_revision(other) == 1
    db.close()


@pytest.mark.parametrize('kind', ['discovery', 'expansion', 'download'])
def test_undo_and_mutations_reject_active_jobs_without_changing_picks(client, app, config, kind):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    saved = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    app.extensions['jobs'].shutdown()
    db = Database(config.database_path)
    user = db.upsert_user(spotify_user_id='user-1')
    db.create_job(int(user['id']), kind, workspace_key=Database.workspace_key_for_user('user-1'))
    before = db._public_selection_rows(workspace)
    db.close()
    assert _undo(client, saved).status_code == 409
    assert client.post('/api/selection/bulk', json={'action': 'clear'}).status_code == 409
    db = Database(config.database_path)
    assert db._public_selection_rows(workspace) == before
    db.close()


def test_stale_revision_generation_and_cross_owner_undo_fail_closed(client, app, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    saved = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    assert client.post('/api/selection/bulk', json={'action': 'clear', 'selection_revision': saved['selection_revision'] - 1}).status_code == 409
    assert client.post('/api/selection/all', json={'scope': 'library', 'exact_generation_id': generation + 99}).status_code == 409
    db = Database(config.database_path)
    other = db.upsert_user(spotify_user_id='other', display_name='Other')
    session = db.create_session(int(other['id']), expires_at=9999999999)
    db.close()
    second_client = app.test_client()
    with second_client.session_transaction() as value:
        value['sid'] = session['session_id']
    assert _undo(second_client, saved).status_code == 409
    db = Database(config.database_path)
    db.create_discovery_scan(workspace, scan_key='new', kind='exact', status='complete')
    db.close()
    assert _undo(client, saved).status_code == 409


def test_untracked_selection_change_and_rejected_artifact_cannot_be_undone(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    client.post('/api/selection', json={'track_key': tracks[0].track_key, 'candidate_id': ids[0]})
    cleared = client.post('/api/selection/bulk', json={'action': 'clear'}).get_json()
    db = Database(config.database_path)
    chart = db.get_candidate(ids[0])
    db.record_chart_artifact_validation(chart.source, chart.chart_id, artifact_ref=chart.md5,
        state='REJECTED', validator_version='current', reason='fixture invalid notes')
    db.close()
    assert _undo(client, cleared).status_code == 409
    assert client.get('/api/selection/history').get_json()['charts'] == 0


def test_nested_helpers_roll_back_selection_and_meta_together(config):
    db = Database(config.database_path)
    workspace = db.create_workspace(workspace_key='local-test')
    db.set_meta('fixture', 'old')
    with pytest.raises(RuntimeError):
        with db.atomic_workspace_selection():
            db.set_meta('fixture', 'new')
            db.clear_workspace_selection(workspace)
            raise RuntimeError('rollback')
    assert db.get_meta('fixture') == 'old'
    assert db._conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_preview_is_read_only_matches_bulk_scope_and_counts_without_provider_calls(client, app, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=121)
    app.extensions['jobs'].source_factory = lambda _: pytest.fail('preview must not query providers')
    before = client.get('/api/selection/history').get_json()
    preview = client.get('/api/selection/all-preview?scope=both&policy=all_versions&include_possible=false').get_json()
    assert preview['operation'] == 'replace'
    assert preview['summary'] == {'library': {'songs': 121, 'charts': 242}, 'expansion': {'songs': 0, 'charts': 0}}
    assert preview['possible_charts'] == 1
    assert preview['selection_revision'] == before['selection_revision']
    saved = client.post('/api/selection/all', json={'scope': 'both', 'policy': 'all_versions', 'confirm_possible': False}).get_json()
    assert saved['charts'] == preview['charts']
    assert saved['undo']['available']


def test_cohort_presets_preserve_other_customized_picks_and_restore_entire_previous_state(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    client.post('/api/selection', json={'track_key': tracks[0].track_key, 'candidate_id': ids[1]})
    client.post('/api/selection', json={'track_key': tracks[1].track_key, 'candidate_id': ids[3]})
    before = client.get('/api/selection/history').get_json()
    chosen = client.post('/api/selection/bulk', json={'preset': 'best_matches', 'track_ids': [tracks[0].track_key]}).get_json()
    assert chosen['charts'] == 2
    assert {'track_key': tracks[1].track_key, 'candidate_id': ids[3]} in chosen['selected']['library']
    restored = _undo(client, chosen).get_json()
    assert restored['selected'] == before['selected']


def test_cold_preflight_is_queued_observable_and_never_parsed_on_request_thread(client, app, config, monkeypatch):
    _sign_in(client)
    jobs = app.extensions['jobs']
    jobs.shutdown()
    config.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    config.catalog_path.write_text('fixture catalog', encoding='utf-8')
    monkeypatch.setattr(safeguards, 'load_enchor_catalog', lambda *args, **kwargs: pytest.fail('request parsed a cold catalog'))
    status = client.get('/api/preflight').get_json()
    assert status['preflight']['status'] == 'check_required'
    response = client.post('/api/preflight')
    assert response.status_code == 202
    job = response.get_json()['job']
    assert job['kind'] == 'preflight'
    assert client.post('/api/preflight').get_json()['job']['id'] == job['id']
    assert client.get('/api/preflight').get_json()['preflight']['busy']
    ready = {'available': True, 'fresh': True, 'usable': True, 'status': 'ready',
             'busy': False, 'mode': 'catalog', 'catalog_available': True, 'message': 'Ready'}
    monkeypatch.setattr(safeguards, 'refresh_enchor_preflight', lambda *args: ready)
    jobs._run_job(jobs.get(job['id']))
    assert client.get(f"/api/jobs/{job['id']}").get_json()['job']['result']['preflight']['usable']
    assert client.get('/api/preflight').get_json()['preflight']['usable']
    assert client.post('/api/preflight').status_code == 200
    config.catalog_path.write_text('changed fixture catalog', encoding='utf-8')
    assert client.get('/api/preflight').get_json()['preflight']['status'] == 'check_required'


def _replace_expansion_fixture(client, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, artists=2, songs=2)
    client.get('/api/expansion/artists')
    initial = client.post('/api/expansion', json={'artists': ['artist 0'], 'coverage_mode': 'all'})
    _wait_job(client, initial.get_json()['job']['id'])
    picked = client.post('/api/selection/all', json={'scope': 'expansion', 'confirm_possible': True, 'policy': 'all_versions'})
    assert picked.status_code == 200, picked.get_json()
    assert picked.get_json()['summary']['expansion']['charts'] == 2
    db = Database(config.database_path)
    db._conn.execute("UPDATE workspace_selections SET metadata_json=? WHERE workspace_id=?", (json.dumps({'custom': 'all original versions', 'possible_confirmed': True}), workspace))
    db._conn.execute("UPDATE workspace_expansion_artists SET metadata_json=? WHERE workspace_id=? AND artist_norm='artist 1'", (json.dumps({'exact_generation_id': generation, 'custom': 'unchecked artist note'}), workspace))
    db._conn.commit()
    before = db._public_expansion_snapshot(workspace)
    db.close()
    start = client.post('/api/expansion', json={'artists': ['artist 1'], 'coverage_mode': 'all'})
    assert start.status_code == 202
    terminal = _wait_job(client, start.get_json()['job']['id'])
    assert terminal['result']['undo']['available']
    return workspace, generation, before, terminal['result']


def test_expansion_replacement_undo_restores_all_prior_ids_picks_and_unchecked_artist_metadata(client, config, monkeypatch):
    workspace, generation, before, receipt = _replace_expansion_fixture(client, config, monkeypatch)
    restored = _undo(client, receipt)
    assert restored.status_code == 200, restored.get_json()
    db = Database(config.database_path)
    assert db._public_expansion_snapshot(workspace) == before
    assert db.get_workspace_candidates(workspace, kind='expansion')[0]['artist_norm'] == 'artist 0'
    db.close()
    assert _undo(client, receipt).status_code == 409
    # Reading the artist list must retain customized artist metadata as well.
    client.get('/api/expansion/artists')
    db = Database(config.database_path)
    unchecked = next(row for row in db.get_workspace_expansion_artists(workspace) if row['artist_norm'] == 'artist 1')
    assert not unchecked['selected']
    assert json.loads(unchecked['metadata_json'])['custom'] == 'unchecked artist note'
    db.close()


@pytest.mark.parametrize('block', ['generation', 'active_job', 'artifact', 'metadata'])
def test_expansion_restore_rechecks_generation_jobs_artifacts_and_current_chart_metadata(client, app, config, monkeypatch, block):
    workspace, generation, before, receipt = _replace_expansion_fixture(client, config, monkeypatch)
    app.extensions['jobs'].shutdown()
    db = Database(config.database_path)
    if block == 'generation':
        db.create_discovery_scan(workspace, scan_key='newer', kind='exact', status='complete')
    elif block == 'active_job':
        user = db.upsert_user(spotify_user_id='user-1')
        db.create_job(int(user['id']), 'expansion', workspace_key=Database.workspace_key_for_user('user-1'))
    else:
        chart = db.get_candidate(before['candidates'][0]['candidate_id'])
        if block == 'artifact':
            db.record_chart_artifact_validation(chart.source, chart.chart_id, artifact_ref=chart.md5,
                state='REJECTED', validator_version='current', reason='new artifact inspection rejected this chart')
        else:
            chart.modchart = True
            db.upsert_candidate(chart)
    current = db._public_expansion_snapshot(workspace)
    db.close()
    assert _undo(client, receipt).status_code == 409
    db = Database(config.database_path)
    assert db._public_expansion_snapshot(workspace) == current
    db.close()


def test_oversized_expansion_snapshot_keeps_working_and_reports_restore_unavailable(client, config, monkeypatch):
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, artists=2, songs=1)
    client.get('/api/expansion/artists')
    monkeypatch.setattr('csv_to_clonehero.database.PUBLIC_RESTORE_POINT_BYTES', 1)
    response = client.post('/api/expansion', json={'artists': ['artist 1'], 'coverage_mode': 'all'})
    terminal = _wait_job(client, response.get_json()['job']['id'])
    assert not terminal['result']['undo']['available']
    assert 'size limit' in terminal['result']['undo']['reason']
    assert client.get('/api/expansion/results').get_json()['total'] == 1


def test_interrupted_expansion_restore_point_survives_reopen_and_noop_retains_previous_operation(client, config, monkeypatch):
    workspace, generation, before, receipt = _replace_expansion_fixture(client, config, monkeypatch)
    db = Database(config.database_path)
    with db.atomic_workspace_selection():
        pending = db.begin_public_expansion_change(workspace, generation)
    with db.atomic_workspace_selection():
        db.finish_public_expansion_change(workspace, generation, pending)
    assert db.public_selection_receipt(workspace)['undo']['operation_id'] == receipt['undo']['operation_id']
    current = db._public_expansion_snapshot(workspace)
    with db.atomic_workspace_selection():
        db.begin_public_expansion_change(workspace, generation)
    db.clear_workspace_expansion_candidates(workspace)
    db.close()
    db = Database(config.database_path)
    db.recover_public_expansion_changes()
    recovered = db.public_selection_receipt(workspace)
    assert recovered['undo']['available']
    db.close()
    assert _undo(client, recovered).status_code == 200
    db = Database(config.database_path)
    assert db._public_expansion_snapshot(workspace) == current
    assert db.public_selection_receipt(workspace)['undo']['operation_id'] == receipt['undo']['operation_id']
    db.close()


def test_real_identical_expansion_rerun_preserves_latest_library_restore_point(client, config, monkeypatch):
    from tests.conftest import make_chart
    workspace, generation, charts, catalog = _seed_catalog(client, config, monkeypatch, artists=1, songs=1)
    client.get('/api/expansion/artists')
    started = client.post('/api/expansion', json={'artists': ['artist 0'], 'coverage_mode': 'all'})
    _wait_job(client, started.get_json()['job']['id'])
    db = Database(config.database_path)
    track = db.get_library_tracks(workspace_id=workspace)[0]
    task = db.upsert_discovery_task(workspace, scan_id=generation, source='test', query_key='known', status='complete')
    db.upsert_workspace_candidate(workspace, track.track_key, make_chart('Known', 'Artist 0', chart_id=99001), score=99,
                                  eligibility='RECOMMENDED', decision='AUTO_ACCEPT', task_id=task)
    db.close()
    receipt = client.post('/api/selection/all', json={'scope': 'library'}).get_json()
    assert receipt['undo']['available']
    repeated = client.post('/api/expansion', json={'artists': ['artist 0'], 'coverage_mode': 'all'})
    terminal = _wait_job(client, repeated.get_json()['job']['id'])
    assert terminal['result']['undo']['operation_id'] == receipt['undo']['operation_id']
    assert _undo(client, terminal['result']).get_json()['charts'] == 0


def test_cached_readiness_rechecks_equal_stat_replacement_and_bounded_malformed_header(client, app, config):
    from csv_to_clonehero.catalog import MAX_CATALOG_HEADER_BYTES
    from tests.test_catalog import candidate, write_snapshot

    _sign_in(client)
    jobs = app.extensions['jobs']
    jobs.shutdown()
    config.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    write_snapshot(config.catalog_path, [candidate(1, 'Artist', 'Song')])
    stat = config.catalog_path.stat()
    first = client.post('/api/preflight').get_json()['job']
    jobs._run_job(jobs.get(first['id']))
    assert client.get('/api/preflight').get_json()['preflight']['usable']
    write_snapshot(config.catalog_path, [candidate(2, 'Artist', 'Tune')])
    assert config.catalog_path.stat().st_size == stat.st_size
    os.utime(config.catalog_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert client.get('/api/preflight').get_json()['preflight']['status'] == 'check_required'
    second = client.post('/api/preflight').get_json()['job']
    jobs._run_job(jobs.get(second['id']))
    assert client.get('/api/preflight').get_json()['preflight']['usable']
    config.catalog_path.write_bytes(b'x' * (MAX_CATALOG_HEADER_BYTES + 1))
    assert client.get('/api/preflight').get_json()['preflight']['status'] == 'check_required'
