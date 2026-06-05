import json
import pytest
from unittest.mock import patch

import os

def test_sse_run_and_kill(client):
    # Trigger run on test/long.sh
    response = client.post('/api/scripts/run', json={'path': 'test/long.sh'})
    assert response.status_code == 200
    
    # We will read chunks
    iterator = response.iter_encoded()
    
    # Let's read the first few events
    started_found = False
    run_id = None
    
    # Read up to some chunks
    for chunk in iterator:
        text = chunk.decode('utf-8')
        for line in text.split('\n'):
            if line.startswith('data: '):
                try:
                    data = json.loads(line[6:])
                    if data.get('type') == 'started':
                        started_found = True
                        run_id = data.get('run_id')
                        break
                except json.JSONDecodeError:
                    pass
        if started_found:
            break
            
    assert started_found
    assert run_id is not None
    
    # Now call kill api
    kill_response = client.post('/api/scripts/kill', json={'run_id': run_id})
    assert kill_response.status_code == 200
    
    # Read remaining events from iterator to ensure stream terminates cleanly
    aborted_found = False
    for chunk in iterator:
        text = chunk.decode('utf-8')
        for line in text.split('\n'):
            if line.startswith('data: '):
                try:
                    data = json.loads(line[6:])
                    if data.get('type') == 'aborted':
                        aborted_found = True
                        break
                except json.JSONDecodeError:
                    pass
        if aborted_found:
            break
            
    assert aborted_found


def test_run_script_not_found_returns_404(client):
    r = client.post('/api/scripts/run', json={'path': 'no/such.sh'})
    assert r.status_code == 404
    body = r.get_json()
    assert 'error' in body


def test_run_script_popen_error_stream_returns_error_and_cleans(client, app_module, tmp_path):
    # create a small script in the scripts dir
    scripts_dir = app_module.SCRIPTS_DIR
    os.makedirs(scripts_dir, exist_ok=True)
    script_path = os.path.join(scripts_dir, 'tmp_test.sh')
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\necho hello\n')

    # make Popen raise to trigger exception path and cleanup
    with patch('subprocess.Popen', side_effect=OSError('spawn failed')):
        resp = client.post('/api/scripts/run', json={'path': 'tmp_test.sh'})
        assert resp.status_code == 200
        # iterate stream to find error message
        found_error = False
        for chunk in resp.iter_encoded():
            text = chunk.decode('utf-8')
            for line in text.split('\n'):
                if line.startswith('data: '):
                    try:
                        data = json.loads(line[6:])
                        if data.get('type') == 'error':
                            found_error = True
                            break
                    except Exception:
                        pass
            if found_error:
                break

        assert found_error
        assert not app_module.active_processes


def test_kill_missing_and_unknown_run_id(client):
    r = client.post('/api/scripts/kill', json={})
    assert r.status_code == 400
    r2 = client.post('/api/scripts/kill', json={'run_id': 'nope'})
    assert r2.status_code == 404
