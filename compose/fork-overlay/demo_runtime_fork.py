"""Cross-VM conversation fork for the mixed-provider Compose demo."""

import asyncio
import io
import json
import logging
import os
import re
import shlex
import stat
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zipfile import ZipFile

from openhands.app_server.app_conversation.app_conversation_models import (
    ARCHIVE_WORKSPACE_PATH_TAG_KEY,
    AppConversationInfo,
)
from openhands.app_server.sandbox.e2b_sandbox_service import E2BSandboxService
from openhands.app_server.sandbox.routed_sandbox_service import RoutedSandboxService
from openhands.app_server.sandbox.sandbox_models import SandboxStartedBy, SandboxStatus
from openhands.app_server.sandbox.sandbox_store import (
    E2B_BACKEND,
    get_credential_org_id,
    lock_stored_sandbox_if_free,
)
from openhands.app_server.utils.customer_gateway_hooks import bind_profile_llm
from openhands.app_server.utils.redis_lock import RedisLockUnavailable, try_acquire_redis_lock
from openhands.sdk import LLM

_logger = logging.getLogger(__name__)
_ROOT = PurePosixPath('/workspace/project')
_CONVERSATIONS = '/var/openhands/conversations'
_MAX_BUNDLE = 16 * 1024 * 1024
_MAX_STATE_FILE = 4 * 1024 * 1024
_VM = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
_EVENT = re.compile(r'events/event-[0-9]{5,}-[0-9a-fA-F-]{8,}\.json')


class ForkConflict(RuntimeError):
    """The source cannot safely be forked in its current state."""


def _require(condition, message):
    if not condition:
        raise ForkConflict(message)


def _workspace(info):
    path = PurePosixPath(info.tags.get(ARCHIVE_WORKSPACE_PATH_TAG_KEY, ''))
    if path.is_relative_to(_ROOT) and '..' not in path.parts:
        return str(path)
    return None


def can_fork(info, sandbox):
    return bool(
        os.getenv('DEMO_E2B_FORK') == 'true'
        and sandbox
        and sandbox.supports_runtime_pause
        and sandbox.status == SandboxStatus.RUNNING
        and sandbox.credential_org_id
        and str(sandbox.credential_org_id) == os.getenv('DEMO_MODEL_ORG_ID')
        and info.created_by_user_id
        and info.created_by_user_id == sandbox.created_by_user_id
        and info.agent_kind == 'openhands'
        and not info.additional_repositories
        and _workspace(info)
        and info.llm_model in (
            'openai/deepseek-v4.1-flash', 'openai/gpt-4o-mini', 'gpt-4o-mini'
        )
    )


def _server(sandbox):
    _require(bool(_VM.fullmatch(sandbox.id)), 'Invalid E2B sandbox identity')
    url = next((item.url for item in sandbox.exposed_urls or [] if item.name == 'AGENT_SERVER'), '')
    address = urlsplit(url)
    _require(
        address.scheme == 'https'
        and address.netloc == f'8000-{sandbox.id}.e2b.app'
        and address.path in ('', '/')
        and not (address.query or address.fragment),
        'Unexpected E2B Agent Server address',
    )
    return url.rstrip('/')


async def _download(client, url, headers, cap, **kwargs):
    async with client.stream(
        'GET', url, headers=headers, timeout=18, follow_redirects=False, **kwargs
    ) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            _require(
                len(data) + len(chunk) <= cap, 'Checkpoint entry exceeds size limit'
            )
            data.extend(chunk)
    return bytes(data)


def _trajectory(archive, cid):
    items = {}
    with ZipFile(io.BytesIO(archive)) as source:
        entries = source.infolist()
        _require(len(entries) <= 350, 'Too many trajectory members')
        total = 0
        for entry in entries:
            full = entry.filename
            _require(full.startswith(cid.hex + '/'), 'Trajectory identity mismatch')
            relative = full[len(cid.hex) + 1 :]
            if entry.is_dir():
                continue
            if relative not in (
                'base_state.json',
                'meta.json',
            ) and not _EVENT.fullmatch(relative):
                continue
            mode = entry.external_attr >> 16
            _require(
                not stat.S_ISLNK(mode) and relative not in items,
                'Invalid trajectory member',
            )
            total += entry.file_size
            _require(
                entry.file_size <= _MAX_STATE_FILE and total <= _MAX_BUNDLE,
                'Trajectory too large',
            )
            items[relative] = source.read(entry)
    _require(
        {'base_state.json', 'meta.json'} <= items.keys(), 'Trajectory state missing'
    )
    _require(
        UUID(json.loads(items['base_state.json'])['id']) == cid,
        'Wrong conversation in state',
    )
    return items


def _headers(sandbox):
    _require(bool(sandbox.session_api_key), 'Runtime session unavailable')
    return {'X-Session-API-Key': sandbox.session_api_key}


async def _chat(client, server, headers, cid):
    data = json.loads(
        await _download(
            client,
            f'{server}/api/conversations/{cid.hex}/events/search',
            headers,
            _MAX_BUNDLE,
            params={'limit': 100},
        )
    )
    return {
        row['id']: row['llm_message']['content']
        for row in data['items']
        if row['kind'] == 'MessageEvent'
    }


async def _run_workspace_tool(client, server, headers, command, *arguments):
    source = Path(__file__).with_name('demo_fork_workspace.py').read_text()
    cmd = shlex.join(['python3', '-I', '-c', source, command, *arguments])
    response = await client.post(
        server + '/api/bash/execute_bash_command',
        headers=headers,
        timeout=22,
        follow_redirects=False,
        json={'command': cmd, 'cwd': '/tmp', 'timeout': 18},
    )
    response.raise_for_status()
    result = response.json()
    _require(result['exit_code'] == 0, 'Workspace archive operation failed')
    return json.loads(result['stdout'].strip().splitlines()[-1])


async def _workspace_snapshot(client, server, headers, workspace, *, download):
    temporary = '/tmp/canvas-pilot-' + uuid4().hex + '.tar.gz'
    try:
        manifest = await _run_workspace_tool(
            client, server, headers, 'capture', workspace, temporary
        )
        archive = (
            await _download(
                client,
                server + '/api/file/download',
                headers,
                _MAX_BUNDLE,
                params={'path': temporary},
            )
            if download
            else None
        )
        return manifest, archive
    finally:
        await _run_workspace_tool(client, server, headers, 'cleanup', temporary)


async def _upload(client, server, headers, path, data):
    response = await client.post(
        server + '/api/file/upload',
        headers=headers,
        params={'path': path},
        files={'file': (Path(path).name, data)},
        timeout=20,
        follow_redirects=False,
    )
    response.raise_for_status()


async def _fork_agent(saved, source_model, sandbox):
    agent = saved.get('agent', {})
    _require(agent.get('kind') == 'Agent', 'Only standard OpenHands chats can be forked')
    model = 'openai/gpt-4o-mini' if source_model == 'gpt-4o-mini' else source_model
    if model == 'openai/deepseek-v4.1-flash':
        url = os.environ['DEMO_MODEL_RELAY_URL']
        llm = await bind_profile_llm(LLM(model=model, base_url=url), sandbox)
        _require(llm.api_key is not None, 'Could not issue a model grant for the fork')
    else:
        relay_url = urlsplit(os.environ['DEMO_MODEL_RELAY_URL'])
        url = f'{relay_url.scheme}://{relay_url.netloc}/synthetic-model/v1'
        llm = LLM(model=model, base_url=url, api_key='synthetic-fork-fixture')
    # Rebuild the trusted model endpoint and issue a new grant for the new VM.
    # In particular, never replay model credentials found in the source archive.
    clean = llm.model_dump(mode='json', context={'expose_secrets': True})
    agent['llm'] = {**clean, 'usage_id': 'agent'}
    condenser = agent.get('condenser')
    if condenser and 'llm' in condenser:
        condenser['llm'] = {**clean, 'usage_id': 'condenser'}
    return agent


async def _capture(source_info, source, client, workspace):
    cid = source_info.id
    server, headers = _server(source), _headers(source)
    detail = f'{server}/api/conversations/{cid.hex}'

    async def require_idle():
        state = json.loads(await _download(client, detail, headers, _MAX_BUNDLE))
        _require(
            UUID(state['id']) == cid and state.get('execution_status') in ('idle', 'finished'),
            'Wait for the source chat to finish its current turn',
        )

    await require_idle()
    manifest, workspace_archive = await _workspace_snapshot(
        client, server, headers, workspace, download=True
    )
    archive = await _download(
        client, f'{server}/api/file/download-trajectory/{cid.hex}', headers, _MAX_BUNDLE
    )
    state = _trajectory(archive, cid)
    exported_chat = {}
    for name, data in state.items():
        if _EVENT.fullmatch(name):
            event = json.loads(data)
            if event.get('kind') == 'MessageEvent':
                exported_chat[event['id']] = event['llm_message']['content']
    current, _ = await _workspace_snapshot(client, server, headers, workspace, download=False)
    await require_idle()
    _require(
        current == manifest and await _chat(client, server, headers, cid) == exported_chat,
        'The source changed during capture. Let it settle and try again.',
    )
    return manifest, workspace_archive, state, exported_chat


async def _restore(source, child, child_id, title, client, workspace, snapshot):
    manifest, archive, state, history = snapshot
    server, headers = _server(child), _headers(child)
    temporary = f'/tmp/canvas-pilot-{uuid4().hex}.tar.gz'
    try:
        await _upload(client, server, headers, temporary, archive)
        restored = await _run_workspace_tool(
            client, server, headers, 'restore', temporary, workspace
        )
        _require(restored == manifest, 'Forked workspace did not match the source')
    finally:
        await _run_workspace_tool(client, server, headers, 'cleanup', temporary)

    saved = json.loads(state['base_state.json'])
    agent = await _fork_agent(saved, source.llm_model, child)
    saved.update(id=child_id.hex, execution_status='idle')
    saved.pop('stats', None)
    metadata = json.loads(state['meta.json'])
    metadata.update(
        id=child_id.hex, title=title,
        forked_from_conversation_id=source.id.hex,
        forked_from_event_id=None,
    )
    metadata.pop('metrics', None)
    state['base_state.json'] = json.dumps(saved).encode()
    state['meta.json'] = json.dumps(metadata).encode()
    for relative, data in state.items():
        await _upload(client, server, headers, f'{_CONVERSATIONS}/{child_id.hex}/{relative}', data)

    response = await client.post(
        server + '/api/conversations',
        headers=headers,
        json={
            'conversation_id': child_id.hex,
            'workspace': {'kind': 'LocalWorkspace', 'working_dir': workspace},
            'agent': agent,
        },
        timeout=32,
        follow_redirects=False,
    )
    response.raise_for_status()
    _require(UUID(response.json()['id']) == child_id, 'The fork could not be opened')
    _require(await _chat(client, server, headers, child_id) == history, 'Fork chat history did not match')


async def fork_runtime(conversation_id, user_context, info_service, app_service,
                       router_service, db_session, client):
    if not isinstance(router_service, RoutedSandboxService):
        raise ForkConflict('E2B forks are unavailable on this deployment')
    user_id = await user_context.get_user_id()
    org_id = await get_credential_org_id(user_context)
    source_info = await info_service.get_app_conversation_info(conversation_id)
    if not source_info or source_info.created_by_user_id != user_id:
        raise PermissionError('Only the owner can fork this conversation')
    source_id = source_info.sandbox_id
    provider = await router_service.for_sandbox(source_id)
    if not isinstance(provider, E2BSandboxService):
        raise ForkConflict('Only E2B sandboxes can be forked')
    source = await provider.get_sandbox(source_id)
    if not can_fork(source_info, source) or source.credential_org_id != org_id:
        raise ForkConflict('This chat cannot be forked. Reopen an eligible E2B chat first.')
    workspace = _workspace(source_info)
    try:
        lock = await try_acquire_redis_lock(f'demo-e2b-fork:{conversation_id}', 240)
    except RedisLockUnavailable as exc:
        raise ForkConflict('Fork lock is unavailable') from exc
    if lock is None:
        raise ForkConflict('A fork is already in progress')

    child = None
    child_id = uuid4()
    publishing = False
    try:
        async with asyncio.timeout(155):
            row = await lock_stored_sandbox_if_free(db_session, E2B_BACKEND, source_id)
            _require(
                row is not None and row.created_by_user_id == user_id
                and row.credential_org_id == org_id,
                'The source VM has another operation in progress',
            )
            snapshot = await _capture(source_info, source, client, workspace)
            await db_session.commit()
            child = await provider.start_sandbox(
                sandbox_spec_id=source.sandbox_spec_id,
                started_by=SandboxStartedBy.APP_CONVERSATION,
                preserve_sandbox_id=source_id,
            )
            _require(
                child.id != source_id and child.created_by_user_id == user_id
                and child.credential_org_id == org_id,
                'Fork requires a different E2B VM owned by the same user and organization',
            )
            await db_session.commit()
            row = await lock_stored_sandbox_if_free(db_session, E2B_BACKEND, child.id)
            _require(row is not None, 'The new VM is unavailable')
            title = f'Fork of {source_info.title or "conversation"}'[:200]
            await _restore(source_info, child, child_id, title, client, workspace, snapshot)
            await db_session.commit()

        # Preserve a verified fork if publication has an ambiguous commit outcome.
        publishing = True
        info = AppConversationInfo(
            id=child_id, sandbox_id=child.id, created_by_user_id=user_id,
            title=title, llm_model=source_info.llm_model,
            selected_repository=source_info.selected_repository,
            selected_branch=source_info.selected_branch,
            git_provider=source_info.git_provider,
            git_provider_id=source_info.git_provider_id,
            parent_conversation_id=conversation_id,
            tags={ARCHIVE_WORKSPACE_PATH_TAG_KEY: workspace},
        )
        existing = await info_service.get_app_conversation_info(child_id)
        if existing:
            _require(
                existing.sandbox_id == child.id and existing.created_by_user_id == user_id,
                'The fork conversation could not be bound',
            )
        await info_service.save_app_conversation_info(info)
        result = await app_service.get_app_conversation(child_id)
        _require(result is not None, 'The fork was saved but could not be read')
        return result
    finally:
        if child and child.id != source_id and child.created_by_user_id == user_id and child.credential_org_id == org_id and not publishing:
            try:
                await db_session.rollback()
                info = await info_service.get_app_conversation_info(child_id)
                if info and info.created_by_user_id == user_id and info.sandbox_id == child.id:
                    await info_service.delete_app_conversation_info(child_id)
                await provider.delete_sandbox(child.id)
                await db_session.commit()
            except Exception:
                _logger.warning('Could not clean up a failed E2B fork', exc_info=True)
        try:
            await lock.release()
        except Exception:
            _logger.warning('Could not release E2B fork lock', exc_info=True)
