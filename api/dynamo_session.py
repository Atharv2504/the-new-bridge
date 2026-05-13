"""Server-side Flask sessions stored in Amazon DynamoDB (not signed cookies)."""

import json
import time
import uuid

import boto3
from flask.sessions import SessionInterface, SessionMixin
from flask.sessions import SecureCookieSessionInterface
from werkzeug.datastructures import CallbackDict


class DynamoSession(CallbackDict, SessionMixin):
    def __init__(self, initial=None, sid=None):
        def on_update(_):
            self.modified = True

        CallbackDict.__init__(self, initial or {}, on_update)
        self.sid = sid
        self.modified = bool(initial)


class DynamoDBSessionInterface(SessionInterface):
    """Partition key on the table must be ``session_id`` (String). Optional numeric ``ttl`` for DynamoDB TTL."""

    def __init__(self, table_name: str, region: str):
        self.table_name = table_name
        self.region = region
        self._table = None
        self.fallback = SecureCookieSessionInterface()

    @property
    def table(self):
        if self._table is None:
            self._table = boto3.resource('dynamodb', region_name=self.region).Table(self.table_name)
        return self._table

    def open_session(self, app, request):
        sid = request.cookies.get(getattr(app, 'session_cookie_name', 'session'))
        if not sid:
            return self.fallback.open_session(app, request) or DynamoSession(sid=None)
        try:
            resp = self.table.get_item(Key={'session_id': sid})
            item = resp.get('Item')
            if not item or 'payload' not in item:
                return self.fallback.open_session(app, request) or DynamoSession(sid=None)
            data = json.loads(item['payload'])
            if not isinstance(data, dict):
                return self.fallback.open_session(app, request) or DynamoSession(sid=None)
            return DynamoSession(data, sid=sid)
        except Exception:
            return self.fallback.open_session(app, request) or DynamoSession(sid=None)

    def save_session(self, app, session, response):
        domain = self.get_cookie_domain(app)
        path = self.get_cookie_path(app)
        name = getattr(app, 'session_cookie_name', 'session')
        secure = self.get_cookie_secure(app)
        samesite = self.get_cookie_samesite(app)
        httponly = self.get_cookie_httponly(app)

        if not session:
            if session.modified and getattr(session, 'sid', None):
                try:
                    self.table.delete_item(Key={'session_id': session.sid})
                except Exception:
                    pass
                response.delete_cookie(name, domain=domain, path=path)
            return

        if not isinstance(session, DynamoSession):
            session = DynamoSession(dict(session), sid=getattr(session, 'sid', None))

        sid = session.sid or str(uuid.uuid4())
        session.sid = sid
        max_age = int(app.permanent_session_lifetime.total_seconds())
        ttl = int(time.time()) + max_age

        try:
            self.table.put_item(
                Item={
                    'session_id': sid,
                    'payload': json.dumps(dict(session)),
                    'ttl': ttl,
                }
            )

            response.set_cookie(
                name,
                sid,
                max_age=max_age,
                httponly=httponly,
                secure=secure,
                samesite=samesite,
                path=path,
                domain=domain,
            )
        except Exception as e:
            print(f'DynamoDB session write failed; using signed cookie session: {e}')
            fallback_session = self.fallback.session_class(dict(session))
            fallback_session.modified = True
            self.fallback.save_session(app, fallback_session, response)
