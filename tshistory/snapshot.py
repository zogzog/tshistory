import pandas as pd

from sqlalchemy import Table, Column, Integer, ForeignKey
from sqlalchemy.sql.expression import select, desc
from sqlalchemy.dialects.postgresql import BYTEA, TIMESTAMP

from tshistory.util import (
    mindate,
    maxdate,
    subset,
    SeriesServices,
)


TABLES = {}


class Snapshot(SeriesServices):
    __slots__ = ('cn', 'name', 'tsh')
    _max_bucket_size = 250
    _min_bucket_size = 10

    def __init__(self, cn, tsh, seriename):
        self.cn = cn
        self.tsh = tsh
        self.name = seriename

    @property
    def namespace(self):
        return '{}.snapshot'.format(self.tsh.namespace)

    @property
    def table(self):
        tablename = '{}.{}'.format(self.namespace, self.name)
        table = TABLES.get(tablename)
        if table is None:
            TABLES[tablename] = table = Table(
                self.name, self.tsh.schema.meta,
                Column('id', Integer, primary_key=True),
                Column('start', TIMESTAMP(timezone=True), index=True),
                Column('end', TIMESTAMP(timezone=True), index=True),
                Column('chunk', BYTEA),
                Column('parent', Integer,
                       ForeignKey('{}.{}.id'.format(
                           self.namespace,
                           self.name)),
                       index=True),
                schema=self.namespace,
                extend_existing=True
            )
        return table

    def split(self, ts):
        if len(ts) < self._max_bucket_size:
            return [ts]

        buckets = []
        for start in range(0, len(ts),
                           self._max_bucket_size):
            buckets.append(ts[start:start + self._max_bucket_size])
        return buckets

    def insert_buckets(self, parent, buckets):
        for bucket in buckets:
            start = mindate(bucket)
            end = maxdate(bucket)
            sql = self.table.insert().values(
                start=start,
                end=end,
                parent=parent,
                chunk=self._serialize(bucket)
            )
            parent = self.cn.execute(sql).inserted_primary_key[0]

        return parent

    def create(self, initial_ts):
        self.table.create(self.cn)
        buckets = self.split(initial_ts)
        return self.insert_buckets(None, buckets)

    def update(self, diff):
        # get last chunkhead for cset
        tstable = self.tsh._get_ts_table(self.cn, self.name)
        headsql = select(
            [tstable.c.snapshot]
        ).order_by(desc(tstable.c.id)
        ).limit(1)
        head = self.cn.execute(headsql).scalar()

        # get raw chunks matching the limits
        diffstart = mindate(diff)
        rawchunks = self.rawchunks(head, diffstart)
        cid, parent, _ = rawchunks[0]
        oldsnapshot = pd.concat(row[2] for row in rawchunks)

        if (len(oldsnapshot) >= self._min_bucket_size and
            diffstart > maxdate(oldsnapshot)):
            # append: let't not rewrite anything
            newsnapshot = diff
            parent = cid
        else:
            # we got a point override, need to patch
            newsnapshot = self.patch(oldsnapshot, diff)
        buckets = self.split(newsnapshot)

        return self.insert_buckets(parent, buckets)

    def rawchunks(self, head, from_value_date=None):
        where = ''
        if from_value_date:
            where = 'where chunks.end >= %(start)s '

        sql = """
        with recursive allchunks as (
            select chunks.id as cid,
                   chunks.parent as parent,
                   chunks.chunk as chunk
            from "{namespace}"."{table}" as chunks
            where chunks.id = {head}
          union
            select chunks.id as cid,
                   chunks.parent as parent,
                   chunks.chunk as chunk
            from "{namespace}"."{table}" as chunks
            join allchunks on chunks.id = allchunks.parent
            {where}
        )
        select cid, parent, chunk from allchunks
        """.format(namespace=self.namespace,
                   table=self.name,
                   head=head,
                   where=where)
        res = self.cn.execute(sql, start=from_value_date)
        chunks = [(cid, parent,
                   self.tsh._ensure_tz_consistency(
                       self.cn, self._deserialize(rawchunk, self.name)))
                  for cid, parent, rawchunk in res.fetchall()]
        chunks.reverse()
        return chunks

    def chunk(self, head, from_value_date=None, to_value_date=None):
        chunks = self.rawchunks(head, from_value_date)
        snapdata = pd.concat(row[2] for row in chunks)
        return subset(snapdata,
            from_value_date, to_value_date
        )

    @property
    def first(self):
        return self.find(qfilter=[lambda _, table: table.c.id == 1])[1]

    def last(self, from_value_date=None, to_value_date=None):
        return self.find(from_value_date=from_value_date,
                         to_value_date=to_value_date)[1]

    def find(self, qfilter=(),
             from_value_date=None, to_value_date=None):
        cset = self.tsh.schema.changeset
        table = self.tsh._get_ts_table(self.cn, self.name)
        sql = select([table.c.cset, table.c.snapshot]
        ).order_by(desc(table.c.id)
        ).limit(1
        ).select_from(table.join(cset))

        if qfilter:
            sql = sql.where(table.c.cset <= cset.c.id)
            for filtercb in qfilter:
                sql = sql.where(filtercb(cset, table))

        try:
            csid, cid = self.cn.execute(sql).fetchone()
        except TypeError:
            # this happens *only* because of the from/to restriction
            return None, None

        chunk = self.chunk(cid, from_value_date, to_value_date)
        return csid, chunk

    def build_upto(self, qfilter=(),
                   from_value_date=None, to_value_date=None):
        csid, snapshot = self.find(qfilter,
                                   from_value_date=from_value_date,
                                   to_value_date=to_value_date)

        if csid is None:
            return

        cset = self.tsh.schema.changeset
        table = self.tsh._get_ts_table(self.cn, self.name)
        sql = select([table.c.id,
                      table.c.diff,
                      cset.c.insertion_date]
        ).order_by(table.c.id
        ).where(table.c.cset > csid
        ).where(table.c.cset == cset.c.id)

        if qfilter:
            for filtercb in qfilter:
                sql = sql.where(filtercb(cset, table))

        alldiffs = self.cn.execute(sql).fetchall()
        if not len(alldiffs):
            return snapshot

        # initial ts
        ts = self._deserialize(alldiffs[0].diff, self.name)
        ts = self.tsh._ensure_tz_consistency(self.cn, ts)
        for row in alldiffs[1:]:
            diff = subset(self._deserialize(row.diff, self.name),
                          from_value_date, to_value_date)
            diff = self.tsh._ensure_tz_consistency(self.cn, diff)
            ts = self.patch(ts, diff)
        ts = self.patch(snapshot, ts)
        assert ts.index.dtype.name == 'datetime64[ns]' or len(ts) == 0
        return ts
