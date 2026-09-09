import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('keyword_eval', Path(__file__).parents[1] / 'benchmarks/evaluate_retrieval.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def report(ranks):
    return {'dataset_sha256': 'fixture', 'keyword': {'cases': [
        {'case': key, 'rank': rank, 'mode': 'keyword'} for key, rank in ranks.items()]}}


def test_empty_search_cannot_pass_and_gains_do_not_hide_losses():
    baseline = {'version': 1, 'dataset_sha256': 'fixture', 'ranks': {'a': 2, 'b': None}}
    assert evaluation.keyword_regressions(report({'a': None, 'b': None}), baseline)
    assert evaluation.keyword_regressions(report({'a': 3, 'b': 1}), baseline)
    assert not evaluation.keyword_regressions(report({'a': 1, 'b': 1}), baseline)


def test_changed_dataset_or_case_set_requires_review():
    baseline = {'version': 1, 'dataset_sha256': 'different', 'ranks': {'a': 1}}
    with pytest.raises(ValueError, match='dataset'):
        evaluation.keyword_regressions(report({'a': 1}), baseline)
    baseline['dataset_sha256'] = 'fixture'
    with pytest.raises(ValueError, match='case IDs'):
        evaluation.keyword_regressions(report({'other': 1}), baseline)


def test_cli_writes_failure_report_before_exiting(tmp_path, monkeypatch):
    import json
    import sys

    baseline = Path(__file__).parents[1] / 'benchmarks/keyword-baseline.json'
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['evaluate', '--data-dir', str(tmp_path / 'catalog'),
                                    '--output', str(output), '--baseline', str(baseline)])
    monkeypatch.setattr(evaluation, 'hybrid_search', lambda *args: {'results': [], 'mode': 'keyword'})
    with pytest.raises(SystemExit, match='regressed on 20 cases'):
        evaluation.main()
    result = json.loads(output.read_text())
    assert result['keyword']['recall_at_10'] == 0
    assert result['regression']['status'] == 'failed'
    assert len(result['regression']['failures']) == 20
