from datetime import datetime
import logging
import hashlib
import uuid
from threading import Lock
import json
from pathlib import Path

import pandas as pd

from deprecated import deprecated
from sqlhelp import sqlfile, select

from tshistory.util import (
    closed_overlaps,
    num2float,
    SeriesServices,
    start_end,
    tx,
    tzaware_serie
)
from tshistory.snapshot import Snapshot

L = logging.getLogger('tshistory.tsio')
SERIESSCHEMA = Path(__file__).parent / 'series.sql'


class timeseries(SeriesServices):
    namespace = 'tsh'
    schema = None
    metadatacache = None
    metakeys = {
        'tzaware',
        'index_type',
        'index_names',
        'index_dtype',
        'value_dtype',
        'value_type'
    }
    registry_map = None
    serie_tablename = None
    create_lock_id = None
    cachelock = Lock()

    def __init__(self, namespace='tsh'):
        self.namespace = namespace
        self.metadatacache = {}
        self.registry_map = {}
        self.serie_tablename = {}
        self.create_lock_id = sum(ord(c) for c in namespace)

    @tx
    def insert(self, cn, newts, seriename, author,
               metadata=None,
               _insertion_date=None):
        """Create a new revision of a given time series

        newts: pandas.Series with date index
        seriename: str unique identifier of the serie
        author: str free-form author name
        metadata: optional dict for changeset metadata
        """
        assert isinstance(newts, pd.Series), 'Not a pd.Series'
        assert isinstance(seriename, str), 'Name not a string'
        assert isinstance(author, str), 'Author not a string'
        assert metadata is None or isinstance(metadata, dict), 'Bad format for metadata'
        assert (_insertion_date is None or
                isinstance(_insertion_date, datetime)), 'Bad format for insertion date'
        assert not newts.index.duplicated().any(), 'There are some duplicates in the index'

        assert newts.index.notna().all(), 'The index contains NaT entries'
        if not newts.index.is_monotonic_increasing:
            newts = newts.sort_index()

        newts = num2float(newts)

        if not len(newts):
            return

        assert ('<M8[ns]' == newts.index.dtype or
                'datetime' in str(newts.index.dtype) and not
                isinstance(newts.index, pd.MultiIndex))

        newts.name = seriename
        tablename = self._serie_to_tablename(cn, seriename)

        if tablename is None:
            return self._create(cn, newts, seriename, author,
                                metadata, _insertion_date)

        return self._update(cn, tablename, newts, seriename, author,
                            metadata, _insertion_date)

    def list_series(self, cn):
        """Return the mapping of all series to their type"""
        sql = f'select seriename from "{self.namespace}".registry '
        return {
            row.seriename: 'primary'
            for row in cn.execute(sql)
        }

    def get(self, cn, seriename, revision_date=None,
            from_value_date=None, to_value_date=None,
            _keep_nans=False):
        """Compute and return the serie of a given name

        revision_date: datetime filter to get previous versions of the
        serie

        """
        if not self.exists(cn, seriename):
            return

        csetfilter = []
        if revision_date:
            csetfilter.append(
                lambda q: q.where(
                    f'cset.insertion_date <= %(idate)s', idate=revision_date
                )
            )
        snap = Snapshot(cn, self, seriename)
        _, current = snap.find(csetfilter=csetfilter,
                               from_value_date=from_value_date,
                               to_value_date=to_value_date)

        if current is not None and not _keep_nans:
            current.name = seriename
            current = current[~current.isnull()]
        return current

    def metadata(self, cn, seriename):
        """Return metadata dict of timeserie."""
        if seriename in self.metadatacache:
            return self.metadatacache[seriename]
        sql = (f'select metadata from "{self.namespace}".registry '
               'where seriename = %(seriename)s')
        meta = cn.execute(sql, seriename=seriename).scalar()
        if meta is not None:
            self.metadatacache[seriename] = meta
        return meta

    @tx
    def update_metadata(self, cn, seriename, metadata, internal=False):
        assert isinstance(metadata, dict)
        assert internal or not set(metadata.keys()) & self.metakeys
        meta = self.metadata(cn, seriename)
        # remove al but internal stuff
        newmeta = {
            key: meta[key]
            for key in self.metakeys
            if meta.get(key) is not None
        }
        newmeta.update(metadata)
        sql = (f'update "{self.namespace}".registry as reg '
               'set metadata = %(metadata)s '
               'where reg.seriename = %(seriename)s')
        self.metadatacache.pop(seriename)
        cn.execute(
            sql,
            metadata=json.dumps(newmeta),
            seriename=seriename
        )

    def changeset_metadata(self, cn, csid):
        assert isinstance(csid, int)
        q = select(
            'metadata'
        ).table(
            f'"{self.namespace}".changeset'
        ).where(
            f'id = %(csid)s', csid=csid
        )
        return q.do(cn).scalar()

    def type(self, cn, name):
        return 'primary'

    @tx
    def history(self, cn, seriename,
                from_insertion_date=None,
                to_insertion_date=None,
                from_value_date=None,
                to_value_date=None,
                deltabefore=None,
                deltaafter=None,
                diffmode=False,
                _keep_nans=False):
        tablename = self._serie_to_tablename(cn, seriename)
        if tablename is None:
            return

        q = select(
            'cset.id', 'cset.insertion_date'
        ).table(
            f'"{self.namespace}.timeserie"."{tablename}" as ts'
        ).join(
            f'"{self.namespace}".changeset as cset on cset.id = ts.cset'
        )

        if from_insertion_date:
            q.where(
                'cset.insertion_date >= %(from_idate)s',
                from_idate=from_insertion_date
            )
        if to_insertion_date:
            q.where(
                'cset.insertion_date <= %(to_idate)s ',
                to_idate=to_insertion_date
            )

        if from_value_date or to_value_date:
            q.where(
                closed_overlaps(from_value_date, to_value_date),
                fromdate=from_value_date,
                todate=to_value_date
            )

        q.order('cset.id')
        revs = q.do(cn).fetchall()
        if not revs:
            return {}

        revs = [
            (csid, pd.Timestamp(idate).astimezone('UTC'))
            for csid, idate in revs
        ]

        if diffmode:
            # compute the previous serie value
            first_csid = revs[0][0]
            previous_csid = self._previous_cset(cn, seriename, first_csid)
            revs.insert(0, (previous_csid, None))

        snapshot = Snapshot(cn, self, seriename)
        series = []
        if (deltabefore, deltaafter) != (None, None):
            for csid, idate in revs:
                from_date = None
                to_date = None
                if deltabefore is not None:
                    from_date = idate - deltabefore
                if deltaafter is not None:
                    to_date = idate + deltaafter
                series.append((
                    idate,
                    snapshot.find(
                        csetfilter=[
                            lambda q: q.where('cset.id = %(csid)s', csid=csid)
                        ],
                        from_value_date=from_date,
                        to_value_date=to_date)[1]
                    )
                )
        else:
            series = snapshot.findall(revs,
                                      from_value_date,
                                      to_value_date
            )

        if diffmode:
            diffs = []
            for (_revdate_a, serie_a), (revdate_b, serie_b) in zip(series, series[1:]):
                if serie_a is None:
                    # when we scan the entirety of the history: there exists no "previous" serie
                    # we therefore consider the first serie as a diff to the "null" serie
                    diffs.append((revdate_b, serie_b))
                else:
                    diffs.append((revdate_b, self.diff(serie_a, serie_b)))
            series = diffs
        else:
            series = [
                (idate, ts if _keep_nans else ts.dropna() )
                 for idate, ts in series
            ]

        return dict(series)

    @tx
    def staircase(self, cn, seriename, delta,
                  from_value_date=None,
                  to_value_date=None):
        """ compute a series whose value dates are bounded to be
        `delta` time after the insertion dates and where we
        keep the most recent ones
        """
        histo = self.history(
            cn, seriename, deltabefore=-delta,
            from_value_date=from_value_date,
            to_value_date=to_value_date,
            _keep_nans=True
        )
        if histo is None:
            return None

        vimap = {}
        vvmap = {}
        for idate, series in histo.items():
            for vdate, value in series.iteritems():
                if vdate not in vimap or vimap[vdate] < idate:
                    vimap[vdate] = idate
                    vvmap[vdate] = value

        ts = pd.Series(vvmap).sort_index().loc[from_value_date:to_value_date]
        ts.name = seriename
        return ts.dropna()

    def exists(self, cn, seriename):
        return self._serie_to_tablename(cn, seriename) is not None

    def latest_insertion_date(self, cn, seriename):
        tablename = self._serie_to_tablename(cn, seriename)
        q = select(
            'max(insertion_date)'
        ).table(
            f'"{self.namespace}".changeset as cset',
            f'"{self.namespace}.timeserie"."{tablename}" as tstable'
        ).where(
               'cset.id = tstable.cset'
        )
        return pd.Timestamp(
            q.do(cn).scalar()
        ).astimezone('UTC')

    def insertion_dates(self, cn, seriename,
                        fromdate=None, todate=None):
        tablename = self._serie_to_tablename(cn, seriename)
        q = select(
            'insertion_date'
        ).table(
            f'"{self.namespace}".changeset as cset',
            f'"{self.namespace}.timeserie"."{tablename}" as tstable'
        ).where(
            'cset.id = tstable.cset'
        ).order('cset.id')

        if fromdate:
            q.where(
                'cset.insertion_date >= %(fromdate)s',
                fromdate=fromdate
            )
        if todate:
            q.where(
                'cset.insertion_date <= %(todate)s',
                todate=todate
            )

        return [
            pd.Timestamp(idate).astimezone('UTC')
            for idate, in q.do(cn).fetchall()
        ]

    def last_id(self, cn, seriename):
        snapshot = Snapshot(cn, self, seriename)
        return snapshot.last_id()

    def changeset_at(self, cn, seriename, revdate, mode='strict'):
        operators = {
            'strict': '=',
            'before': '<=',
            'after': '>='
        }
        tablename = self._serie_to_tablename(cn, seriename)
        assert mode in operators
        q = select(
            'cset'
        ).table(
            f'"{self.namespace}.timeserie"."{tablename}" as tstable',
            f'"{self.namespace}".changeset as cset '
        ).where(
            'cset.id = tstable.cset',
            f'cset.insertion_date {operators[mode]} %(revdate)s',
            revdate=revdate
        )
        return q.do(cn).scalar()

    @tx
    def rename(self, cn, oldname, newname):
        sql = (f'update "{self.namespace}".registry '
               'set seriename = %(newname)s '
               'where seriename = %(oldname)s')
        cn.execute(sql, oldname=oldname, newname=newname)
        self._resetcaches()

    @tx
    def delete(self, cn, seriename):
        tablename = self._serie_to_tablename(cn, seriename)
        if tablename is None:
            print('not deleting unknown series', seriename, self.namespace)
            return
        # changeset will keep ghost entries
        # whose cleanup is costly
        # we will mark them as from a deleted series
        # update changeset.metadata
        msg = f'belonged to deleted series `{seriename}`'
        csetsql = f'select cset from "{self.namespace}.timeserie"."{tablename}"'
        for csid, in cn.execute(csetsql):
            metadata = self.changeset_metadata(cn, csid) or {}
            metadata['tshistory.info'] = msg
            cn.execute(
                f'update "{self.namespace}".changeset '
                'set metadata = %(metadata)s '
                'where id = %(csid)s',
                csid=csid,
                metadata=json.dumps(metadata)
            )

        rid, tablename = cn.execute(
            f'select id, table_name from "{self.namespace}".registry '
            'where seriename = %(seriename)s',
            seriename=seriename
        ).fetchone()
        # drop series tables
        cn.execute(
            f'drop table "{self.namespace}.timeserie"."{tablename}" cascade'
        )
        cn.execute(
            f'drop table "{self.namespace}.snapshot"."{tablename}" cascade'
        )
        cn.execute(f'delete from "{self.namespace}".registry '
                   'where id = %(rid)s',
                   rid=rid)
        # -> this will transitively cleanup state changeset_series entries
        self._resetcaches()

    @tx
    def strip(self, cn, seriename, csid):
        # wipe the diffs
        tablename = self._serie_to_tablename(cn, seriename)
        sql = (f'delete from "{self.namespace}.timeserie"."{tablename}" '
               'where cset >= %(csid)s')
        cn.execute(sql, csid=csid)

        logs = self.log(cn, fromrev=csid, names=(seriename,))
        assert logs
        for log in logs:
            csid = log['rev']
            # set in metadata the fact that this changeset
            # has been stripped (hence is no longer being referenced)
            metadata = self.changeset_metadata(cn, csid) or {}
            metadata['tshistory.info'] = f'got stripped from {csid}'
            sql = (f'update "{self.namespace}".changeset '
                   'set metadata = %(metadata)s '
                   'where id = %(csid)s')
            cn.execute(sql, csid=csid, metadata=json.dumps(metadata))
            # delete changset_serie item
            sql = (f'delete from "{self.namespace}".changeset_series as css '
                   'where css.cset = %(csid)s')
            cn.execute(sql, csid=csid)

        snapshot = Snapshot(cn, self, seriename)
        snapshot.reclaim()

    def info(self, cn):
        """Gather global statistics on the current tshistory repository
        """
        sql = f'select count(*) from "{self.namespace}".registry'
        stats = {'series count': cn.execute(sql).scalar()}
        sql = f'select max(id) from "{self.namespace}".changeset'
        stats['changeset count'] = cn.execute(sql).scalar()
        sql = (f'select distinct seriename from "{self.namespace}".registry '
               'order by seriename')
        stats['serie names'] = [row for row, in cn.execute(sql).fetchall()]
        return stats

    def log(self, cn, limit=0, names=None, authors=None,
            fromrev=None, torev=None,
            fromdate=None, todate=None):
        """Build a structure showing the history of all the series in the db,
        per changeset, in chronological order.
        """
        log = []

        q = select(
            'cset.id', 'cset.author', 'cset.insertion_date', 'cset.metadata',
            opt='distinct'
        ).table(
            f'"{self.namespace}".changeset as cset'
        ).join(
            f'"{self.namespace}".changeset_series as css on css.cset = cset.id',
            f'"{self.namespace}".registry as reg on reg.id = css.serie'
        )

        if names:
            q.where(
                'reg.seriename in %(names)s',
                names=tuple(names)
            )
        if authors:
            q.where(
                'cset.author in %(authors)s',
                author=tuple(authors)
            )
        if fromrev:
            q.where('cset.id >= %(fromrev)s', fromrev=fromrev)
        if torev:
            q.where('cset.id <= %(torev)s', torev=torev)
        if fromdate:
            q.where('cset.insertion_date >= %(fromdate)s', fromdate=fromdate)
        if todate:
            q.where('cset.insertion_date <= %(todate)s', todate=todate)

        q.order('cset.id', 'desc')
        if limit:
            q.limit(int(limit))

        rset = q.do(cn)
        for csetid, author, revdate, meta in rset.fetchall():
            log.append({'rev': csetid, 'author': author,
                        'date': pd.Timestamp(revdate).tz_convert('utc'),
                        'meta': meta or {},
                        'name': self._changeset_series(cn, csetid)})

        log.sort(key=lambda rev: rev['rev'])
        return log

    def interval(self, cn, seriename, notz=False):
        tablename = self._serie_to_tablename(cn, seriename)
        if tablename is None:
            raise ValueError(f'no such serie: {seriename}')
        sql = (f'select tsstart, tsend '
               f'from "{self.namespace}.timeserie"."{tablename}" '
               f'order by cset desc limit 1')
        res = cn.execute(sql).fetchone()
        start, end = res.tsstart, res.tsend
        if self.metadata(cn, seriename).get('tzaware') and not notz:
            start, end = pd.Timestamp(start, tz='UTC'), pd.Timestamp(end, tz='UTC')
        return pd.Interval(left=start, right=end, closed='both')

    # /API
    # Helpers

    # creation / update

    def _create(self, cn, newts, seriename, author,
                metadata=None, insertion_date=None):
        start, end = start_end(newts, notz=False)
        if start is None:
            assert end is None
            # this is just full of nans
            return None
        # chop off unwanted nans
        newts = newts.loc[start:end]
        if len(newts) == 0:
            return None

        # at creation time we take an exclusive lock to avoid
        # a deadlock on created tables against the changeset-series fk
        cn.execute(
            f'select pg_advisory_xact_lock({self.create_lock_id})'
        )
        self._register_serie(cn, seriename, newts)
        snapshot = Snapshot(cn, self, seriename)
        csid = self._newchangeset(cn, author, insertion_date, metadata)
        head = snapshot.create(newts)
        start, end = start_end(newts)
        tablename = self._make_ts_table(cn, seriename)
        sql = (f'insert into "{self.namespace}.timeserie"."{tablename}" '
               '(cset, snapshot, tsstart, tsend) '
               f'values (%s, %s, %s, %s)')
        cn.execute(sql, csid, head,
                   start.to_pydatetime(), end.to_pydatetime())
        self._finalize_insertion(cn, csid, seriename)
        L.info('first insertion of %s (size=%s) by %s',
               seriename, len(newts), author)
        return newts

    def _update(self, cn, tablename, newts, seriename, author,
                metadata=None, insertion_date=None):
        self._validate(cn, newts, seriename)
        snapshot = Snapshot(cn, self, seriename)
        diff = self.diff(snapshot.last(newts.index.min(),
                                       newts.index.max()),
                         newts)
        if not len(diff):
            L.info('no difference in %s by %s (for ts of size %s)',
                   seriename, author, len(newts))
            return

        # compute series start/end stamps
        tsstart, tsend = start_end(newts)
        ival = self.interval(cn, seriename, notz=True)
        start = min(tsstart or ival.left, ival.left)
        end = max(tsend or ival.right, ival.right)

        if pd.isnull(diff[0]) or pd.isnull(diff[-1]):
            # we *might* be shrinking, let's look at the full series
            # and yes, shrinkers have a slow path
            last = snapshot.last()
            patched = self.patch(last, diff).dropna()
            if not len(patched):
                raise ValueError('complete erasure of a series is forbidden')
            if pd.isnull(diff[0]):
                start = patched.index[0]
            if pd.isnull(diff[-1]):
                end = patched.index[-1]

        csid = self._newchangeset(cn, author, insertion_date, metadata)
        head = snapshot.update(diff)
        sql = (f'insert into "{self.namespace}.timeserie"."{tablename}" '
               '(cset, snapshot, tsstart, tsend) '
               'values (%s, %s, %s, %s)')
        cn.execute(sql, csid, head, start, end)
        self._finalize_insertion(cn, csid, seriename)

        L.info('inserted diff (size=%s) for ts %s by %s',
               len(diff), seriename, author)
        return diff

    # serie table handling

    def _make_tablename(self, cn, seriename):
        """ compute the unqualified (no namespace) table name
        from a serie name, to allow arbitrary serie names
        """
        # default
        tablename = seriename
        # postgresql table names are limited to 63 chars.
        if len(seriename) > 63:
            tablename = hashlib.sha1(seriename.encode('utf-8')).hexdigest()

        # collision detection (collision can happen after a rename)
        if cn.execute(f'select table_name '
                      f'from "{self.namespace}".registry '
                      f'where table_name = %(seriename)s',
                      seriename=seriename).scalar():
            tablename = str(uuid.uuid4())

        return tablename

    def _serie_to_tablename(self, cn, seriename):
        tablename = self.serie_tablename.get(seriename)
        if tablename is not None:
            return tablename

        tablename = cn.execute(
            f'select table_name from "{self.namespace}".registry '
            f'where seriename = %(seriename)s',
            seriename=seriename
        ).scalar()
        if tablename is None:
            # creation time
            return
        self.serie_tablename[seriename] = tablename
        return tablename

    def _table_definition_for(self, cn, seriename):
        tablename = self._serie_to_tablename(cn, seriename)
        if tablename is None:
            # creation time
            tablename = self._make_tablename(cn, seriename)
        table = sqlfile(
            SERIESSCHEMA,
            namespace=self.namespace,
            tablename=tablename
        )
        return table, tablename

    def _make_ts_table(self, cn, seriename):
        table, tablename = self._table_definition_for(cn, seriename)
        cn.execute(table)
        return tablename

    def _series_initial_meta(self, cn, name, ts):
        index = ts.index
        inames = [name for name in index.names if name]
        return {
            'tzaware': tzaware_serie(ts),
            'index_type': index.dtype.name,
            'index_names': inames,
            'index_dtype': index.dtype.str,
            'value_dtype': ts.dtypes.str,
            'value_type': ts.dtypes.name
        }

    def _register_serie(self, cn, seriename, ts):
        sql = (f'insert into "{self.namespace}".registry '
               '(seriename, table_name, metadata) '
               'values (%s, %s, %s) '
               'returning id')
        table_name = self._make_tablename(cn, seriename)
        metadata = json.dumps(
            self._series_initial_meta(cn, seriename, ts)
        )
        regid = cn.execute(
            sql,
            seriename,
            table_name,
            metadata
        ).scalar()
        self.registry_map[seriename] = regid

    def _get_ts_table(self, cn, seriename):
        tablename = self._serie_to_tablename(cn, seriename)
        if tablename:
            return self._table_definition_for(cn, seriename)

    # changeset handling

    def _newchangeset(self, cn, author, insertion_date=None, metadata=None):
        if insertion_date is not None:
            assert insertion_date.tzinfo is not None
            idate = pd.Timestamp(insertion_date)
        else:
            idate = pd.Timestamp(datetime.utcnow(), tz='UTC')
        sql = (f'insert into "{self.namespace}".changeset '
               '(author, metadata, insertion_date) '
               'values (%s, %s, %s) '
               'returning id')
        if metadata:
            metadata = json.dumps(metadata)
        return cn.execute(
            sql,
            author,
            metadata,
            idate
        ).scalar()

    def _changeset_series(self, cn, csid):
        q = select(
            'seriename'
        ).table(
            f'"{self.namespace}".registry as reg',
        ).join(
            f'"{self.namespace}".changeset_series as css on css.serie = reg.id'
        ).where(
            'css.cset = %(csid)s', csid=csid
        )

        return q.do(cn).scalar()

    def _previous_cset(self, cn, seriename, csid):
        tablename = self._serie_to_tablename(cn, seriename)
        sql = (f'select cset from "{self.namespace}.timeserie"."{tablename}" '
               'where cset < %(csid)s '
               'order by cset desc limit 1')
        return cn.execute(sql, csid=csid).scalar()

    # insertion handling

    def _validate(self, cn, ts, seriename):
        if ts.isnull().all():
            # ts erasure
            return
        tstype = ts.dtype
        meta = self.metadata(cn, seriename)
        if tstype != meta['value_type']:
            m = (f'Type error when inserting {seriename}, '
                 f'new type is {tstype}, type in base is {meta["value_type"]}')
            raise Exception(m)
        if ts.index.dtype.name != meta['index_type']:
            raise Exception('Incompatible index types')

    def _name_to_regid(self, cn, seriename):
        regid = self.registry_map.get(seriename)
        if regid is not None:
            return regid

        sql = ('select id '
               f'from "{self.namespace}".registry '
               'where seriename = %(seriename)s')
        regid = self.registry_map[seriename] = cn.execute(
            sql,
            seriename=seriename
        ).scalar()
        return regid

    def _finalize_insertion(self, cn, csid, seriename):
        sql = (f'insert into "{self.namespace}".changeset_series '
               '(cset, serie) '
               'values (%s, %s)')
        cn.execute(sql, csid, self._name_to_regid(cn, seriename))

    def _resetcaches(self):
        with self.cachelock:
            self.metadatacache.clear()
            self.registry_map.clear()
            self.serie_tablename.clear()



@deprecated(reason='use the `timeseries` object instead')
class TimeSerie(timeseries):

    get_history = deprecated(timeseries.history)
    get_delta = deprecated(timeseries.staircase)
