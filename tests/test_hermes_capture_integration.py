import json

import pytest

from session_search.capture.local import capture_providers
from session_search.capture.queue import UploadQueue
from session_search.capture.sync import sync_once
from session_search.core.records import Event, SearchQuery, SessionRevision
from session_search.interfaces.capture_status import capture_status
from session_search.interfaces.cli import main
from session_search.storage.catalog import Catalog


@pytest.fixture
def capture_setup(tmp_path, monkeypatch):
    from session_search.capture import hermes

    config = tmp_path / "providers.toml"
    config.write_text("[providers.hermes]\nsearch_by_default = true\n")
    calls = []
    snapshots = [SessionRevision("hermes:test", (Event("e", "user", "first alpha"),),
                                 source="hermes", parser_version="hermes-v1")]

    def reader(path, *, producer, profile, on_deferred, is_known):
        calls.append((path, producer, profile))
        yield from snapshots

    monkeypatch.setattr(hermes, "iter_hermes_revisions", reader)
    return config, calls, snapshots


def test_default_capture_explicit_selection_and_sync_status(tmp_path, capture_setup, monkeypatch):
    config, calls, _ = capture_setup
    root = tmp_path / "catalog"
    db = tmp_path / "state.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db.touch()
    with Catalog(root) as catalog:
        assert capture_providers(catalog, tmp_path, "device")["providers"]["hermes"]["counts"] == {
            "captured": 1,
        }
        assert calls.pop() == (db.resolve(), "device", "default")
    result = sync_once(root, tmp_path, "device", providers_config=config,
                       hermes_dbs=[f"default={db}"], max_bytes=10000, reserve_bytes=0,
                       archive_raw=True, chunk_raw=True)
    assert calls == [(db.resolve(), "device", "default")]
    assert result["capture"]["providers"]["hermes"]["raw_archival_enabled"] is False
    assert capture_status(root)["providers"]["hermes"]["counts"] == {"unchanged": 1}
    with Catalog(root) as catalog:
        assert catalog.search(SearchQuery("alpha"))["results"]
        assert catalog.db.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0] == 0


def test_explicit_opt_out_never_reads_selected_hermes_db(tmp_path, capture_setup):
    config, calls, _ = capture_setup
    config.write_text("[providers.hermes]\ncapture_enabled = false\n")
    with Catalog(tmp_path / "catalog") as catalog:
        result = capture_providers(catalog, tmp_path, "device", providers_config=config,
                                   hermes_dbs=[f"default={tmp_path / 'state.db'}"])
    assert result["providers"]["hermes"]["status"] == "disabled"
    assert not calls


def test_discovery_stays_in_selected_home_and_explicit_inputs_replace_it(tmp_path, capture_setup,
                                                                  monkeypatch):
    _, calls, _ = capture_setup
    gym = tmp_path / "profiles" / "gym"
    gym.mkdir(parents=True)
    (gym / "state.db").touch()
    (tmp_path / "state.db").touch()
    monkeypatch.setenv("HERMES_HOME", str(gym))
    with Catalog(tmp_path / "catalog") as catalog:
        capture_providers(catalog, tmp_path, "device")
        assert calls == [(gym / "state.db", "device", "gym")]
        calls.clear()
        capture_providers(catalog, tmp_path, "device", hermes_dbs=[f"default={tmp_path / 'state.db'}"])
        assert calls == [(tmp_path / "state.db", "device", "default")]


def test_cli_budget_repeat_and_reactivate(tmp_path, capture_setup, capsys):
    config, _, snapshots = capture_setup
    root = tmp_path / "catalog"
    cmd = ["--data-dir", str(root), "--providers-config", str(config), "capture",
           "--producer", "device", "--hermes-db", f"default={tmp_path / 'state.db'}",
           "--reserve-bytes", "0", "--max-bytes"]

    def capture(budget):
        assert main([*cmd, str(budget)]) == 0
        return json.loads(capsys.readouterr().out)["providers"]["hermes"]["counts"]

    assert capture(1) == {"deferred": 1}
    assert capture(10000) == {"captured": 1}
    assert capture(10000) == {"unchanged": 1}
    first = snapshots[0]
    snapshots[0] = SessionRevision(first.session_id, (Event("e", "user", "edited beta"),),
                                   source="hermes", parser_version="hermes-v1")
    assert capture(10000) == {"captured": 1}
    snapshots[0] = first
    assert capture(10000) == {"captured": 1}
    with Catalog(root) as catalog:
        assert catalog.search(SearchQuery("alpha"))["results"]
        assert not catalog.search(SearchQuery("beta"))["results"]


def test_queue_keeps_each_head_transition_without_raw_upload(tmp_path, capture_setup):
    config, _, snapshots = capture_setup
    first = snapshots[0]
    second = SessionRevision(first.session_id, (), source="hermes", parser_version="hermes-v1")
    with UploadQueue(tmp_path / "client") as queue:
        for revision in (first, second, first, second):
            snapshots[0] = revision
            result = capture_providers(queue, tmp_path, "device", providers_config=config,
                                       hermes_dbs=[f"default={tmp_path / 'state.db'}"],
                                       max_bytes=10000, reserve_bytes=0, archive_raw=True)
            assert result["providers"]["hermes"]["counts"] == {"captured": 1}
        payloads = [json.loads(r[0]) for r in queue.db.execute("SELECT payload FROM pending ORDER BY seq")]
        assert len({p["request_id"] for p in payloads}) == 4
        assert [p["expected_revision"] for p in payloads] == [None, first.revision,
                                                              second.revision, first.revision]
        assert all("raw" not in p for p in payloads)
