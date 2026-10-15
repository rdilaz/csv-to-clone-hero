"""Catalog date filters preserve authoritative counts, pagination, and guards."""
import pytest

from tests.conftest import make_chart
from tests.test_catalog import write_snapshot
from tests.test_server_web import app, client, config, spotify_client_id


def seed_years(app, config):
    app.extensions['jobs'].shutdown()
    rows = []
    for index, year in enumerate(('1989', '1990', '1999-11-02', '2000', '', 'Unknown', '1995?')):
        row = make_chart(f'Record {index}', 'The Artist', chart_id=index + 1)
        row.source = 'enchor'
        row.year = year
        row.album = 'Collection'
        rows.append(row)
    config.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    write_snapshot(config.catalog_path, rows)
    app.extensions['jobs'].catalog_browser.request()
    app.extensions['jobs'].catalog_browser.prepare()


def test_decade_filters_metadata_without_replacing_full_catalog_counts(client, app, config):
    seed_years(app, config)
    first = client.get('/api/catalog?q=Collection&decade=1990&limit=2').get_json()
    second = client.get('/api/catalog?q=Collection&decade=1990&limit=2&offset=2').get_json()
    assert first['total_charts'] == first['total_songs'] == 7
    assert first['total_filtered_charts'] == second['total_filtered_charts'] == 3
    assert [x['parsed_year'] for x in first['items'] + second['items']] == [1990, 1999, 1995]
    assert first['has_more'] is True and second['has_more'] is False
    assert client.get('/api/catalog?decade=1980').get_json()['total_filtered_charts'] == 1
    assert client.get('/api/catalog').get_json()['total_filtered_charts'] == 7


def test_filtered_shuffle_is_repeatable_and_snapshot_guard_remains_active(client, app, config):
    seed_years(app, config)
    url = '/api/catalog?decade=1990&order=shuffle&seed=release-check'
    page = client.get(url).get_json()
    assert page['items'] == client.get(url).get_json()['items']
    assert len(page['items']) == 3
    assert client.get(url + '&snapshot_token=stale').status_code == 409
    assert client.get('/api/selection/history').status_code == 401


@pytest.mark.parametrize('value', ('1995', 'unknown', 'NaN', '1790', '2200'))
def test_invalid_decade_is_rejected_before_catalog_work(client, value):
    assert client.get('/api/catalog?decade=' + value).status_code == 400
