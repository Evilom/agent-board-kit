"""Project capability directory and shared bulletins; never launch or assign agents."""
import json
import uuid

from .common import NetworkError, digest, identifier, now
from .collaboration import strings, text


def profile(value):
    if not isinstance(value, dict) or set(value) - {'summary', 'skills', 'tools', 'knowledge', 'limitations'}:
        raise NetworkError('能力档案包含未知字段')
    return dict(summary=text(value.get('summary', ''), '能力摘要', 1000, True),
                **{key: strings(value.get(key, []), key, 30)
                   for key in ('skills', 'tools', 'knowledge', 'limitations')})


def update_profile(c, actor, key, body):
    value = profile(body.get('profile'))
    with c.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        agent = c.own_agent(db, actor, key, identifier(body.get('session_id')))
        agent.update(profile=value, profile_updated_at=now())
        c.put(db, 'agent', agent)
        return agent


def candidates(c, actor, project, skills, exclude=None):
    skills = strings(skills, '所需能力')
    result = []
    for agent in c.list_agents(actor, project)['agents']:
        declared = {s.casefold() for s in agent.get('profile', {}).get('skills', [])}
        if agent['id'] != exclude and all(s.casefold() in declared for s in skills):
            result.append(dict(agent, matched_skills=skills,
                               match_basis='Agent 声明的能力；联系前请核对范围与可用状态'))
    return {'agents': sorted(result, key=lambda a: (not a['online'], a['name']))}


def reader(c, db, actor, project, agent_id=None, session_id=None):
    c.hub.authorize(actor, project, 'read')
    if not agent_id:
        return 'principal:' + actor[0]
    agent = c.own_agent(db, actor, identifier(agent_id), identifier(session_id))
    if agent['project_id'] != project:
        raise NetworkError('公告板项目不一致', 403)
    return 'agent:' + agent['id']


def publish(c, actor, body):
    project = identifier(body.get('project_id'))
    c.hub.authorize(actor, project, 'collaborate')
    title = text(body.get('title'), '公告标题', 200)
    content = text(body.get('body'), '公告正文', 8000)
    category = body.get('category', 'info')
    if category not in ('info', 'capability', 'help', 'update'):
        raise NetworkError('公告分类无效')

    def create(db):
        sender = None
        if body.get('from_agent_id'):
            sender = c.own_agent(db, actor, identifier(body['from_agent_id']), identifier(body.get('session_id')))
            if sender['project_id'] != project:
                raise NetworkError('发布方不属于该项目', 403)
        work_id = body.get('work_id') or None
        if work_id and c.get(db, 'work', identifier(work_id))['project_id'] != project:
            raise NetworkError('关联任务不属于该项目', 403)
        item = dict(id='bulletin-' + uuid.uuid4().hex, project_id=project, title=title, body=content,
                    category=category, work_id=work_id, from_agent_id=sender['id'] if sender else None,
                    from_principal=actor[0], created_at=now())
        c.put(db, 'bulletin', item)
        c.event(db, actor, item, 'bulletin.published')
        return item
    return c.atomic_request(actor, body, 'bulletin.publish', create)


def receipt_id(identity, key):
    return 'read-' + digest([identity, key])


def unread_count(db, project, identity):
    return db.execute('''SELECT count(*) FROM collab_records b WHERE b.kind='bulletin' AND b.project=?
        AND NOT EXISTS(SELECT 1 FROM collab_records r WHERE r.kind='bulletin-read' AND r.project=b.project
            AND json_extract(r.data,'$.bulletin_id')=b.id AND json_extract(r.data,'$.reader')=?)''',
        (project, identity)).fetchone()[0]


def list_bulletins(c, actor, project, agent_id=None, session_id=None, before=None, unread=False):
    try:
        before = int(before) if before else 9223372036854775807
        if not 1 <= before <= 9223372036854775807:
            raise ValueError()
    except (ValueError, TypeError):
        raise NetworkError('公告分页游标无效') from None
    with c.store.connect() as db:
        identity = reader(c, db, actor, project, agent_id, session_id)
        # The receipt joins by reader and bulletin; browsing never writes a read receipt.
        records = db.execute('''SELECT b.rowid,b.data,r.data FROM collab_records b
            LEFT JOIN collab_records r ON r.kind='bulletin-read' AND r.project=b.project
                AND json_extract(r.data,'$.bulletin_id')=b.id AND json_extract(r.data,'$.reader')=?
            WHERE b.kind='bulletin' AND b.project=? AND b.rowid<? ''' +
            ('AND r.id IS NULL ' if unread else '') + 'ORDER BY b.rowid DESC LIMIT 101',
            (identity, project, before)).fetchall()
        items = [dict(json.loads(row[1]), read_at=json.loads(row[2])['read_at'] if row[2] else None)
                 for row in records[:100]]
        return {'bulletins': items, 'next_cursor': str(records[99][0]) if len(records) > 100 else None,
                'unread_count': unread_count(db, project, identity)}


def mark_read(c, actor, key, body):
    with c.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        item = c.get(db, 'bulletin', key)
        identity = reader(c, db, actor, item['project_id'], body.get('agent_id'), body.get('session_id'))
        receipt = receipt_id(identity, key)
        row = db.execute("SELECT data FROM collab_records WHERE kind='bulletin-read' AND id=?", (receipt,)).fetchone()
        if row:
            return json.loads(row[0])
        value = dict(id=receipt, project_id=item['project_id'], reader=identity, bulletin_id=key, read_at=now())
        c.put(db, 'bulletin-read', value)
        return value


def updates(c, actor, key, session):
    """Read-only notification summary. No delivery, acknowledgment or work state change."""
    with c.store.connect() as db:
        agent = c.own_agent(db, actor, identifier(key), identifier(session))
        project = agent['project_id']
        identity = reader(c, db, actor, project, key, session)
        messages = [dict(id=r[0], work_id=json.loads(r[1]).get('work_id')) for r in db.execute(
            "SELECT id,data FROM collab_records WHERE kind='message' AND project=? "
            "AND json_extract(data,'$.to_agent_id')=? AND json_extract(data,'$.acknowledged_at') IS NULL ORDER BY rowid LIMIT 101",
            (project, key))]
        count = unread_count(db, project, identity)
        return {'messages': messages[:100], 'messages_have_more': len(messages) > 100,
                'unread_bulletins': count,
                'next': '用 board_inbox 读取私信，用 board_bulletins 读取公告；按当前用户目标决定是否回复或处理。通知不会自动认领或执行任务。'}
