"""Prepare a fork overlay from the source of the routed Enterprise candidate."""

import argparse
import ast
from pathlib import Path

APP = Path('openhands/app_server/app_conversation')
E2B = Path('openhands/app_server/sandbox/e2b_sandbox_service.py')


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f'Unsupported Enterprise candidate. Expected one occurrence of {old!r}')
    return source.replace(old, new, 1)


def prepare(source_dir: Path, output: Path) -> None:
    package = Path(__file__).resolve().parent
    models = APP / 'app_conversation_models.py'
    service = APP / 'live_status_app_conversation_service.py'
    router = APP / 'app_conversation_router.py'

    def read_source(path: Path) -> str:
        return (source_dir / path).read_text()

    changes = {}
    reuse = '    sandbox_supports_workspace_reuse: bool = False'
    changes[models] = replace_once(
        read_source(models), reuse, reuse + '\n    sandbox_supports_runtime_fork: bool = False'
    )
    provider_import = (
        '        from openhands.app_server.sandbox.routed_sandbox_service import RoutedSandboxService'
    )
    projection = '\n\n        return AppConversation('
    code = replace_once(
        read_source(service),
        provider_import + projection,
        provider_import
        + '\n        from openhands.app_server.app_conversation.demo_runtime_fork import can_fork'
        + projection,
    )
    reuse_projection = '            sandbox_supports_workspace_reuse=bool('
    changes[service] = replace_once(
        code,
        reuse_projection,
        '            sandbox_supports_runtime_fork=can_fork(app_conversation_info, sandbox),\n'
        + reuse_projection,
    )
    changes[router] = (
        read_source(router).rstrip() + '\n\n\n' + (package / 'fork_route.txt').read_text()
    )
    start_argument = '        started_by: SandboxStartedBy = SandboxStartedBy.SANDBOX_API,\n'
    signature_end = '    ) -> SandboxInfo:'
    code = replace_once(
        read_source(E2B),
        start_argument + signature_end,
        start_argument + '        *,\n        preserve_sandbox_id: str | None = None,\n' + signature_end,
    )
    pause_old = 'await self.pause_old_sandboxes(self.max_num_sandboxes - 1)'
    code = replace_once(
        code,
        '        ' + pause_old,
        '        if preserve_sandbox_id:\n'
        '            # Forks reserve an additional slot and leave active VMs alone.\n'
        '            running = 0\n'
        '            async for current in page_iterator(self.search_sandboxes, limit=100):\n'
        '                if current.status in (SandboxStatus.RUNNING, SandboxStatus.STARTING):\n'
        '                    running += 1\n'
        '            if running >= self.max_num_sandboxes:\n'
        "                raise SandboxError('Pause another E2B sandbox before forking')\n"
        '        else:\n'
        '            ' + pause_old,
    )
    if 'import page_iterator' not in code:
        injector_import = 'from openhands.app_server.services.injector import InjectorState'
        code = replace_once(
            code,
            injector_import,
            injector_import + '\nfrom openhands.sdk.utils.paging import page_iterator',
        )
    changes[E2B] = code

    for filename in ('demo_runtime_fork.py', 'demo_fork_workspace.py'):
        changes[APP / filename] = (package / filename).read_text()

    for path, source in changes.items():
        ast.parse(source, filename=str(path))
    output.mkdir(parents=True, exist_ok=False)
    for path, source in changes.items():
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('enterprise_source', type=Path)
    parser.add_argument('output', type=Path, help='New directory for the generated overlay')
    args = parser.parse_args()
    prepare(args.enterprise_source, args.output)
