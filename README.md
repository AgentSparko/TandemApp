# TandemApp

TandemApp is a dashboard, gateway and usage ledger in front of OpenAI-compatible servers and
cloud APIs. Clients send their requests to one address; TandemApp routes each one by its model id
to the backend that serves it and records tokens, timings and cost. It also manages a fleet of
machines through TandemNode, its agent on each machine, and carries Chat (chatd) and the
conductor.

TandemApp runs no model itself. The TandemLLM engine is a separate project
([TandemLLM](https://github.com/TandemLLM/TandemEngine)); TandemApp talks to it, and to any other
OpenAI-compatible server, over HTTP. The engine API it expects is
[contracts/engine-api-v1](contracts/engine-api-v1/README.md).

## Install

Python 3.11 or newer. The web page is a build output and not in git: build it with Node 20.19 or
newer, or fetch a released bundle (`tools/dashboard_bundle.py fetch`).

    python3 tools/dashboard_bundle.py build
    python3 -m pip install '.[app]'       # '.[host]' adds NVML for GPU hosts

Other ways: `brew install ./packaging/homebrew/tandemapp.rb`, or the image
`docker build -f packaging/Dockerfile.app -t tandemllm-app .`

## Run

    tandem-app                            # 127.0.0.1:8080, state in ~/.tandemllm
    open http://127.0.0.1:8080/dashboard/

`tandem-app service install --open` runs it in the background (launchd on macOS, a systemd user
unit on Linux). `tandem-app check` prints what would run without starting anything.

## Layout

| path | what it is |
|-|-|
| `proxy/` | the controller: gateway, ledger, fleet, `tandem-app` and `tandem-node` |
| `tandemapp/` | the conductor and the dashboard API |
| `chatd/` | Chat's service |
| `dashboard/` | the web page (TypeScript, Vite) |
| `contracts/` | the API contracts with the engine and between the parts |
| `packages/` | the texts the conductor ships with |
| `packaging/` | the build backend, Dockerfile and Homebrew formula |
| `tests/`, `tools/` | the test suite and the tools |
| `docs/` | documentation |

## Tests

    python3 tests/script_suite.py --all
    (cd dashboard && npm ci && npx vitest run)

## Documentation

[docs/proxy.md](docs/proxy.md) covers configuration, the fleet, TandemNode and the data plane.

## License

AGPL-3.0-only ([LICENSE](LICENSE)), or a commercial license
([COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)). Third-party notices: [NOTICE](NOTICE). The
documentation in `docs/` is CC BY 4.0 ([docs/LICENSE](docs/LICENSE)).
