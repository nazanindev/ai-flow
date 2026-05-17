from flow.ping import flow_ping

def test_ping():
    assert flow_ping() == 'pong'
