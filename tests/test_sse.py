import json
import pytest

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
