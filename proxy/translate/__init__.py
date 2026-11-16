"""Translations between protocols, with no I/O: each direction a pure function (or one stream
object fed event by event) with golden fixtures in tests/fixtures/proxy/anthropic/.

    anthropic   OpenAI chat completions to an Anthropic-compatible Messages server, and back
                (SRV-877, cell 3); a Messages API client to an OpenAI-compatible backend, and
                back (SRV-879, cell 4: `from_messages`, `to_message`, `ChatToMessages`)
"""
