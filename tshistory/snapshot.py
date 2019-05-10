import zlib
from array import array
import struct
from pathlib import Path

import pandas as pd
import numpy as np

from tshistory.util import (
    SeriesServices,
    sqlfile
)

SCHEMA = Path(__file__).parent / 'snapshot.sql'


class Snapshot(SeriesServices):
    __slots__ = ('cn', 'name', 'tsh')
    _max_bucket_size = 250

    def __init__(self, cn, tsh, seriename):
        self.cn = cn
        self.tsh = tsh
        self.seriename = seriename
        self.name = self.tsh._serie_to_tablename(cn, seriename)

    @property
    def table(self):
        return sqlfile(SCHEMA,
                       namespace=self.tsh.namespace,
                       tablename=self.name)

    # optimized/asymmetric de/serialisation

    @property
    def isstr(self):
        return self.tsh.metadata(self.cn, self.seriename)['value_type'] == 'object'

    def _serialize(self, ts):
        if ts is None:
            return None
        # use `view` as a workarround for "cannot include dtype 'M' in a buffer"
        indexes = np.ascontiguousarray(ts.index.values).view(np.uint8).data.tobytes()
        indexes_size = struct.pack('!L', len(indexes))

        if self.isstr:
            # string separatd by 0 and nones/nans represented as 3 (ETX)
            END, ETX = b'\0'.decode(), b'\3'.decode()
            # first, safety belt
            for s in ts.values:
                if not pd.isnull(s):
                    assert END not in s and ETX not in s
            values = b'\0'.join(b'\3' if pd.isnull(v) else v.encode('utf-8')
                                for v in ts.values)
        else:
            values = ts.values.data.tobytes()
        return zlib.compress(indexes_size + indexes + values)

    def _ensure_tz_consistency(self, ts):
        """Return timeserie with tz aware index or not depending on metadata
        tzaware.
        """
        assert ts.name is not None
        metadata = self.tsh.metadata(self.cn, ts.name)
        if metadata and metadata.get('tzaware', False):
            return ts.tz_localize('UTC')
        return ts

    def _decodechunk(self, bytestring):
        bytestring = zlib.decompress(bytestring)
        [indexes_size] = struct.unpack('!L', bytestring[:4])
        values_offset = indexes_size + 4
        return bytestring[4:values_offset], bytestring[values_offset:]

    def _chunks_to_ts(self, chunks):
        chunks = (self._decodechunk(chunk) for chunk in chunks)
        indexchunks, valueschunks = list(zip(*chunks))
        metadata = self.tsh.metadata(self.cn, self.seriename)

        # array is a workaround for an obscure bug with pandas.isin
        index = np.frombuffer(
            array('d', b''.join(indexchunks)),
            metadata['index_dtype']
        )

        if self.isstr:
            values = [v.decode('utf-8') if v != b'\3' else None
                      for bvalues in valueschunks
                      for v in bvalues.split(b'\0')]
        else:
            values = np.frombuffer(
                b''.join(valueschunks),
                metadata['value_dtype']
            )

        assert len(values) == len(index)
        serie = pd.Series(values, index=index)
        assert serie.index.is_monotonic_increasing
        serie.name = self.seriename

        return self._ensure_tz_consistency(serie)

    # /serialisation

    def buckets(self, ts):
        if len(ts) < self._max_bucket_size:
            return [ts]

        buckets = []
        for start in range(0, len(ts),
                           self._max_bucket_size):
            buckets.append(ts[start:start + self._max_bucket_size])
        return buckets

    def insert_buckets(self, parent, ts):
        for bucket in self.buckets(ts):
            start = bucket.index.min()
            end = bucket.index.max()
            sql = (f'insert into "{self.tsh.namespace}.snapshot"."{self.name}" '
                   '(cstart, cend, parent, chunk) '
                   'values (%s, %s, %s, %s)'
                   'returning id')
            parent = self.cn.execute(
                sql,
                start,
                end,
                parent,
                self._serialize(bucket)
            ).scalar()

        return parent

    def create(self, initial_ts):
        self.cn.execute(self.table)
        return self.insert_buckets(None, initial_ts)

    def update(self, diff):
        # get last chunkhead for cset
        # tstable = self.tsh._get_ts_table(self.cn, self.seriename)
        tablename = self.tsh._serie_to_tablename(self.cn, self.seriename)
        headsql = ('select snapshot '
                   f'from "{self.tsh.namespace}.timeserie"."{tablename}" '
                   'order by id desc limit 1')
        head = self.cn.execute(headsql).scalar()

        # get raw chunks matching the limits
        diffstart = diff.index.min()
        rawchunks = self.rawchunks(head, diffstart)
        cid, parent, _ = rawchunks[0]
        oldsnapshot = self._chunks_to_ts(row[2] for row in rawchunks)

        if diffstart > oldsnapshot.index.max():
            # append: let't not rewrite anything
            newsnapshot = diff
            parent = cid
        else:
            # we got a point override, need to patch
            newsnapshot = self.patch(oldsnapshot, diff)

        return self.insert_buckets(parent, newsnapshot)

    rawsql = """
        with recursive allchunks as (
            select chunks.id as cid,
                   chunks.parent as parent,
                   chunks.chunk as chunk
            from "{namespace}"."{table}" as chunks
            where chunks.id in ({heads})
          union
            select chunks.id as cid,
                   chunks.parent as parent,
                   chunks.chunk as chunk
            from "{namespace}"."{table}" as chunks
            join allchunks on chunks.id = allchunks.parent
            {where}
        )
        select cid, parent, chunk from allchunks
    """

    def rawchunks(self, head, from_value_date=None):
        where = ''
        if from_value_date:
            where = 'where chunks.cend >= %(start)s '

        sql = self.rawsql.format(
            namespace=f'{self.tsh.namespace}.snapshot',
            table=self.name,
            heads=','.join([str(head)]),
            where=where
        )
        res = self.cn.execute(sql, start=from_value_date)
        chunks = [(cid, parent, rawchunk)
                  for cid, parent, rawchunk in res.fetchall()]
        chunks.reverse()
        return chunks

    def chunk(self, head, from_value_date=None, to_value_date=None):
        snapdata = self._chunks_to_ts(
            raw[2] for raw in self.rawchunks(head, from_value_date)
        )
        return snapdata.loc[from_value_date:to_value_date]

    def last(self, from_value_date=None, to_value_date=None):
        return self.find(from_value_date=from_value_date,
                         to_value_date=to_value_date)[1]

    def last_id(self, from_value_date=None, to_value_date=None):
        return self.find(from_value_date=from_value_date,
                         to_value_date=to_value_date)[0]

    def cset_heads_query(self, csetfilter=(), order='desc'):
        tablename = self.tsh._serie_to_tablename(self.cn, self.seriename)
        sql = [
            'select ts.cset, ts.snapshot '
            f'from "{self.tsh.namespace}.timeserie"."{tablename}" as ts, '
            f'      "{self.tsh.namespace}".changeset as cset'
            ' where cset.id = ts.cset '
        ]

        if csetfilter:
            sql.append('and ts.cset <= cset.id ')
            for filtercb in csetfilter:
                sql.append('and ' + filtercb)

        sql.append(f'order by ts.id {order} ')
        return sql

    def find(self, csetfilter=(),
             from_value_date=None, to_value_date=None):

        sql = self.cset_heads_query(csetfilter)
        sql.append('limit 1')

        sql = ''.join(sql)

        try:
            csid, cid = self.cn.execute(sql).fetchone()
        except TypeError:
            # this happens *only* because of the from/to restriction
            return None, None

        chunk = self.chunk(cid, from_value_date, to_value_date)
        return csid, chunk

    def allchunks(self, heads, from_value_date=None):
        where = ''
        if from_value_date:
            where = 'where chunks.cend >= %(start)s '

        sql = self.rawsql.format(
            namespace=f'{self.tsh.namespace}.snapshot',
            table=self.name,
            heads=','.join(str(head) for head in heads),
            where=where
        )
        res = self.cn.execute(sql, start=from_value_date)
        chunks = {cid: (parent, rawchunk)
                  for cid, parent, rawchunk in res.fetchall()}
        return chunks

    def findall(self, revs, from_value_date, to_value_date):
        csets = [rev for rev, _ in revs if rev is not None]
        # csid -> heads

        sql = self.cset_heads_query((f'cset.id >= {min(csets)}',
                                     f'cset.id <= {max(csets)}'),
                                     order='asc')
        sql = ''.join(sql)

        cset_snap_map = {
            row.cset: row.snapshot
            for row in self.cn.execute(sql).fetchall()
        }
        rawchunks = self.allchunks(
            sorted(cset_snap_map.values()),
            from_value_date
        )

        series = []
        for cset, idate in revs:
            if cset is None:
                series.append((idate, None))
                continue
            chunks = []
            head = cset_snap_map[cset]
            while True:
                parent, chunk = rawchunks.get(head, (None, None))
                if chunk is None:
                    break
                chunks.append(chunk)
                head = parent
            series.append(
                (idate, self._chunks_to_ts(reversed(chunks)).loc[
                        from_value_date:to_value_date
                    ]
                )
            )
        return series

    def garbage(self):
        """ inefficient but simple garbage list builder
        garbage chunks are created on strip operations
        """
        tablename = self.tsh._serie_to_tablename(self.cn, self.seriename)
        reachablesql = f"""
        with recursive heads as (
            select snapshot from "{self.tsh.namespace}.timeserie"."{tablename}"
          ),
          allchunks as (
            select chunks.id as cid,
                   chunks.parent as parent
            from "{self.tsh.namespace}.snapshot"."{self.name}" as chunks
            where chunks.id in (select * from heads)
          union
            select chunks.id as cid,
                   chunks.parent as parent
            from "{self.tsh.namespace}.snapshot"."{self.name}" as chunks
            join allchunks on chunks.id = allchunks.parent
        )
        select cid from allchunks
        """

        reachable_chunks = {
            rev for rev, in self.cn.execute(reachablesql)
        }
        allsql = f'select id from "{self.tsh.namespace}.snapshot"."{self.name}" '
        allchuks = {
            rev for rev, in self.cn.execute(allsql).fetchall()
        }

        return allchuks - reachable_chunks

    def reclaim(self):
        todelete = ','.join(str(id) for id in self.garbage())
        sql = (f'delete from "{self.tsh.namespace}.snapshot"."{self.name}" '
               f'where id in ({todelete})')
        self.cn.execute(sql)
