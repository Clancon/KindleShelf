import pytest
from bs4 import BeautifulSoup


@pytest.fixture
def post_form():
    def submit(client, path, data=None, **kwargs):
        page = BeautifulSoup(client.get("/").get_data(as_text=True), "html.parser")
        token = page.find("input", attrs={"name": "csrf_token"})["value"]
        payload = dict(data or {})
        payload["csrf_token"] = token
        return client.post(path, data=payload, **kwargs)

    return submit
