"""Portable Agent Board: Bind reusable Dagu execution to the collaboration task without a second queue."""
from .common import NetworkError, digest, now, identifier
from .execution import CAPABILITIES
from .hub import public_task


def run(collab, actor, key, body):
    hub = collab.hub
    workspace_id = identifier(body.get('workspace_id'))
    recipe = body.get('capability')
    if recipe not in CAPABILITIES:
        raise NetworkError('未知的设备能力')
    fingerprint = digest(body)
    with collab.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        work = collab.get(db, 'work', key)
        hub.authorize(actor, work['project_id'], 'execute', workspace_id)
        hub.authorize(actor, work['project_id'], 'collaborate')
        if work['status'] == 'ready':
            if body.get('revision') != work['revision']:
                raise NetworkError('任务已更新，请刷新', 409)
            work.update(status='executing', execution_id=None, execution=None, attempt_id=None, blocker=None,
                        run_request={'fingerprint': fingerprint, 'body': body, 'key': key + '-' + str(work['revision'])},
                        revision=work['revision'] + 1, updated_at=now())
            collab.put(db, 'work', work)
            collab.event(db, actor, work, 'execution.reserved', {'workspace_id': workspace_id, 'capability': recipe})
        elif work['status'] != 'executing' or work.get('run_request', {}).get('fingerprint') != fingerprint:
            raise NetworkError('任务已有执行归属，不能重复启动', 409)
    workspace = dict(hub.config['projects'][work['project_id']]['workspaces'][workspace_id])
    with collab.store.connect() as db:
        try:
            device = collab.get(db, 'device', workspace['device_id'])
            if device.get('environment_id') == workspace['environment_id'] and device.get('os') == workspace['os']:
                workspace['runner'] = device.get('runner')
        except NetworkError as exc:
            if exc.status != 404:
                raise
    request = {k: work[k] for k in ('project_id', 'title', 'goal', 'scope', 'constraints', 'acceptance')}
    # The execution ledger uses acceptance for the review record; criteria must
    # therefore be supplied to the recipe before that field is initialized.
    request['criteria'] = request.pop('acceptance')
    request.update(workspace_id=workspace_id, recipe=recipe, work_id=key,
                   target={k: workspace[k] for k in ('device_id', 'environment_id', 'os')})
    def build(task):
        contract = dict(task, acceptance=task['criteria'])
        return hub.dagu.build(contract, workspace)
    execution, _ = collab.store.create(work['project_id'], work['run_request']['key'], request, actor[0], build)
    with collab.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        work = collab.get(db, 'work', key)
        if work['execution_id'] != execution['task_id']:
            work.update(execution_id=execution['task_id'], attempt_id=execution['attempt_id'])
            work['attempts'].append({'id': execution['attempt_id'], 'workspace_id': workspace_id, 'capability': recipe, 'started_at': now()})
            collab.put(db, 'work', work)
    if execution['status'] == 'prepared':
        try:
            hub.dagu.require_worker(workspace)
        except NetworkError as exc:
            collab.store.change(execution['task_id'], actor[0], 'worker.unavailable', lambda t: t.update(last_error=str(exc)), {'prepared'})
            return reconcile(collab, actor, key)
        execution, owned = collab.store.change(execution['task_id'], actor[0], 'dispatch.started', lambda t: t.update(status='submitting'), {'prepared'})
        if owned:
            try:
                hub.dagu.submit(execution)
                collab.store.change(execution['task_id'], actor[0], 'dispatch.acknowledged', lambda t: t.update(status='queued', last_error=None), {'submitting'})
            except NetworkError as exc:
                collab.store.change(execution['task_id'], actor[0], 'dispatch.uncertain', lambda t: t.update(status='unknown', last_error=str(exc)), {'submitting'})
    return reconcile(collab, actor, key)


def reconcile(collab, actor, key):
    work = collab.work_detail(actor, key)
    if not work['execution_id']:
        return work
    execution = collab.hub.reconcile(actor, work['execution_id'])
    with collab.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = collab.get(db, 'work', key)
        if current['status'] == 'executing' and current['execution_id'] == execution['task_id']:
            current['execution'] = execution
            if execution['status'] in ('review', 'failed'):
                receipt = (execution.get('evidence') or {}).get('receipt') or {}
                current.update(status='review', revision=current['revision'] + 1, updated_at=now(),
                    result={'summary': receipt.get('error') or ('设备执行已结束，请对照输出逐项审核。' if execution['status'] == 'review' else '设备执行失败，请检查执行记录。'),
                            'checks': [{'criterion': c, 'status': 'unverified', 'evidence': '待人工对照设备输出确认'} for c in current['acceptance']],
                            'artifact_ids': [], 'attempt_id': current['attempt_id'], 'submitted_at': now(), 'kind': 'device-execution'})
                current['attempts'][-1]['execution'] = execution
                collab.event(db, actor, current, 'execution.finished', {'status': execution['status']})
            collab.put(db, 'work', current)
    return collab.work_detail(actor, key)
