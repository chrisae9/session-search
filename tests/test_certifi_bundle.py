from pathlib import Path

import pytest


def test_trust_bundle_excludes_compromised_emsign_root_and_keeps_unaffected_roots():
    # GHSA-6q48-6c7w-9gw2: 2026.10.18 also removed unaffected roots;
    # 2026.10.19 restores them without trusting the compromised G3 chain.
    certifi = pytest.importorskip('certifi')
    x509 = pytest.importorskip('cryptography.x509')
    from cryptography.x509.oid import NameOID

    certificates = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    common_names = {
        name.value
        for certificate in certificates
        for name in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    }
    assert 'emSign ECC Root CA - G3' not in common_names
    assert {
        'emSign Root CA - G1',
        'emSign Root CA - C1',
        'emSign ECC Root CA - C3',
    } <= common_names
