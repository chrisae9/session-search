import copy
import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from session_search.interfaces.credentials import (
    device_entries, export_credentials, read_registry, receive_credentials,
    sync_credentials, update_device,
)
from session_search.interfaces.server import create_app
from session_search.storage.catalog import Catalog


def test_revocation_survives_delayed_updates_and_lost_acknowledgement(tmp_path):
    primary, replica = tmp_path / 'primary.json', tmp_path / 'replica.json'
    token = tmp_path / 'token'
    update_device(primary, 'device', token)
    replica.write_bytes(primary.read_bytes())
    with Catalog(tmp_path / 'data'):
        pass
    http = TestClient(create_app(tmp_path / 'data', replica, readonly=True))
    headers = {'Authorization': 'Bearer ' + token.read_text().strip()}
    first = export_credentials(primary)
    assert receive_credentials(replica, first)['epoch'] == 0
    assert http.get('/v1/status', headers=headers).status_code == 200
    update_device(primary, 'device')
    revoked = export_credentials(primary)
    assert revoked['epoch'] > first['epoch']
    receive_credentials(replica, revoked)
    assert http.get('/v1/status', headers=headers).status_code == 401
    with pytest.raises(ValueError, match='conflicts'):
        receive_credentials(replica, first)
    with pytest.raises(ValueError, match='authority'):
        update_device(replica, 'device', tmp_path / 'replacement-token')
    other = {**revoked, 'authority': '0' * 32}
    with pytest.raises(ValueError, match='conflicts'):
        receive_credentials(replica, other)
    same_epoch_changed = copy.deepcopy(revoked)
    same_epoch_changed['devices'] = first['devices']
    with pytest.raises(ValueError, match='conflicts'):
        receive_credentials(replica, same_epoch_changed)
    receipt = tmp_path / 'receipt.json'
    lose_ack = True

    def transport(command, *, input, **kwargs):
        nonlocal lose_ack
        # Only the hashed versioned registry travels over private SSH stdin.
        assert token.read_text().strip() not in input
        assert input not in ' '.join(command)
        result = receive_credentials(replica, json.loads(input))
        if lose_ack:
            lose_ack = False
            raise subprocess.TimeoutExpired('ssh', 45)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    def synchronize():
        return sync_credentials(primary, 'standby', str(replica), '/bin/session-search', receipt,
                                runner=transport)

    assert synchronize()['status'] == 'partial'
    assert http.get('/v1/status', headers=headers).status_code == 401
    result = synchronize()
    assert result['status'] == 'synchronized'
    assert result['acknowledged'] == result['desired']
    assert read_registry(replica)['replica'] is True
    assert replica.stat().st_mode & 0o777 == 0o600
    assert receipt.stat().st_mode & 0o777 == 0o600


def test_sync_reports_intervening_revocation_and_refuses_divergent_bootstrap(tmp_path):
    primary, replica = tmp_path / 'primary', tmp_path / 'replica'
    update_device(primary, 'a', tmp_path / 'token')
    replica.write_text('{}')
    with pytest.raises(ValueError, match='differs'):
        receive_credentials(replica, export_credentials(primary))
    replica.unlink()

    def transport(command, *, input, **kwargs):
        incoming = json.loads(input)
        result = receive_credentials(replica, incoming)
        update_device(primary, 'a')
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    receipt = tmp_path / 'receipt'
    result = sync_credentials(primary, 'standby', str(replica), '/bin/session-search', receipt,
                              runner=transport)
    assert result['status'] == 'behind'
    assert read_registry(primary)['epoch'] > result['acknowledged']['epoch']
    assert device_entries(read_registry(primary)) == {}
    with pytest.raises(ValueError, match='separate'):
        sync_credentials(primary, 'standby', str(replica), '/bin/session-search', primary)


def test_malformed_registry_fails_closed(tmp_path):
    registry = tmp_path / 'registry'
    registry.write_text('{"version":2}')
    http = TestClient(create_app(tmp_path / 'unused', registry))
    assert http.get('/v1/status').status_code == 503
