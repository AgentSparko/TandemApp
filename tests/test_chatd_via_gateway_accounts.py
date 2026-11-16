"""SRV-869: the chatd suite (tests/test_chatd.py) through the controller's `/chat-engine`, as
tests/test_chatd_via_gateway.py runs it, with the controller on TANDEM_AUTH=accounts, an owner and
local mode off. chatd calls it with its service identity svc:chatd (minted as proxy/app.py
own_chat mints it at each start of chatd); nothing on the same machine is let in for being there,
so every call chatd makes needs that identity's rights (tests/chatd_util.py GATEWAY.accounts).

CPU only. Run: python tests/test_chatd_via_gateway_accounts.py
"""

from __future__ import annotations

import chatd_util

chatd_util.GATEWAY.on = True
chatd_util.GATEWAY.accounts = True

import test_chatd_via_gateway as via  # noqa: E402

if __name__ == "__main__":
    via.main()
