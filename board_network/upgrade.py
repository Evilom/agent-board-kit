"""Portable Agent Board: verified, additive upgrade of the central configuration."""
import json
import os
import secrets
import sqlite3
from pathlib import Path
from .common import NetworkError, load_config


def upgrade(config_path):
    path = Path(config_path).expanduser().resolve()
    raw = path.read_bytes();config=json.loads(raw)
    loaded=load_config(path)
    suffix=secrets.token_hex(4)
    backup=path.with_name(path.name+'.before-0.2-'+suffix)
    backup.write_bytes(raw);backup.chmod(0o600)
    if backup.read_bytes()!=path.read_bytes():
        raise NetworkError('配置在备份期间发生变化，未应用升级')
    database_backup=None
    if config['role']=='server':
        source=Path(loaded['database'])
        if source.exists():
            database_backup=source.with_name(source.stem+'.before-0.2-'+suffix+'.sqlite3')
            with sqlite3.connect(str(source)) as db, sqlite3.connect(str(database_backup)) as dest:
                db.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise NetworkError('数据库备份未通过完整性检查，未应用升级')
                for table in ('tasks','audit'):
                    if db.execute('SELECT count(*) FROM '+table).fetchone()!=dest.execute('SELECT count(*) FROM '+table).fetchone():
                        raise NetworkError('数据库备份记录数量不一致，未应用升级')
            database_backup.chmod(0o600)
        for principal in config['principals'].values():
            for grant in principal['grants'].values():
                if 'execute' in grant['operations'] and 'collaborate' not in grant['operations']:
                    grant['operations'].append('collaborate')
    temp=path.with_name(path.name+'.upgrade-'+suffix)
    temp.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temp.chmod(0o600)
    os.replace(temp,path)
    if config['role']=='server':
        from .hub import Hub
        hub=Hub(load_config(path))
        with hub.store.connect() as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise NetworkError('升级后完整性检查失败，请保留现场和备份')
    return {'config_backup':str(backup),'database_backup':str(database_backup) if database_backup else None,
            'message':'升级已完成，请重启本设备服务；现有任务和原始资料已保留。'}
