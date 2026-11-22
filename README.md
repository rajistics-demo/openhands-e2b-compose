# E2B for OpenHands Enterprise

**Experimental.** This package configures the E2B adapter bundled in
Enterprise 1.88.0 and builds its Agent Server template. The control plane and
Canvas stay in Docker Compose. E2B runs the conversation's sandbox.

- [Build a template and configure Compose](docs/SETUP.md)
- [Interfaces, design choices, and alternatives](docs/INTERFACES.md)
- [Build the pinned Canvas Pause demo](canvas/README.md)
- [Pause, reopen and verify output](docs/SETUP.md#validate-and-clean-up)

The [template builder](scripts/build_template.py) and [Compose overlay](compose/)
are in this repo. Transport and lifecycle code live in the same Enterprise
release as the application. See the [bundled adapter](https://github.com/OpenHands/enterprise/blob/c6191be/openhands/app_server/sandbox/e2b_sandbox_service.py).

The [provider status record](provider.json) lists the package checks. The
earlier pilot exercised native conversations and same-process preview
recovery using a provider-aware Canvas build. The stock Sandbox V1 API can
exercise the lifecycle independently of that Canvas change.
