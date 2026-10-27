"""Owner-configured schedules enqueue ordinary jobs atomically with their receipts."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import settings
from .db import uid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS schedules(
    id TEXT PRIMARY KEY,name TEXT NOT NULL,request TEXT NOT NULL,project TEXT NOT NULL,
    cadence TEXT NOT NULL,start_local TEXT NOT NULL,timezone TEXT NOT NULL,
    status TEXT NOT NULL,next_run TEXT,last_job_id TEXT,version INTEGER NOT NULL,
    created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_runs(
    schedule_id TEXT NOT NULL,due TEXT NOT NULL,job_id TEXT NOT NULL,created TEXT NOT NULL,
    PRIMARY KEY(schedule_id,due));
'''
UTC = timezone.utc
TERMINAL = {'completed', 'cancelled', 'failed'}


def utcnow():
    return datetime.now(UTC)


def local_instant(local, zone):
    # Use the first occurrence of a repeated time; move a nonexistent time to
    # the first valid minute after the DST gap.
    for _ in range(1441):
        instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            return instant
        local += timedelta(minutes=1)
    raise ValueError('The selected local date does not exist in this timezone.')


def next_occurrence(row, after):
    start = datetime.fromisoformat(row['start_local'])
    zone = ZoneInfo(row['timezone'])
    first = local_instant(start, zone)
    if first > after:
        return first
    if row['cadence'] == 'once':
        return None
    period = 1 if row['cadence'] == 'daily' else 7
    days = max(0, (after.astimezone(zone).date() - start.date()).days)
    candidate = start + timedelta(days=(days // period) * period)
    while True:
        instant = local_instant(candidate, zone)
        if instant > after:
            return instant
        candidate += timedelta(days=period)


class Schedules:
    def __init__(self, store):
        self.store = store

    def list(self):
        rows = self.store.rows("SELECT * FROM schedules WHERE status!='deleted' ORDER BY created DESC,id")
        for row in rows:
            row['runs'] = self.store.rows('''SELECT r.*,j.status,j.title FROM schedule_runs r
                JOIN jobs j ON j.id=r.job_id WHERE schedule_id=? ORDER BY r.created DESC LIMIT 20''', (row['id'],))
        return rows

    def save(self, body, id=None):
        if not isinstance(body, dict):
            raise ValueError('Schedule must be an object.')
        row = {}
        for key, limit in [('name', 200), ('request', 100000), ('project', 200), ('timezone', 100), ('start_local', 32), ('cadence', 20)]:
            value = body.get(key, '')
            if not isinstance(value, str) or len(value) > limit or (key != 'project' and not value.strip()):
                raise ValueError('Enter a valid ' + key.replace('_', ' ') + '.')
            row[key] = value.strip()
        if row['project'] and row['project'] not in settings()['projects']:
            raise ValueError('Unknown project.')
        if row['cadence'] not in {'once', 'daily', 'weekly'}:
            raise ValueError('Choose once, daily, or weekly.')
        try:
            ZoneInfo(row['timezone'])
            start = datetime.fromisoformat(row['start_local'])
            if start.tzinfo is not None or len(row['start_local']) < 16:
                raise ValueError()
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Enter a local date/time and a valid IANA timezone, such as America/Chicago.') from None
        stamp = utcnow()
        upcoming = next_occurrence(row, stamp)
        if upcoming is None:
            raise ValueError('A one-time schedule must be in the future.')
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if id:
                old = c.execute("SELECT * FROM schedules WHERE id=? AND status!='deleted'", (id,)).fetchone()
                self._version(old, body)
                status = 'paused' if old['status'] == 'paused' else 'active'
                c.execute('''UPDATE schedules SET name=?,request=?,project=?,cadence=?,start_local=?,timezone=?,
                    next_run=?,status=?,version=version+1,updated=? WHERE id=?''',
                    tuple(row[k] for k in ('name','request','project','cadence','start_local','timezone')) + (upcoming.isoformat(),status,stamp.isoformat(),id))
            else:
                id = uid()
                c.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (id,row['name'],row['request'],row['project'],row['cadence'],row['start_local'],row['timezone'],
                     'active',upcoming.isoformat(),None,1,stamp.isoformat(),stamp.isoformat()))
        return self.store.one('SELECT * FROM schedules WHERE id=?', (id,))

    @staticmethod
    def _version(row, body):
        if row is None:
            raise ValueError('Schedule not found.')
        if type(body.get('version')) is not int or row['version'] != body['version']:
            raise ValueError('Schedule changed. Refresh and try again.')

    def change(self, id, body):
        action = body.get('action')
        if action not in {'pause', 'resume', 'delete'}:
            raise ValueError('Unknown schedule action.')
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM schedules WHERE id=? AND status!='deleted'", (id,)).fetchone()
            self._version(row, body)
            status = {'pause':'paused','resume':'active','delete':'deleted'}[action]
            if action == 'resume' and not row['next_run']:
                raise ValueError('Edit the completed schedule to choose a new start time.')
            c.execute('UPDATE schedules SET status=?,version=version+1,updated=? WHERE id=?', (status,utcnow().isoformat(),id))
        return {'id':id,'status':status}

    def tick(self, at=None):
        at = at or utcnow()
        created = []
        # A single transaction claims the occurrence, creates its job and advances
        # the schedule. A crash or concurrent tick cannot leave half a handoff.
        with self.store.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute("SELECT * FROM schedules WHERE status='active' AND next_run<=?", (at.isoformat(),)).fetchall()
            for row in rows:
                previous = c.execute('SELECT status FROM jobs WHERE id=?', (row['last_job_id'],)).fetchone()
                if previous and previous['status'] not in TERMINAL:
                    continue
                if row['project'] and row['project'] not in settings()['projects']:
                    continue  # The owner must fix a removed project before running.
                job_id = uid()
                stamp = at.isoformat()
                c.execute('INSERT INTO jobs(id,parent_id,title,request,org,project,status,created,updated) VALUES(?,NULL,?,?,?,?,?,?,?)',
                    (job_id,row['name'],row['request'],'Chief of Staff',row['project'],'queued',stamp,stamp))
                c.execute('INSERT INTO events(job_id,kind,body,created) VALUES(?,?,?,?)',
                    (job_id,'created',f"Scheduled task: {row['name']} (due {row['next_run']}; {row['timezone']}).",stamp))
                c.execute('INSERT INTO schedule_runs VALUES(?,?,?,?)', (row['id'],row['next_run'],job_id,stamp))
                upcoming = next_occurrence(row, at)
                c.execute('UPDATE schedules SET next_run=?,last_job_id=?,status=?,version=version+1,updated=? WHERE id=?',
                    (upcoming.isoformat() if upcoming else None,job_id,'active' if upcoming else 'completed',stamp,row['id']))
                created.append(job_id)
        return created
