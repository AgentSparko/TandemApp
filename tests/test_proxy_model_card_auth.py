"""A single model card answers only to a key, as the list route has always required.

Before the fix the /v1/models branch checked authorize() and /v1/models/{id} did not:
a deployment that handed out keys could still be read card-by-card by anyone who
knew a model name — the 404-vs-401 difference even made enumeration free.

SEC-28: the request gate asks for the same credential on both (an API token, here a key of
before imported as one); this machine without a password is local mode and is let in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from proxy_util import Proxy  # noqa: E402

KEY = "tdm-0123456789abcdef"


def test_card_needs_the_key_the_list_needs():
    # not listening yet: no local mode, the outside client this fix is about
    with Proxy(env={"TANDEM_API_KEYS": f"ci:{KEY}"}, local=False) as px:
        code, _, _ = px.get("/v1/models")
        assert code == 401, "the list was already gated: unchanged behavior"

        code, _, _ = px.get("/v1/models/anything")
        assert code == 401, "the card must not answer below the gate"

        code, _, _ = px.get("/v1/models/anything",
                           headers={"Authorization": f"Bearer {KEY}"})
        assert code == 404, "with the key the card's own 'no model' answer is back"


def test_local_mode_unchanged():
    with Proxy() as px:
        code, _, _ = px.get("/v1/models/anything")
        assert code == 404, "this machine with no password: nobody is refused"


if __name__ == "__main__":
    test_card_needs_the_key_the_list_needs()
    test_local_mode_unchanged()
    print("2 passed")
