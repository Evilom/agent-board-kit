"""Portable Agent Board: Short-lived browser access; credentials stay in the device's central config."""
import hashlib
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

from .common import NetworkError, read_token


class BrowserAccess:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.tickets, self.sessions = {}, {}

    def grant(self, actor, seconds):
        return (actor[0], hashlib.sha256(read_token(actor[1]['token_file']).encode()).hexdigest(), time.time() + seconds)

    def cleanup(self):
        for records in (self.tickets, self.sessions):
            for key, value in list(records.items()):
                if value[2] <= time.time():
                    records.pop(key, None)

    def ticket(self, actor):
        with self.lock:
            self.cleanup()
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = self.grant(actor, 60)
        return {'ticket': ticket, 'expires_in': 60}

    def actor(self, grant):
        principal = self.hub.config['principals'].get(grant[0])
        if not principal or principal.get('disabled') or grant[2] <= time.time():
            raise NetworkError('登录已过期，请从客户端重新打开', 401)
        fingerprint = hashlib.sha256(read_token(principal['token_file']).encode()).hexdigest()
        if not secrets.compare_digest(fingerprint, grant[1]):
            raise NetworkError('设备凭据已更新，请重新登录', 401)
        return grant[0], principal

    def exchange(self, ticket):
        with self.lock:
            self.cleanup()
            grant = self.tickets.pop(ticket, None)
            if not grant:
                raise NetworkError('登录链接已过期或已使用，请重新打开', 401)
            actor = self.actor(grant)
            key = secrets.token_urlsafe(32)
            self.sessions[key] = self.grant(actor, 8 * 3600)
        return key

    def authenticate(self, cookie):
        jar = SimpleCookie()
        try:
            jar.load(cookie)
            key = jar['agentboard_session'].value
        except (KeyError, ValueError):
            raise NetworkError('请先连接服务端', 401)
        with self.lock:
            self.cleanup()
            grant = self.sessions.get(key)
        if not grant:
            raise NetworkError('请先连接服务端', 401)
        return self.actor(grant)

    def logout(self, cookie):
        jar = SimpleCookie()
        jar.load(cookie)
        if 'agentboard_session' in jar:
            with self.lock:
                self.sessions.pop(jar['agentboard_session'].value, None)


def static_file(path):
    names = {'/': ('index.html', 'text/html; charset=utf-8'),
             '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
             '/style.css': ('style.css', 'text/css; charset=utf-8')}
    if path not in names:
        return None
    name, mime = names[path]
    return (Path(__file__).with_name('static') / name).read_bytes(), mime
