"""Master list: artist -> album -> song derivation and master_list.csv."""

import csv
import json

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_scan, run_search
from csv_to_clonehero.reporting import generate_reports

from .conftest import FakeSource, make_chart, make_track


def _seed_tracks(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    (config.input_path / "lib.csv").write_text(
        "Track Name,Artist Name,Album\n"
        "Everlong,Foo Fighters,The Colour and the Shape\n"
        "Monkey Wrench,Foo Fighters,The Colour and the Shape\n"
        "Times Like These,Foo Fighters,One by One\n"
        "Loose Song,Foo Fighters,\n"
        "Humble,Kendrick Lamar,DAMN.\n",
        encoding="utf-8",
    )


def _scan(config, db):
    from csv_to_clonehero.overrides import Overrides

    return run_scan(config, db, Overrides())


def test_master_list_groups_by_artist_album_with_singles_bucket(config):
    _seed_tracks(config)
    db = Database(config.database_path)
    _scan(config, db)

    tree = db.get_master_list()
    assert set(tree) == {"Foo Fighters", "Kendrick Lamar"}
    foo = tree["Foo Fighters"]
    assert set(foo) == {"One by One", "Singles", "The Colour and the Shape"}
    assert [t.title for t in foo["The Colour and the Shape"]] == ["Everlong", "Monkey Wrench"]
    assert [t.title for t in foo["Singles"]] == ["Loose Song"]
    db.close()


def test_master_list_report_empty_match_status_before_search(config):
    _seed_tracks(config)
    db = Database(config.database_path)
    _scan(config, db)
    generate_reports(config, db)
    db.close()

    with open(config.reports_path / "master_list.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 5
    assert {row["match_status"] for row in rows} == {""}
    assert rows[0]["artist"] == "Foo Fighters"  # display-sorted


def test_master_list_report_match_statuses_after_search(config):
    _seed_tracks(config)
    db = Database(config.database_path)
    _scan(config, db)

    from csv_to_clonehero.overrides import Overrides

    source = FakeSource(
        song_results={
            ("Foo Fighters", "Everlong"): [
                make_chart("Everlong", "Foo Fighters", chart_id=1)
            ],
        }
    )
    run_search(config, db, [source], Overrides(), include_expansion=False)
    generate_reports(config, db)
    db.close()

    with open(config.reports_path / "master_list.csv", newline="", encoding="utf-8") as fh:
        rows = {row["title"]: row for row in csv.DictReader(fh)}
    everlong = rows["Everlong"]
    assert everlong["match_status"] == "auto"
    assert everlong["selected_source"] == "test"
    assert everlong["selected_chart_id"] == "1"
    assert everlong["album"] == "The Colour and the Shape"
    assert rows["Humble"]["match_status"] == "no match"
    assert rows["Humble"]["selected_source"] == ""

    summary_txt = (config.reports_path / "summary.txt").read_text(encoding="utf-8")
    assert "Master list:" in summary_txt
    assert "master_list.csv" in summary_txt
