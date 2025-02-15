"""
This test module tests the COHD API by making requests to cohd.io/api and checking the schema of the response JSONs and
checking the results against known values.

Intended to be run with pytest: pytest -s test_cohd_io.py
"""
from urllib.parse import urljoin
import requests

# Choose which server to test
servers = ['https://cohd.io/api',
           'https://cohd-api.ci.transltr.io/api',
           'https://cohd-api.test.transltr.io/api',
           'https://cohd-api.transltr.io/api']


def test_alive():
    """ Check the /health endpoint of each server to check that it's alive.
    """
    unhealthy = False
    for server in servers:
        print(f'\ntest_alive: testing /health on {server}..... ')
        response = requests.get(urljoin(server, '/health'), timeout=10)

        if response.status_code == 200:
            print('\t' + response.text)
        else:
            print(f'\tUNHEALTHY!')
            unhealthy = True

    # No server should be unhealthy
    assert not unhealthy
